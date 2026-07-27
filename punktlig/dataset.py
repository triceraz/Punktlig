"""Replay the archive into training rows, with a hard no-lookahead guarantee.

Every training row answers one question: "standing at poll time T, knowing only
what the feed had published up to T, what do we believe about stop S ahead,
and what actually happened there later?"

The no-lookahead rule is enforced structurally:
  - features come only from the poll being replayed (state at T)
  - the weather join picks the newest forecast snapshot taken at or before T
  - the label (actual arrival) must come from a strictly later poll
"""

import sys
from bisect import bisect_left, bisect_right
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


class HistoryIndex:
    """As-of-T history from a pre-pass over the same call cursor the replay uses.

    Two lookups share one scan:
      - typical(): mean observed runtime per (line, direction, from_stop,
        to_stop) segment, for the slack feature
      - last_pass(): the latest known passing of a stop by another vehicle on
        the same line, for the bunching features

    An observation becomes visible at the poll where its actual times first
    appeared, and lookups only see observations at or before T, so this
    history obeys the same no-lookahead rule as the weather join. Typical
    runtime is the running mean (prefix sums give O(1) as-of-T lookups; a
    median would cost a sort per lookup at replay scale).
    """

    def __init__(self, call_rows):
        first_known = {}  # order -> (from_actual, to_actual, seen_at, stop_ref)
        raw = defaultdict(list)
        raw_passes = defaultdict(list)  # (line, dir, stop) -> (seen_at, actual, delay, journey)
        current_key = None

        def flush(journey_line_dir):
            for o in sorted(first_known):
                nxt = first_known.get(o + 1)
                if nxt is None:
                    continue
                from_actual, _, seen_from, stop_from = first_known[o]
                _, to_actual, seen_to, stop_to = nxt
                runtime = _secs(to_actual, from_actual)
                if runtime is not None and runtime > 0:
                    raw[journey_line_dir + (stop_from, stop_to)].append(
                        (max(seen_from, seen_to), runtime)
                    )

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
                        raw_passes[(r[4], r[5], r[7])].append(
                            (seen_at, to_actual or from_actual, delay, r[0])
                        )
        if current_key is not None:
            flush(line_dir)

        # Parallel arrays per segment: observation times for bisect, prefix
        # sums for O(1) running means.
        self.segments = {}
        for seg, obs in raw.items():
            obs.sort()
            times = [t for t, _ in obs]
            sums = [0.0]
            for _, rt in obs:
                sums.append(sums[-1] + rt)
            self.segments[seg] = (times, sums)

        self.passes = {}
        for key, events in raw_passes.items():
            events.sort()
            self.passes[key] = ([e[0] for e in events], events)

        # Delay-level indexes for the network features: per stop across all
        # lines, and per (line, direction) across all stops. Prefix sums over
        # events sorted by observation time give O(1) windowed means.
        by_stop, by_line = defaultdict(list), defaultdict(list)
        for (line_ref, direction, stop_ref), events in raw_passes.items():
            by_stop[stop_ref].extend(events)
            by_line[(line_ref, direction)].extend(events)
        self.stop_delays = self._prefix(by_stop)
        self.line_delays = self._prefix(by_line)

    @staticmethod
    def _prefix(events_by_key):
        out = {}
        for key, events in events_by_key.items():
            events.sort()
            sums = [0.0]
            for e in events:
                sums.append(sums[-1] + e[2])
            out[key] = ([e[0] for e in events], sums)
        return out

    @staticmethod
    def _windowed_mean(entry, at_time, window):
        if not entry:
            return None
        times, sums = entry
        j = bisect_right(times, at_time)
        i = bisect_left(times, at_time - window)
        if j <= i:
            return None
        return (sums[j] - sums[i]) / (j - i)

    def stop_recent(self, at_time, stop_ref, window):
        """Mean delay of passes at the stop, any line, in the window before T."""
        return self._windowed_mean(self.stop_delays.get(stop_ref), at_time, window)

    def line_recent(self, at_time, line_ref, direction, window):
        """Mean delay of passes on the line, any stop, in the window before T."""
        return self._windowed_mean(
            self.line_delays.get((line_ref, direction)), at_time, window
        )

    def typical(self, at_time, line_ref, direction, stop_from, stop_to):
        """Mean observed runtime for the segment, over observations known at T."""
        entry = self.segments.get((line_ref, direction, stop_from, stop_to))
        if not entry:
            return None
        times, sums = entry
        idx = bisect_right(times, at_time)
        if idx < SLACK_MIN_OBS:
            return None
        return sums[idx] / idx

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
    WHERE c.journey_ref IS NOT NULL AND c.order_no IS NOT NULL
"""


def _open_sources(archive_path, parquet_dir):
    """Return (weather_rows, call_rows, close) over hot SQLite plus any compacted Parquet.

    The caller must invoke close() after consuming the rows; on Windows an
    open connection blocks deletion of the underlying database file.
    """
    call_files = _parquet_files(parquet_dir, "calls")
    if not call_files:
        src = db.connect(archive_path)
        weather_rows = src.execute(
            f"SELECT {WEATHER_COLS} FROM weather_snapshot ORDER BY polled_at"
        )
        call_rows = src.execute(
            f"SELECT {CALL_COLS} FROM ({SQLITE_CALL_SQL.format(prefix='')}) {CALL_ORDER}"
        )
        return weather_rows, call_rows, src.close

    import duckdb  # analysis extra; only needed once parquet files exist

    con = duckdb.connect()
    con.execute("INSTALL sqlite; LOAD sqlite;")
    con.execute(f"ATTACH '{archive_path}' AS src (TYPE sqlite, READ_ONLY)")

    weather_sql = f"SELECT {WEATHER_COLS} FROM src.weather_snapshot"
    call_sql = SQLITE_CALL_SQL.format(prefix="src.")
    weather_files = _parquet_files(parquet_dir, "weather")
    if weather_files:
        weather_sql += f" UNION ALL SELECT {WEATHER_COLS} FROM read_parquet(?)"
    call_sql = (
        f"SELECT {CALL_COLS} FROM ({call_sql}) UNION ALL "
        f"SELECT {CALL_COLS} FROM read_parquet(?) "
        "WHERE journey_ref IS NOT NULL AND order_no IS NOT NULL"
    )
    weather_rows = _iter_duck(
        con, weather_sql + " ORDER BY polled_at", [weather_files] if weather_files else None
    )
    call_rows = _iter_duck(con, call_sql + " " + CALL_ORDER, [call_files])
    return weather_rows, call_rows, con.close


def build(archive_path=DB_PATH, out_path=OUT_PATH, parquet_dir=PARQUET_DIR):
    out = db.connect(out_path)
    out.execute("DROP TABLE IF EXISTS training_row")  # rebuilds are idempotent, schema may gain columns
    out.executescript(SCHEMA)
    out.commit()

    # Pre-pass for the slack and bunching features: the cursors are
    # single-use, so the sources are opened twice, history first, then replay.
    _, history_rows, close_history = _open_sources(archive_path, parquet_dir)
    history = HistoryIndex(history_rows)
    close_history()

    situations = SituationIndex(_situation_rows(archive_path))

    weather_rows, cursor, close_sources = _open_sources(archive_path, parquet_dir)
    weather = WeatherIndex(weather_rows)

    n_rows = 0
    batch = []
    cols = (
        "poll_id polled_at journey_ref operating_date line_ref direction stop_ref stop_name "
        "order_no dow hour horizon_sec horizon_stops current_order n_recorded "
        "current_delay_sec delay_trend_sec fc_air_temp fc_precip_mm fc_wind_mps "
        "sched_runtime_sec seg_slack_sec headway_ahead_sec delay_ahead_sec "
        "stop_recent_delay_sec line_recent_delay_sec obs_age_sec since_last_stop_sec "
        "sx_line_active sx_network_active "
        "aimed_ts entur_expected_ts actual_ts label_delay_sec entur_pred_delay_sec"
    ).split()

    def flush():
        nonlocal batch
        if batch:
            marks = ", ".join("?" for _ in cols)
            out.executemany(
                f"INSERT INTO training_row ({', '.join(cols)}) VALUES ({marks})", batch
            )
            out.commit()
            batch = []

    for (journey_ref, operating_date), rows in groupby(cursor, key=lambda r: (r[0], r[1])):
        # A snapshot is keyed by (polled_at, poll_id): chronological first, with
        # poll_id only as a tiebreaker within a single source database.
        snapshots = [
            (snap_key, list(poll_rows))
            for snap_key, poll_rows in groupby(rows, key=lambda r: (r[3], r[2]))
        ]

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
                if not label or label[1] <= snap_key:
                    continue  # no ground truth yet, or truth not strictly later than T
                actual_ts_val = label[0]
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

                batch.append(
                    [
                        snap_key[1],
                        polled_at.isoformat(),
                        journey_ref,
                        operating_date,
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
                        actual_ts_val.isoformat(),
                        _secs(actual_ts_val, aimed),
                        _secs(expected, aimed),
                    ]
                )
                n_rows += 1
                if len(batch) >= 5000:
                    flush()

    flush()
    close_sources()
    out.close()
    return n_rows


if __name__ == "__main__":
    written = build()
    print(f"training rows written: {written} -> {OUT_PATH}")
    sys.exit(0)
