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

-- Positions for the stop references the feed uses. Fetched once from
-- JourneyPlanner so the network can be drawn from our own archive.
CREATE TABLE IF NOT EXISTS quay (
  quay_ref        TEXT PRIMARY KEY,
  name            TEXT,
  lat             REAL,
  lon             REAL,
  stop_place_ref  TEXT,
  stop_place_name TEXT,
  fetched_at      TEXT
);

-- One row per (SX snapshot, situation, affected line). A situation with no
-- line reference is network-wide and stored once with a NULL line_ref.
CREATE TABLE IF NOT EXISTS situation (
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  polled_at        TEXT NOT NULL,
  situation_number TEXT,
  line_ref         TEXT,
  progress         TEXT,
  severity         TEXT,
  report_type      TEXT,
  start_time       TEXT,
  end_time         TEXT
);

CREATE INDEX IF NOT EXISTS idx_situation_polled ON situation (polled_at);

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
    # The replay reads every call in chronological order per journey, which
    # no index can satisfy because the timestamp lives in the poll table. That
    # sort spilled into RAM and cost gigabytes once the archive passed twenty
    # million rows, so temporary results go to disk instead.
    conn.execute("PRAGMA temp_store=FILE")
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


SITUATION_COLS = (
    "polled_at situation_number line_ref progress severity report_type "
    "start_time end_time"
).split()


def insert_situations(conn, polled_at, situations):
    """Store one snapshot of the deviation feed, one row per affected line."""
    rows = []
    for s in situations:
        for line_ref in s["line_refs"] or [None]:
            rows.append([
                polled_at, s["situation_number"], line_ref, s["progress"],
                s["severity"], s["report_type"], s["start_time"], s["end_time"],
            ])
    if not rows:
        return 0
    marks = ", ".join("?" for _ in SITUATION_COLS)
    conn.executemany(
        f"INSERT INTO situation ({', '.join(SITUATION_COLS)}) VALUES ({marks})", rows
    )
    conn.commit()
    return len(rows)


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
