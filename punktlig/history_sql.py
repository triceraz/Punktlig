"""History features computed by the database rather than by dictionaries.

The Python index in `dataset` keeps a count and a sum per entity per time
bucket, held as Python objects. That is fine for one line group in one city
and hopeless for a whole city over months: the archive already has 6 194
stops and 11 925 line-direction-stop combinations, so the entry count is
entities times buckets and both keep growing.

DuckDB holds the same aggregates as columns, spills to disk when it has to,
and computes them with a scan rather than a Python loop. The answers are
identical, which the tests check against the dictionary version rather than
assume.

The as-of-T rule is unchanged and still tightened by bucketing: only buckets
that closed strictly before T are visible, because a bucket still filling
could contain observations from after T.
"""

from array import array

from .config import DB_PATH, PARQUET_DIR
from .dataset import (BUCKET_SECONDS, DUCK_MEMORY_LIMIT, HistoryLookups,
                      _duck_connect, _parquet_files)

# Every observation of a stop being passed, with the delay it was passed
# with and the moment that became known. One scan feeds every aggregate.
PASSES_SQL = """
CREATE OR REPLACE TABLE passes AS
SELECT
    line_ref,
    direction,
    stop_ref,
    journey_ref,
    operating_date,
    order_no,
    CAST(epoch(CAST(polled_at AS TIMESTAMPTZ)) AS BIGINT) // {bucket} AS bucket,
    epoch(COALESCE(CAST(actual_arr AS TIMESTAMPTZ), CAST(actual_dep AS TIMESTAMPTZ)))
        - epoch(COALESCE(CAST(aimed_arr AS TIMESTAMPTZ), CAST(aimed_dep AS TIMESTAMPTZ)))
        AS delay,
    epoch(COALESCE(CAST(actual_arr AS TIMESTAMPTZ), CAST(actual_dep AS TIMESTAMPTZ))) AS arrived,
    epoch(COALESCE(CAST(actual_dep AS TIMESTAMPTZ), CAST(actual_arr AS TIMESTAMPTZ))) AS departed,
    CAST(epoch(CAST(polled_at AS TIMESTAMPTZ)) AS BIGINT) AS seen_at
FROM calls
WHERE call_type = 'recorded'
  AND cancelled = 0
  AND COALESCE(actual_arr, actual_dep) IS NOT NULL
  AND COALESCE(aimed_arr, aimed_dep) IS NOT NULL
"""

# A stop is counted once per journey, at the poll where it first appeared as
# passed, matching the replay's "first known" rule.
FIRST_SEEN_SQL = """
CREATE OR REPLACE TABLE first_seen AS
SELECT * EXCLUDE (rn) FROM (
    SELECT *, row_number() OVER (
        PARTITION BY journey_ref, operating_date, order_no ORDER BY seen_at
    ) AS rn
    FROM passes
) WHERE rn = 1
"""

# A segment runtime is known once both of its endpoints are, so it becomes
# visible at the later of the two observations.
SEGMENTS_SQL = """
CREATE OR REPLACE TABLE segment_buckets AS
SELECT line_ref, direction, stop_from, stop_to, bucket,
       COUNT(*) AS n, SUM(runtime) AS total
FROM (
    SELECT a.line_ref, a.direction,
           a.stop_ref AS stop_from, b.stop_ref AS stop_to,
           CAST(GREATEST(a.seen_at, b.seen_at) AS BIGINT) // {bucket} AS bucket,
           b.arrived - a.departed AS runtime
    FROM first_seen a
    JOIN first_seen b
      ON b.journey_ref = a.journey_ref
     AND b.operating_date = a.operating_date
     AND b.order_no = a.order_no + 1
)
WHERE runtime > 0
GROUP BY ALL
"""

STOP_BUCKETS_SQL = """
CREATE OR REPLACE TABLE stop_buckets AS
SELECT stop_ref, bucket, COUNT(*) AS n, SUM(delay) AS total
FROM first_seen GROUP BY ALL
"""

LINE_BUCKETS_SQL = """
CREATE OR REPLACE TABLE line_buckets AS
SELECT line_ref, direction, bucket, COUNT(*) AS n, SUM(delay) AS total
FROM first_seen GROUP BY ALL
"""


# One row per entity instead of one row per entity and bucket. The replay
# asks about an entity thousands of times, so the buckets are collected here
# and handed over as three parallel lists, which keeps the key string out of
# every row and lets the reader store the whole series as packed arrays.
GROUPED_SQL = {
    "segments": """
        SELECT line_ref, direction, stop_from, stop_to,
               list(bucket ORDER BY bucket) AS buckets,
               list(n ORDER BY bucket) AS counts,
               list(total ORDER BY bucket) AS totals
        FROM segment_buckets GROUP BY line_ref, direction, stop_from, stop_to
    """,
    "stop_delays": """
        SELECT stop_ref,
               list(bucket ORDER BY bucket) AS buckets,
               list(n ORDER BY bucket) AS counts,
               list(total ORDER BY bucket) AS totals
        FROM stop_buckets GROUP BY stop_ref
    """,
    "line_delays": """
        SELECT line_ref, direction,
               list(bucket ORDER BY bucket) AS buckets,
               list(n ORDER BY bucket) AS counts,
               list(total ORDER BY bucket) AS totals
        FROM line_buckets GROUP BY line_ref, direction
    """,
}


def _packed(buckets, counts, totals):
    """The prefix-summed triple `HistoryLookups` reads, in packed arrays.

    Python ints and floats cost about forty bytes each once a list has
    pointed at them. The archive produces millions of bucket entries, so they
    are stored as machine words instead; `bisect` treats an array as a
    sequence, so nothing above this line changes.
    """
    running_count, running_total = 0, 0.0
    packed_counts, packed_totals = array("q", [0]), array("d", [0.0])
    for count, total in zip(counts, totals):
        running_count += count
        running_total += total
        packed_counts.append(running_count)
        packed_totals.append(running_total)
    return array("q", buckets), packed_counts, packed_totals


class SqlHistory(HistoryLookups):
    """The same lookups as `dataset.HistoryIndex`, aggregated by DuckDB.

    The aggregates are read out once and then held as packed arrays, so the
    database is closed before the replay starts. That matters: a query per
    row costs tens of milliseconds, which is fine for a test and hopeless for
    a million rows, while the aggregate itself is around a million entries no
    matter how many vehicles produced it.
    """

    def __init__(self, archive_path=DB_PATH, parquet_dir=PARQUET_DIR,
                 bucket_seconds=BUCKET_SECONDS, memory_limit=DUCK_MEMORY_LIMIT):
        self.bucket = bucket_seconds
        self.passes = {}  # bunching is parked; no per-pass detail is kept
        con = _duck_connect(archive_path, memory_limit)
        try:
            columns = ("journey_ref, operating_date, line_ref, direction, call_type, "
                       "stop_ref, order_no, aimed_arr, actual_arr, aimed_dep, actual_dep, "
                       "cancelled")
            parts = [
                f"SELECT {columns}, p.polled_at FROM src.call_snapshot c "
                "JOIN src.poll p ON p.poll_id = c.poll_id"
            ]
            files = _parquet_files(parquet_dir, "calls")
            if files:
                parts.append(f"SELECT {columns}, polled_at FROM read_parquet({files!r})")
            con.execute(f"CREATE OR REPLACE VIEW calls AS {' UNION ALL '.join(parts)}")

            for statement in (PASSES_SQL, FIRST_SEEN_SQL, SEGMENTS_SQL,
                              STOP_BUCKETS_SQL, LINE_BUCKETS_SQL):
                con.execute(statement.format(bucket=bucket_seconds))
            con.execute("DROP TABLE passes")

            for name, sql in GROUPED_SQL.items():
                setattr(self, name, self._read(con, sql))
        finally:
            con.close()

    @staticmethod
    def _read(con, sql):
        """Entity key to packed prefix sums, streamed a batch at a time."""
        store = {}
        cursor = con.execute(sql)
        while True:
            rows = cursor.fetchmany(2000)
            if not rows:
                return store
            for row in rows:
                key = row[0] if len(row) == 4 else tuple(row[:-3])
                store[key] = _packed(*row[-3:])

    def close(self):
        """The database is already closed; kept so callers can be uniform."""
