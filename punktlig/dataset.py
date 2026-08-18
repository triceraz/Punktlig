"""Replay the archive into training rows, with a hard no-lookahead guarantee.

Every training row answers one question: "standing at poll time T, knowing only
what the feed had published up to T, what do we believe about stop S ahead,
and what actually happened there later?"

The no-lookahead rule is enforced structurally:
  - features come only from the poll being replayed (state at T)
  - the weather join picks the newest forecast snapshot taken at or before T
  - the label (actual arrival) must come from a strictly later poll
"""

import os
import sys
from bisect import bisect_left, bisect_right
from contextlib import contextmanager
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from itertools import groupby
from pathlib import Path

from . import db
from .config import DATA_DIR, DB_PATH, PARQUET_DIR

OUT_PATH = DATA_DIR / "dataset.db"

# Window for the network-state features: recent enough to reflect the current
# operational situation, wide enough to usually contain several passes.
RECENT_WINDOW = timedelta(minutes=30)

# A segment's typical runtime only counts once it has this many observations;
# a path sum over one-off runtimes is noise, not history.
SLACK_MIN_OBS = 3

# History is counted into buckets of this length rather than stored per
# observation, which is what keeps memory tied to how long the archive spans
# instead of to how much traffic it saw. Features are as fresh as one bucket.
BUCKET_SECONDS = 900

# The replay reads the same 18 columns in the same order regardless of where
# the rows live (hot SQLite, compacted Parquet, or both).
CALL_COLS = (
    "journey_ref, operating_date, poll_id, polled_at, line_ref, direction, "
    "call_type, stop_ref, stop_name, order_no, aimed_arr, expected_arr, "
    "actual_arr, aimed_dep, expected_dep, actual_dep, cancelled, call_cancelled, "
    "recorded_at"
)
# Snapshots are ordered by wall-clock time, not by poll_id: poll_id is only
# unique within one collector database, and archives from several machines
# (or collection eras) must interleave correctly when merged in parquet.
CALL_ORDER = "ORDER BY journey_ref, operating_date, polled_at, poll_id, order_no"
WEATHER_COLS = "polled_at, forecast_time, air_temp, precip_mm, wind_mps"

SCHEMA = """
CREATE TABLE IF NOT EXISTS training_row (
  id                  INTEGER PRIMARY KEY AUTOINCREMENT,
  poll_id             INTEGER,
  polled_at           TEXT,
  journey_ref         TEXT,
  operating_date      TEXT,
  line_ref            TEXT,
  direction           TEXT,
  stop_ref            TEXT,
  stop_name           TEXT,
  order_no            INTEGER,
  dow                 INTEGER,   -- 0=Monday
  hour                INTEGER,   -- local-ish hour taken from the aimed time's own offset
  horizon_sec         REAL,      -- how far ahead the arrival was expected at T
  horizon_stops       INTEGER,
  current_order       INTEGER,
  n_recorded          INTEGER,
  current_delay_sec   REAL,      -- delay at the last stop actually passed before T
  delay_trend_sec     REAL,      -- current delay minus delay ~3 stops earlier
  fc_air_temp         REAL,      -- forecast for the expected arrival hour, as known at T
  fc_precip_mm        REAL,
  fc_wind_mps         REAL,
  sched_runtime_sec   REAL,      -- aimed(target) minus aimed(current stop): scheduled drive time left
  seg_slack_sec       REAL,      -- sched_runtime minus typical observed runtime for the same path, as known at T
  headway_ahead_sec   REAL,      -- expected(target) minus when the vehicle ahead passed it, as known at T
  delay_ahead_sec     REAL,      -- that vehicle's delay when it passed the target stop
  stop_recent_delay_sec REAL,    -- mean delay at the target stop, any line, last 30 min known at T
  line_recent_delay_sec REAL,    -- mean delay on the line, any stop, last 30 min known at T
  obs_age_sec         REAL,      -- poll time minus the feed's own RecordedAtTime for the journey
  since_last_stop_sec REAL,      -- poll time minus when the vehicle actually passed its last stop
  sx_line_active      INTEGER,   -- deviation messages in force for this line, as known at T
  sx_network_active   INTEGER,   -- deviation messages in force anywhere, as known at T
  aimed_ts            TEXT,
  entur_expected_ts   TEXT,
  actual_ts           TEXT,
  label_delay_sec     REAL,      -- actual - aimed: what the model learns to predict
  entur_pred_delay_sec REAL      -- expected(T) - aimed: the baseline to beat
);
CREATE INDEX IF NOT EXISTS idx_row_horizon ON training_row (horizon_sec);
"""


def _ts(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None


def _secs(a, b):
    if a is None or b is None:
        return None
    return (a - b).total_seconds()


def _floor_hour(dt):
    return dt.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)


class WeatherIndex:
    """forecast_time-hour -> [(polled_at, temp, precip, wind)], for as-of-T lookups."""

    def __init__(self, rows):
        by_hour = defaultdict(list)
        for polled_at, forecast_time, temp, precip, wind in rows:
            key = _floor_hour(_ts(forecast_time))
            by_hour[key].append((_ts(polled_at), temp, precip, wind))
        # Parallel key lists so the bisect compares timestamps only.
        self.by_hour = {
            key: ([c[0] for c in cands], cands) for key, cands in by_hour.items()
        }

    def lookup(self, at_time, target_time):
        """Newest forecast for target_time's hour issued at or before at_time."""
        entry = self.by_hour.get(_floor_hour(target_time))
        if not entry:
            return None, None, None
        issued_times, candidates = entry
        idx = bisect_right(issued_times, at_time) - 1
        if idx < 0:
            return None, None, None
        _, temp, precip, wind = candidates[idx]
        return temp, precip, wind


class HistoryLookups:
    """The as-of-T questions the replay asks of history, and nothing else.

    Two things build the answers: `HistoryIndex` by scanning the archive in
    Python, and `history_sql.SqlHistory` by letting DuckDB aggregate. They
    disagree about cost, not about arithmetic, so the reading half lives here
    once and the tests can hold the two constructions against each other.

    Every subclass must set `bucket`, the three prefix-summed stores, and
    `passes`. A store maps an entity to (bucket keys, running counts, running
    sums), all three the same length plus a leading zero on the running pair,
    so a range is two subtractions after one binary search.
    """

    def _bucket_of(self, moment):
        return int(moment.timestamp()) // self.bucket

    @staticmethod
    def _prefix(buckets):
        """Sorted bucket keys with running count and sum, for O(log n) lookups."""
        keys = sorted(buckets)
        counts, sums = [0], [0.0]
        for key in keys:
            count, total = buckets[key]
            counts.append(counts[-1] + count)
            sums.append(sums[-1] + total)
        return keys, counts, sums

    def _closed_before(self, entry, at_time, since_bucket=None):
        """(count, sum) over buckets that closed before T, optionally windowed."""
        if not entry:
            return 0, 0.0
        keys, counts, sums = entry
        end = bisect_left(keys, self._bucket_of(at_time))
        start = 0 if since_bucket is None else bisect_left(keys, since_bucket)
        if end <= start:
            return 0, 0.0
        return counts[end] - counts[start], sums[end] - sums[start]

    def _recent(self, entry, at_time, window):
        since = self._bucket_of(at_time) - max(1, int(window.total_seconds()) // self.bucket)
        count, total = self._closed_before(entry, at_time, since_bucket=since)
        return total / count if count else None

    def stop_recent(self, at_time, stop_ref, window):
        """Mean delay of passes at the stop, any line, in the window before T."""
        return self._recent(self.stop_delays.get(stop_ref), at_time, window)

    def line_recent(self, at_time, line_ref, direction, window):
        """Mean delay of passes on the line, any stop, in the window before T."""
        return self._recent(
            self.line_delays.get((line_ref, direction)), at_time, window
        )

    def typical(self, at_time, line_ref, direction, stop_from, stop_to):
        """Mean observed runtime for the segment, over buckets closed before T."""
        count, total = self._closed_before(
            self.segments.get((line_ref, direction, stop_from, stop_to)), at_time
        )
        if count < SLACK_MIN_OBS:
            return None
        return total / count

    def last_pass(self, at_time, line_ref, direction, stop_ref, exclude_journey):
        """Latest pass of the stop by another vehicle on the line, known at T.

        Returns (actual_pass_time, delay_at_pass) or (None, None).
        """
        entry = self.passes.get((line_ref, direction, stop_ref))
        if not entry:
            return None, None
        times, events = entry
        idx = bisect_right(times, at_time) - 1
        while idx >= 0:
            _, actual, delay, journey_ref = events[idx]
            if journey_ref != exclude_journey:
                return actual, delay
            idx -= 1
        return None, None


class HistoryIndex(HistoryLookups):
    """As-of-T history, aggregated into time buckets so it fits in memory.

    Two lookups share one scan of the archive:
      - typical(): mean observed runtime per (line, direction, from_stop,
        to_stop) segment, for the schedule slack feature
      - stop_recent() and line_recent(): recent delay level around a stop and
        along a line, for the network features

    Observations are counted into buckets rather than stored individually.
    That is what makes the replay survive a growing archive: memory then
    depends on how many stops and segments exist times how many buckets the
    archive spans, not on how many vehicles have driven through them. Keeping
    every observation costs about a gigabyte per four million archived calls,
    which stops working within days once a whole city is collected.

    Only buckets that closed strictly before T are visible, so the
    no-lookahead rule survives the change and in fact tightens: a bucket
    still filling could contain observations from after T, so it is never
    read. Features are therefore as fresh as the bucket size, and that is the
    honest cost of the trade.
    """

    def __init__(self, call_rows, bucket_seconds=BUCKET_SECONDS, keep_passes=False):
        self.bucket = bucket_seconds
        self.keep_passes = keep_passes
        first_known = {}  # order -> (from_actual, to_actual, seen_at, stop_ref)
        segments = defaultdict(dict)   # seg -> bucket -> [count, sum]
        stop_delays = defaultdict(dict)
        line_delays = defaultdict(dict)
        raw_passes = defaultdict(list) if keep_passes else None
        current_key = None

        def add(store, key, seen_at, value):
            slot = store[key].setdefault(self._bucket_of(seen_at), [0, 0.0])
            slot[0] += 1
            slot[1] += value

        def flush(journey_line_dir):
            for o in sorted(first_known):
                nxt = first_known.get(o + 1)
                if nxt is None:
                    continue
                from_actual, _, seen_from, stop_from = first_known[o]
                _, to_actual, seen_to, stop_to = nxt
                runtime = _secs(to_actual, from_actual)
                if runtime is not None and runtime > 0:
                    add(segments, journey_line_dir + (stop_from, stop_to),
                        max(seen_from, seen_to), runtime)

        for r in call_rows:
            if r[16]:  # cancelled journey: its runtimes are not typical
                continue
            key = (r[0], r[1])
            if key != current_key:
                if current_key is not None:
                    flush(line_dir)
                current_key, first_known = key, {}
            line_dir = (r[4], r[5])
            if r[6] == "recorded" and r[9] is not None and r[9] not in first_known:
                from_actual = _ts(r[15]) or _ts(r[12])  # departure, else arrival
                to_actual = _ts(r[12]) or _ts(r[15])  # arrival, else departure
                if to_actual or from_actual:
                    seen_at = _ts(r[3])
                    first_known[r[9]] = (from_actual, to_actual, seen_at, r[7])
                    aimed = _ts(r[10]) or _ts(r[13])
                    delay = _secs(to_actual or from_actual, aimed)
                    if delay is not None:
                        add(stop_delays, r[7], seen_at, delay)
                        add(line_delays, line_dir, seen_at, delay)
                        if keep_passes:
                            raw_passes[(r[4], r[5], r[7])].append(
                                (seen_at, to_actual or from_actual, delay, r[0])
                            )
        if current_key is not None:
            flush(line_dir)

        self.segments = {k: self._prefix(v) for k, v in segments.items()}
        self.stop_delays = {k: self._prefix(v) for k, v in stop_delays.items()}
        self.line_delays = {k: self._prefix(v) for k, v in line_delays.items()}

        self.passes = {}
        if keep_passes:
            for key, events in raw_passes.items():
                events.sort()
                self.passes[key] = ([e[0] for e in events], events)


class SituationIndex:
    """Deviation messages in force at a given time, as known at that time.

    The feed is polled hourly and republishes every open situation, so the
    snapshot taken at or before T is the operator's own view of the network
    at T. Counting inside that snapshot, filtered by validity period, keeps
    the no-lookahead rule: a disruption published later is invisible.
    """

    def __init__(self, rows):
        snapshots = defaultdict(lambda: (defaultdict(list), {}))
        for polled_at, situation_number, line_ref, start, end in rows:
            by_line, all_situations = snapshots[_ts(polled_at)]
            window = (_ts(start), _ts(end))
            if line_ref:
                by_line[line_ref].append(window)
            all_situations[situation_number] = window
        self.times = sorted(snapshots)
        self.snapshots = [snapshots[t] for t in self.times]

    @staticmethod
    def _in_force(window, at_time):
        start, end = window
        return (start is None or start <= at_time) and (end is None or end >= at_time)

    def counts(self, at_time, line_ref):
        idx = bisect_right(self.times, at_time) - 1
        if idx < 0:
            return None, None
        by_line, all_situations = self.snapshots[idx]
        line = sum(1 for w in by_line.get(line_ref, ()) if self._in_force(w, at_time))
        network = sum(1 for w in all_situations.values() if self._in_force(w, at_time))
        return line, network


def _situation_rows(archive_path):
    conn = db.connect(archive_path)
    try:
        return conn.execute(
            "SELECT polled_at, situation_number, line_ref, start_time, end_time "
            "FROM situation"
        ).fetchall()
    finally:
        conn.close()


def _parquet_files(parquet_dir, sub):
    directory = Path(parquet_dir) / sub
    return sorted(str(p) for p in directory.glob("*.parquet")) if directory.is_dir() else []


def _hot_exclusion(files, column="p.polled_at"):
    """SQL keeping the hot archive's copy of a day out when parquet has it.

    The two tiers are meant to be disjoint: compaction deletes a day from hot
    SQLite only after its parquet export is verified. But the deletion can
    fail on its own, and did: on 2026-08-17 and the two nights after, the
    delete died with "database or disk is full" after the export had already
    been verified, so 2026-08-14 sat in both tiers. The replay reads the
    tiers with UNION ALL, so every one of that day's rows entered the dataset
    exactly twice, measured at a ratio of 1.999 against distinct keys, and
    the same signature shows the July days were doubled in the previous
    dataset too. Parquet wins because it is the verified copy; the hot rows
    are the leftovers of a failed delete.
    """
    days = sorted({Path(f).stem[:10] for f in files})
    if not days:
        return ""
    quoted = ", ".join(f"'{d}'" for d in days)
    return f" AND substr({column}, 1, 10) NOT IN ({quoted})"


def _iter_duck(con, sql, params=None):
    cur = con.execute(sql, params or [])
    while True:
        chunk = cur.fetchmany(50_000)
        if not chunk:
            return
        yield from chunk


SQLITE_CALL_SQL = f"""
    SELECT c.journey_ref, c.operating_date, c.poll_id, p.polled_at,
           c.line_ref, c.direction, c.call_type, c.stop_ref, c.stop_name,
           c.order_no, c.aimed_arr, c.expected_arr, c.actual_arr,
           c.aimed_dep, c.expected_dep, c.actual_dep,
           c.cancelled, c.call_cancelled, c.recorded_at
    FROM {{prefix}}call_snapshot c
    JOIN {{prefix}}poll p ON p.poll_id = c.poll_id
    WHERE c.journey_ref IS NOT NULL AND c.order_no IS NOT NULL{{sample}}
"""

# Journey references end in a hex character, so keeping the ones that end in
# a chosen set of digits is a deterministic, journey-atomic sample. Sampling
# whole journeys rather than rows matters: rows from one journey are near
# duplicates, and splitting them across the sample boundary would leak.
SAMPLE_DIGITS = "0123456789abcdef"


def _sample_clause(keep):
    """SQL fragment keeping `keep` sixteenths of all journeys, or nothing."""
    if not keep or keep >= len(SAMPLE_DIGITS):
        return ""
    digits = ", ".join(f"'{d}'" for d in SAMPLE_DIGITS[:keep])
    return f" AND lower(substr(c.journey_ref, -1)) IN ({digits})"


# DuckDB otherwise helps itself to four fifths of the machine and spills into
# the working directory. Replaying the archive means sorting every call ever
# archived, so both defaults are wrong here: the limit has to leave room for
# the replay itself, and the spill belongs on the disk that holds the archive
# rather than the one holding the code.
#
# Raised from two gigabytes on 2026-08-04, when the site export began failing
# outright as the archive grew: the history aggregate collects a list per
# entity and cannot spill that. Only one DuckDB job runs at a time, held apart
# by the lock, so the headroom is there to give.
DUCK_MEMORY_LIMIT = "4GB"


def _duck_connect(archive_path, memory_limit=DUCK_MEMORY_LIMIT):
    """A DuckDB connection that is bounded and spills somewhere sensible."""
    import duckdb  # analysis extra; only needed once parquet files exist

    con = duckdb.connect()
    con.execute(f"SET memory_limit='{memory_limit}'")
    # Nothing here depends on rows coming back in the order they were stored:
    # every query either aggregates or sorts explicitly. Dropping the
    # guarantee lets DuckDB stream large aggregations instead of buffering
    # them, which is the difference between finishing and running out.
    con.execute("SET preserve_insertion_order=false")
    spill = os.path.dirname(os.path.abspath(archive_path))
    if spill:
        con.execute(f"SET temp_directory='{spill}'")
    con.execute("INSTALL sqlite; LOAD sqlite;")
    con.execute(f"ATTACH '{archive_path}' AS src (TYPE sqlite, READ_ONLY)")
    return con


@contextmanager
def frozen_archive(archive_path):
    """Yield a path to a still copy of the hot database.

    The replay reads the archive for the better part of an hour while the
    collector keeps writing to it every minute. DuckDB's SQLite reader opens
    several connections to scan in parallel and they do not share one read
    transaction, so a row can be read from a page that is being rewritten;
    the symptom is a string that decodes as invalid unicode, hours in, on
    data that is provably clean when read on its own.

    VACUUM INTO writes a consistent copy in a single statement without
    blocking the writer, which removes the race rather than retrying it. The
    copy costs a couple of gigabytes of scratch and a minute of wall clock,
    against a replay that otherwise has to start over.
    """
    source = Path(archive_path)
    still = source.with_name(source.name + ".frozen")
    still.unlink(missing_ok=True)
    conn = db.connect(source)
    try:
        conn.execute("VACUUM INTO ?", (str(still),))
    finally:
        conn.close()
    try:
        yield str(still)
    finally:
        still.unlink(missing_ok=True)


def _open_sources(archive_path, parquet_dir, sample=0):
    """Return (weather_rows, call_rows, close) over hot SQLite plus any compacted Parquet.

    The caller must invoke close() after consuming the rows; on Windows an
    open connection blocks deletion of the underlying database file.
    """
    clause = _sample_clause(sample)
    call_files = _parquet_files(parquet_dir, "calls")
    if not call_files:
        src = db.connect(archive_path)
        weather_rows = src.execute(
            f"SELECT {WEATHER_COLS} FROM weather_snapshot ORDER BY polled_at"
        )
        call_rows = src.execute(
            f"SELECT {CALL_COLS} FROM "
            f"({SQLITE_CALL_SQL.format(prefix='', sample=clause)}) {CALL_ORDER}"
        )
        return weather_rows, call_rows, src.close

    con = _duck_connect(archive_path)

    weather_sql = f"SELECT {WEATHER_COLS} FROM src.weather_snapshot"
    call_sql = SQLITE_CALL_SQL.format(
        prefix="src.", sample=clause + _hot_exclusion(call_files))
    weather_files = _parquet_files(parquet_dir, "weather")
    if weather_files:
        weather_sql = (f"{weather_sql} WHERE 1=1"
                       f"{_hot_exclusion(weather_files, column='polled_at')}"
                       f" UNION ALL SELECT {WEATHER_COLS} FROM read_parquet(?)")
    call_sql = (
        f"SELECT {CALL_COLS} FROM ({call_sql}) UNION ALL "
        f"SELECT {CALL_COLS} FROM read_parquet(?) c "
        "WHERE journey_ref IS NOT NULL AND order_no IS NOT NULL"
        + clause
    )
    weather_rows = _iter_duck(
        con, weather_sql + " ORDER BY polled_at", [weather_files] if weather_files else None
    )
    call_rows = _iter_duck(con, call_sql + " " + CALL_ORDER, [call_files])
    return weather_rows, call_rows, con.close


def build(archive_path=DB_PATH, out_path=OUT_PATH, parquet_dir=PARQUET_DIR, sample=0,
          bucket_seconds=BUCKET_SECONDS, with_bunching=False, sql_history=True):
    """Replay the archive into training rows.

    History is aggregated by DuckDB, which scans the archive and spills to
    disk rather than building the counts as Python objects. That is what lets
    the whole archive be replayed: the Python pre-pass cost roughly a
    gigabyte per four million archived calls and had to be fed a sample to
    fit, and a sampled history is not only smaller but wrong in a quiet way,
    since "how late has this line been lately" then answers from a fraction
    of the vehicles that were actually out.

    `sample` keeps that many sixteenths of all journeys, whole journeys at a
    time so none is split across the boundary. It is a fallback for a machine
    that cannot spare the memory, not the normal path.

    `with_bunching` still needs the Python pre-pass, because bunching reads
    individual passings rather than bucket totals. Those features measured as
    a wash and are parked, so that cost is only paid when asked for.

    The whole replay reads one frozen copy of the hot database rather than
    the live file, so the collector can keep collecting throughout. Two
    passes over a moving archive would not even agree with each other.
    """
    with frozen_archive(archive_path) as still:
        return _build(still, out_path, parquet_dir, sample, bucket_seconds,
                      with_bunching, sql_history)


def _build(archive_path, out_path, parquet_dir, sample, bucket_seconds,
           with_bunching, sql_history):
    out = db.connect(out_path)
    out.execute("DROP TABLE IF EXISTS training_row")  # rebuilds are idempotent, schema may gain columns
    out.executescript(SCHEMA)
    out.commit()

    if sql_history and not with_bunching:
        from .history_sql import SqlHistory  # analysis extra; imported on use

        history = SqlHistory(archive_path, parquet_dir, bucket_seconds=bucket_seconds)
    else:
        # Pre-pass for the slack and bunching features: the cursors are
        # single-use, so the sources are opened twice, history first, then replay.
        _, history_rows, close_history = _open_sources(archive_path, parquet_dir, sample)
        history = HistoryIndex(history_rows, bucket_seconds=bucket_seconds,
                               keep_passes=with_bunching)
        close_history()

    situations = SituationIndex(_situation_rows(archive_path))

    weather_rows, cursor, close_sources = _open_sources(archive_path, parquet_dir, sample)
    weather = WeatherIndex(weather_rows)

    n_rows = 0
    batch = []
    cols = ROW_COLS

    def flush():
        nonlocal batch
        if batch:
            marks = ", ".join("?" for _ in cols)
            out.executemany(
                f"INSERT INTO training_row ({', '.join(cols)}) VALUES ({marks})", batch
            )
            out.commit()
            batch = []

    # The row loop is arithmetic, not disk. Under the job lock's background
    # mode the CPU runs at idle priority, measured at a 27x cost on pure
    # compute, and this loop wrote rows at 26 MB a minute for ten hours. At
    # full speed its I/O is still pull-paced by the row processing itself,
    # about a dozen MB a second at worst, which cannot starve a poll the way
    # the aggregation's bulk scans can. Those stay in background mode above.
    from .joblock import at_full_speed

    with at_full_speed():
        for row in iter_rows(cursor, history, weather, situations):
            batch.append(row)
            n_rows += 1
            if len(batch) >= 5000:
                flush()
        flush()

    close_sources()
    out.close()
    return n_rows


ROW_COLS = (
    "poll_id polled_at journey_ref operating_date line_ref direction stop_ref stop_name "
    "order_no dow hour horizon_sec horizon_stops current_order n_recorded "
    "current_delay_sec delay_trend_sec fc_air_temp fc_precip_mm fc_wind_mps "
    "sched_runtime_sec seg_slack_sec headway_ahead_sec delay_ahead_sec "
    "stop_recent_delay_sec line_recent_delay_sec obs_age_sec since_last_stop_sec "
    "sx_line_active sx_network_active "
    "aimed_ts entur_expected_ts actual_ts label_delay_sec entur_pred_delay_sec"
).split()


def _service_date(snapshots):
    """The day a journey was scheduled to start, for feeds that omit it.

    Read from the earliest aimed time anywhere in the journey, so it is the
    same answer at every snapshot even once the first calls have dropped out
    of the feed's rolling window.
    """
    earliest = None
    for _, poll_rows in snapshots:
        for r in poll_rows:
            aimed = _ts(r[10]) or _ts(r[13])
            if aimed and (earliest is None or aimed < earliest):
                earliest = aimed
    return earliest.date().isoformat() if earliest else None


def iter_rows(cursor, history, weather, situations, require_label=True):
    """Yield one row per (snapshot, future stop), in ROW_COLS order.

    Training and live prediction ask the same question and must therefore
    build the same features from the same code. The only difference is the
    label: replaying history requires ground truth from a strictly later
    snapshot, while predicting now has none by definition.
    """
    for (journey_ref, operating_date), rows in groupby(cursor, key=lambda r: (r[0], r[1])):
        # A snapshot is keyed by (polled_at, poll_id): chronological first, with
        # poll_id only as a tiebreaker within a single source database.
        snapshots = [
            (snap_key, list(poll_rows))
            for snap_key, poll_rows in groupby(rows, key=lambda r: (r[3], r[2]))
        ]

        # The train feeds publish no operating day, and training splits on it:
        # a row without one can never fall in the validation day, so those
        # journeys would train and never be measured. The service date is the
        # day the journey was scheduled to set off, which is what an operating
        # day means, and it is taken from the whole journey rather than the
        # current snapshot so it cannot drift as earlier calls age out.
        service_date = operating_date or _service_date(snapshots)

        # Ground truth: last seen actual time per stop order, remembering which
        # snapshot it came from so labels can be required to be later than features.
        truth = {}
        for snap_key, poll_rows in snapshots:
            for r in poll_rows:
                if r[6] == "recorded":
                    actual = _ts(r[12]) or _ts(r[15])  # prefer arrival, fall back to departure
                    if actual:
                        truth[r[9]] = (actual, snap_key)

        for snap_key, poll_rows in snapshots:
            polled_at = _ts(poll_rows[0][3])
            if poll_rows[0][16]:  # journey cancelled: no meaningful labels
                continue

            recorded = []
            actual_at_order = {}
            for r in poll_rows:
                if r[6] == "recorded":
                    actual = _ts(r[12]) or _ts(r[15])
                    aimed = _ts(r[10]) or _ts(r[13])
                    delay = _secs(actual, aimed)
                    if delay is not None:
                        recorded.append((r[9], delay))
                        actual_at_order[r[9]] = actual
            if not recorded:
                continue  # journey not started yet at T; v1 predicts en-route vehicles only
            recorded.sort()
            current_order, current_delay = recorded[-1]
            trend_base = [d for o, d in recorded if o <= current_order - 3]
            delay_trend = current_delay - trend_base[-1] if trend_base else None

            # How stale the picture is: the feed's own report time for this
            # vehicle, and how long since it actually passed a stop. Between
            # stops the operator sees live positions and we do not, so these
            # say how much the current-delay reading should be trusted.
            obs_age = _secs(polled_at, _ts(poll_rows[0][18]))
            since_last_stop = _secs(polled_at, actual_at_order.get(current_order))
            sx_line, sx_network = situations.counts(polled_at, poll_rows[0][4])

            order_stop = {r[9]: r[7] for r in poll_rows if r[9] is not None}
            cur_aimed = None
            for r in poll_rows:
                if r[9] == current_order and r[6] == "recorded":
                    cur_aimed = _ts(r[13]) or _ts(r[10])  # departure, else arrival
                    break

            for r in poll_rows:
                if r[6] != "estimated" or r[9] <= current_order or r[17]:
                    continue
                aimed = _ts(r[10]) or _ts(r[13])
                expected = _ts(r[11]) or _ts(r[14])
                if aimed is None or expected is None:
                    continue
                label = truth.get(r[9])
                if require_label and (not label or label[1] <= snap_key):
                    continue  # no ground truth yet, or truth not strictly later than T
                actual_ts_val = label[0] if label and label[1] > snap_key else None
                horizon = _secs(expected, polled_at)
                if horizon is None or horizon <= 0:
                    continue
                temp, precip, wind = weather.lookup(polled_at, expected)

                # Slack: scheduled remaining runtime vs the typical observed
                # runtime over the same path, using history known at T only.
                sched_runtime = _secs(aimed, cur_aimed)
                slack = None
                if sched_runtime is not None:
                    typical_sum = 0.0
                    for o in range(current_order, r[9]):
                        s_from, s_to = order_stop.get(o), order_stop.get(o + 1)
                        typ = (
                            history.typical(polled_at, r[4], r[5], s_from, s_to)
                            if s_from and s_to else None
                        )
                        if typ is None:
                            typical_sum = None
                            break
                        typical_sum += typ
                    if typical_sum is not None:
                        slack = sched_runtime - typical_sum

                # Bunching: predicted gap to the vehicle ahead at the target
                # stop, and how delayed that vehicle was when it passed.
                ahead_pass, delay_ahead = history.last_pass(
                    polled_at, r[4], r[5], r[7], journey_ref
                )
                headway_ahead = _secs(expected, ahead_pass)

                # Network state: delay level around the target in the last
                # half hour, at the stop (any line) and on the line (any stop).
                stop_recent = history.stop_recent(polled_at, r[7], RECENT_WINDOW)
                line_recent = history.line_recent(polled_at, r[4], r[5], RECENT_WINDOW)

                yield [
                        snap_key[1],
                        polled_at.isoformat(),
                        journey_ref,
                        service_date,
                        r[4],
                        r[5],
                        r[7],
                        r[8],
                        r[9],
                        aimed.weekday(),
                        aimed.hour,
                        horizon,
                        r[9] - current_order,
                        current_order,
                        len(recorded),
                        current_delay,
                        delay_trend,
                        temp,
                        precip,
                        wind,
                        sched_runtime,
                        slack,
                        headway_ahead,
                        delay_ahead,
                        stop_recent,
                        line_recent,
                        obs_age,
                        since_last_stop,
                        sx_line,
                        sx_network,
                        aimed.isoformat(),
                        expected.isoformat(),
                        actual_ts_val.isoformat() if actual_ts_val else None,
                        _secs(actual_ts_val, aimed),
                        _secs(expected, aimed),
                ]


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Replay the archive into training rows")
    parser.add_argument("--sample", type=int, default=0, metavar="N",
                        help="keep N sixteenths of all journeys (16 or 0 means all). "
                             "Only the training rows are thinned; history is "
                             "aggregated from everything either way")
    parser.add_argument("--python-history", action="store_true",
                        help="build history with the in-memory Python pre-pass "
                             "instead of DuckDB (needs --sample on a large archive)")
    args = parser.parse_args()
    from .joblock import heavy  # the site export runs every ten minutes

    with heavy("dataset"):
        written = build(sample=args.sample, sql_history=not args.python_history)
    if args.sample and args.sample < 16:
        print(f"sampled {args.sample}/16 of journeys")
    print(f"training rows written: {written} -> {OUT_PATH}")
    sys.exit(0)
