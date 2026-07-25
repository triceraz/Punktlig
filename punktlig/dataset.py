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
from bisect import bisect_right
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from itertools import groupby
from pathlib import Path

from . import db
from .config import DATA_DIR, DB_PATH, PARQUET_DIR

OUT_PATH = DATA_DIR / "dataset.db"

# The replay reads the same 18 columns in the same order regardless of where
# the rows live (hot SQLite, compacted Parquet, or both).
CALL_COLS = (
    "journey_ref, operating_date, poll_id, polled_at, line_ref, direction, "
    "call_type, stop_ref, stop_name, order_no, aimed_arr, expected_arr, "
    "actual_arr, aimed_dep, expected_dep, actual_dep, cancelled, call_cancelled"
)
CALL_ORDER = "ORDER BY journey_ref, operating_date, poll_id, order_no"
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
           c.cancelled, c.call_cancelled
    FROM {{prefix}}call_snapshot c
    JOIN {{prefix}}poll p ON p.poll_id = c.poll_id
    WHERE c.journey_ref IS NOT NULL AND c.order_no IS NOT NULL
"""


def _open_sources(archive_path, parquet_dir):
    """Return (weather_rows, call_rows) over hot SQLite plus any compacted Parquet."""
    call_files = _parquet_files(parquet_dir, "calls")
    if not call_files:
        src = db.connect(archive_path)
        weather_rows = src.execute(
            f"SELECT {WEATHER_COLS} FROM weather_snapshot ORDER BY polled_at"
        )
        call_rows = src.execute(
            f"SELECT {CALL_COLS} FROM ({SQLITE_CALL_SQL.format(prefix='')}) {CALL_ORDER}"
        )
        return weather_rows, call_rows

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
    return weather_rows, call_rows


def build(archive_path=DB_PATH, out_path=OUT_PATH, parquet_dir=PARQUET_DIR):
    out = db.connect(out_path)
    out.executescript(SCHEMA)
    out.execute("DELETE FROM training_row")  # rebuilds are idempotent
    out.commit()

    weather_rows, cursor = _open_sources(archive_path, parquet_dir)
    weather = WeatherIndex(weather_rows)

    n_rows = 0
    batch = []
    cols = (
        "poll_id polled_at journey_ref operating_date line_ref direction stop_ref stop_name "
        "order_no dow hour horizon_sec horizon_stops current_order n_recorded "
        "current_delay_sec delay_trend_sec fc_air_temp fc_precip_mm fc_wind_mps "
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
        snapshots = [(poll_id, list(poll_rows)) for poll_id, poll_rows in groupby(rows, key=lambda r: r[2])]

        # Ground truth: last seen actual time per stop order, remembering which
        # poll it came from so labels can be required to be later than features.
        truth = {}
        for poll_id, poll_rows in snapshots:
            for r in poll_rows:
                if r[6] == "recorded":
                    actual = _ts(r[12]) or _ts(r[15])  # prefer arrival, fall back to departure
                    if actual:
                        truth[r[9]] = (actual, poll_id)

        for poll_id, poll_rows in snapshots:
            polled_at = _ts(poll_rows[0][3])
            if poll_rows[0][16]:  # journey cancelled: no meaningful labels
                continue

            recorded = []
            for r in poll_rows:
                if r[6] == "recorded":
                    actual = _ts(r[12]) or _ts(r[15])
                    aimed = _ts(r[10]) or _ts(r[13])
                    delay = _secs(actual, aimed)
                    if delay is not None:
                        recorded.append((r[9], delay))
            if not recorded:
                continue  # journey not started yet at T; v1 predicts en-route vehicles only
            recorded.sort()
            current_order, current_delay = recorded[-1]
            trend_base = [d for o, d in recorded if o <= current_order - 3]
            delay_trend = current_delay - trend_base[-1] if trend_base else None

            for r in poll_rows:
                if r[6] != "estimated" or r[9] <= current_order or r[17]:
                    continue
                aimed = _ts(r[10]) or _ts(r[13])
                expected = _ts(r[11]) or _ts(r[14])
                if aimed is None or expected is None:
                    continue
                label = truth.get(r[9])
                if not label or label[1] <= poll_id:
                    continue  # no ground truth yet, or truth not strictly later than T
                actual_ts_val = label[0]
                horizon = _secs(expected, polled_at)
                if horizon is None or horizon <= 0:
                    continue
                temp, precip, wind = weather.lookup(polled_at, expected)
                batch.append(
                    [
                        poll_id,
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
    return n_rows


if __name__ == "__main__":
    written = build()
    print(f"training rows written: {written} -> {OUT_PATH}")
    sys.exit(0)
