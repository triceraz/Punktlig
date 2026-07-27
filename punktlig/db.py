"""SQLite storage. Append-only by design: snapshots are never updated or deleted.

The SQL sticks to the SQLite dialect so the same statements run against Turso
(hosted libSQL) when collection moves to a serverless function.
"""

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (
  k TEXT PRIMARY KEY,
  v TEXT
);

CREATE TABLE IF NOT EXISTS line (
  line_ref   TEXT PRIMARY KEY,
  mode       TEXT,
  public_code TEXT,
  name       TEXT,
  fetched_at TEXT
);

CREATE TABLE IF NOT EXISTS poll (
  poll_id     INTEGER PRIMARY KEY AUTOINCREMENT,
  polled_at   TEXT NOT NULL,
  feed        TEXT NOT NULL,
  dataset     TEXT,
  pages       INTEGER,
  n_journeys  INTEGER,
  n_calls     INTEGER,
  n_dropped   INTEGER,
  duration_ms INTEGER,
  error       TEXT
);

-- One row per (poll, journey, stop): the full prediction state of one stop
-- of one vehicle journey, as published at poll time. This is the core archive.
CREATE TABLE IF NOT EXISTS call_snapshot (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  poll_id        INTEGER NOT NULL,
  recorded_at    TEXT,
  line_ref       TEXT,
  direction      TEXT,
  journey_ref    TEXT,
  operating_date TEXT,
  operator_ref   TEXT,
  monitored      INTEGER,
  cancelled      INTEGER,
  call_type      TEXT,           -- 'recorded' (vehicle has passed) | 'estimated' (still a prediction)
  stop_ref       TEXT,
  stop_name      TEXT,
  order_no       INTEGER,
  aimed_arr      TEXT,
  expected_arr   TEXT,
  actual_arr     TEXT,
  aimed_dep      TEXT,
  expected_dep   TEXT,
  actual_dep     TEXT,
  call_cancelled INTEGER
);

CREATE INDEX IF NOT EXISTS idx_call_journey
  ON call_snapshot (journey_ref, operating_date, order_no);
CREATE INDEX IF NOT EXISTS idx_call_poll
  ON call_snapshot (poll_id);

CREATE TABLE IF NOT EXISTS weather_snapshot (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  polled_at     TEXT NOT NULL,
  lat           REAL,
  lon           REAL,
  forecast_time TEXT,
  air_temp      REAL,
  precip_mm     REAL,
  wind_mps      REAL,
  wind_dir      REAL,
  symbol        TEXT
);
"""


# Writers wait this long for a lock before raising. The collector shares the
# database with compaction and ad-hoc analysis, and a lock held for a few
# seconds is normal; failing instantly turns that into a lost poll.
BUSY_TIMEOUT_MS = 30_000


def connect(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=BUSY_TIMEOUT_MS / 1000)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    conn.executescript(SCHEMA)
    return conn


def kv_get(conn, key):
    row = conn.execute("SELECT v FROM kv WHERE k = ?", (key,)).fetchone()
    return row[0] if row else None


def kv_set(conn, key, value):
    conn.execute(
        "INSERT INTO kv (k, v) VALUES (?, ?) ON CONFLICT(k) DO UPDATE SET v = excluded.v",
        (key, value),
    )
    conn.commit()


def insert_poll(conn, **cols):
    keys = ", ".join(cols)
    marks = ", ".join("?" for _ in cols)
    cur = conn.execute(f"INSERT INTO poll ({keys}) VALUES ({marks})", list(cols.values()))
    conn.commit()
    return cur.lastrowid


CALL_COLS = (
    "poll_id recorded_at line_ref direction journey_ref operating_date operator_ref "
    "monitored cancelled call_type stop_ref stop_name order_no "
    "aimed_arr expected_arr actual_arr aimed_dep expected_dep actual_dep call_cancelled"
).split()


def insert_calls(conn, rows):
    if not rows:
        return
    marks = ", ".join("?" for _ in CALL_COLS)
    conn.executemany(
        f"INSERT INTO call_snapshot ({', '.join(CALL_COLS)}) VALUES ({marks})",
        [[r.get(c) for c in CALL_COLS] for r in rows],
    )
    conn.commit()


def insert_weather(conn, rows):
    if not rows:
        return
    cols = "polled_at lat lon forecast_time air_temp precip_mm wind_mps wind_dir symbol".split()
    marks = ", ".join("?" for _ in cols)
    conn.executemany(
        f"INSERT INTO weather_snapshot ({', '.join(cols)}) VALUES ({marks})",
        [[r.get(c) for c in cols] for r in rows],
    )
    conn.commit()
