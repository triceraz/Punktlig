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

from datetime import timedelta

from .config import DB_PATH, PARQUET_DIR
from .dataset import BUCKET_SECONDS, SLACK_MIN_OBS, _parquet_files

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


class SqlHistory:
    """The same lookups as `dataset.HistoryIndex`, answered by DuckDB."""

    def __init__(self, archive_path=DB_PATH, parquet_dir=PARQUET_DIR,
                 bucket_seconds=BUCKET_SECONDS, memory_limit="2GB"):
        import duckdb

        self.bucket = bucket_seconds
        self.con = duckdb.connect()
        self.con.execute(f"SET memory_limit='{memory_limit}'")
        self.con.execute("INSTALL sqlite; LOAD sqlite;")
        self.con.execute(f"ATTACH '{archive_path}' AS src (TYPE sqlite, READ_ONLY)")

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
        self.con.execute(f"CREATE OR REPLACE VIEW calls AS {' UNION ALL '.join(parts)}")

        for statement in (PASSES_SQL, FIRST_SEEN_SQL, SEGMENTS_SQL,
                          STOP_BUCKETS_SQL, LINE_BUCKETS_SQL):
            self.con.execute(statement.format(bucket=bucket_seconds))
        self.con.execute("DROP TABLE passes")

    def close(self):
        self.con.close()

    def _bucket_of(self, moment):
        return int(moment.timestamp()) // self.bucket

    def typical(self, at_time, line_ref, direction, stop_from, stop_to):
        row = self.con.execute(
            "SELECT SUM(n), SUM(total) FROM segment_buckets "
            "WHERE line_ref = ? AND direction IS NOT DISTINCT FROM ? "
            "AND stop_from = ? AND stop_to = ? AND bucket < ?",
            [line_ref, direction, stop_from, stop_to, self._bucket_of(at_time)],
        ).fetchone()
        if not row or not row[0] or row[0] < SLACK_MIN_OBS:
            return None
        return row[1] / row[0]

    def _recent(self, sql, params, at_time, window):
        end = self._bucket_of(at_time)
        start = end - max(1, int(window.total_seconds()) // self.bucket)
        row = self.con.execute(sql, params + [start, end]).fetchone()
        if not row or not row[0]:
            return None
        return row[1] / row[0]

    def stop_recent(self, at_time, stop_ref, window=timedelta(minutes=30)):
        return self._recent(
            "SELECT SUM(n), SUM(total) FROM stop_buckets "
            "WHERE stop_ref = ? AND bucket >= ? AND bucket < ?",
            [stop_ref], at_time, window,
        )

    def line_recent(self, at_time, line_ref, direction, window=timedelta(minutes=30)):
        return self._recent(
            "SELECT SUM(n), SUM(total) FROM line_buckets "
            "WHERE line_ref = ? AND direction IS NOT DISTINCT FROM ? "
            "AND bucket >= ? AND bucket < ?",
            [line_ref, direction], at_time, window,
        )

    def last_pass(self, at_time, line_ref, direction, stop_ref, exclude_journey):
        """Bunching is parked; the SQL version does not carry per-pass detail."""
        return None, None
