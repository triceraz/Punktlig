"""Move completed days out of the hot SQLite archive into Parquet files.

The hot database stays small so the collector and ad-hoc queries stay fast.
History lives in day-partitioned Parquet with zstd compression, which shrinks
this kind of repetitive data heavily and is what the analysis layer reads.

Safety rules:
  - only days strictly before today (UTC) are eligible, so a day is never
    exported while the collector is still adding to it
  - every export is verified by row count before the source rows are deleted
  - reruns are idempotent: an already-exported day is verified again and
    the deletion is retried, so a crash between export and delete self-heals

Raw XML directories older than the retention window are pruned at the end.
Requires duckdb (see the analysis extras in the README); the collector itself
never imports this module.
"""

import argparse
import shutil
import sys
from datetime import datetime, timedelta, timezone

from . import db
from .config import DB_PATH, HOT_KEEP_DAYS, PARQUET_DIR, RAW_DIR, RAW_KEEP_DAYS

TABLES = {
    "calls": (
        "SELECT c.journey_ref, c.operating_date, c.poll_id, p.polled_at, c.line_ref, "
        "c.direction, c.call_type, c.stop_ref, c.stop_name, c.order_no, "
        "c.aimed_arr, c.expected_arr, c.actual_arr, c.aimed_dep, c.expected_dep, "
        "c.actual_dep, c.cancelled, c.call_cancelled, c.recorded_at, c.operator_ref, c.monitored "
        "FROM src.call_snapshot c JOIN src.poll p ON p.poll_id = c.poll_id "
        "WHERE substr(p.polled_at, 1, 10) = '{day}'"
    ),
    "polls": "SELECT * FROM src.poll WHERE substr(polled_at, 1, 10) = '{day}'",
    "weather": "SELECT * FROM src.weather_snapshot WHERE substr(polled_at, 1, 10) = '{day}'",
}

COUNTS = {
    "calls": (
        "SELECT COUNT(*) FROM call_snapshot c JOIN poll p ON p.poll_id = c.poll_id "
        "WHERE substr(p.polled_at, 1, 10) = ?"
    ),
    "polls": "SELECT COUNT(*) FROM poll WHERE substr(polled_at, 1, 10) = ?",
    "weather": "SELECT COUNT(*) FROM weather_snapshot WHERE substr(polled_at, 1, 10) = ?",
}

DELETES = [
    "DELETE FROM call_snapshot WHERE poll_id IN "
    "(SELECT poll_id FROM poll WHERE substr(polled_at, 1, 10) = ?)",
    "DELETE FROM poll WHERE substr(polled_at, 1, 10) = ?",
    "DELETE FROM weather_snapshot WHERE substr(polled_at, 1, 10) = ?",
]


def _log(msg):
    print(msg, flush=True)


def eligible_days(conn, keep_days, today=None):
    today = today or datetime.now(timezone.utc).date()
    cutoff = (today - timedelta(days=keep_days)).isoformat()
    days = [
        row[0]
        for row in conn.execute(
            "SELECT DISTINCT substr(polled_at, 1, 10) FROM poll ORDER BY 1"
        )
    ]
    return [d for d in days if d <= cutoff and d < today.isoformat()]


def export_day(duck, conn, day, parquet_dir):
    """Export one day's rows to parquet and verify row counts. No deletion here."""
    for name, select in TABLES.items():
        want = conn.execute(COUNTS[name], (day,)).fetchone()[0]
        out_dir = parquet_dir / name
        out_dir.mkdir(parents=True, exist_ok=True)
        out_file = out_dir / f"{day}.parquet"

        if not out_file.exists():
            duck.execute(
                f"COPY ({select.format(day=day)}) TO '{out_file}' "
                "(FORMAT PARQUET, COMPRESSION ZSTD)"
            )
        have = duck.execute(
            "SELECT COUNT(*) FROM read_parquet(?)", [str(out_file)]
        ).fetchone()[0]
        if have != want:
            raise RuntimeError(
                f"{day}/{name}: parquet has {have} rows, sqlite has {want}; "
                "not deleting anything"
            )


def delete_day(conn, day):
    for stmt in DELETES:
        conn.execute(stmt, (day,))
    conn.commit()


def prune_raw(raw_dir, keep_days, today=None):
    today = today or datetime.now(timezone.utc).date()
    cutoff = (today - timedelta(days=keep_days)).isoformat()
    removed = 0
    for feed_dir in (raw_dir / "et", raw_dir / "sx"):
        if not feed_dir.is_dir():
            continue
        for day_dir in sorted(feed_dir.iterdir()):
            if day_dir.is_dir() and len(day_dir.name) == 10 and day_dir.name < cutoff:
                shutil.rmtree(day_dir)
                removed += 1
    return removed


def compact(db_path=DB_PATH, parquet_dir=PARQUET_DIR, keep_days=HOT_KEEP_DAYS,
            raw_dir=RAW_DIR, raw_keep_days=RAW_KEEP_DAYS, today=None):
    import duckdb

    conn = db.connect(db_path)
    days = eligible_days(conn, keep_days, today=today)
    if days:
        # Phase 1: export and verify everything with a read-only attach.
        duck = duckdb.connect()
        duck.execute("INSTALL sqlite; LOAD sqlite;")
        duck.execute(f"ATTACH '{db_path}' AS src (TYPE sqlite, READ_ONLY)")
        for day in days:
            export_day(duck, conn, day, parquet_dir)
        duck.close()
        # Phase 2: only after every export is verified, delete and reclaim space.
        for day in days:
            delete_day(conn, day)
            _log(f"compacted {day}")
        conn.execute("VACUUM")
        conn.commit()
    else:
        _log("no completed days old enough to compact")

    removed = prune_raw(raw_dir, raw_keep_days, today=today)
    if removed:
        _log(f"pruned {removed} raw day-directories older than {raw_keep_days} days")
    return days


def main(argv=None):
    parser = argparse.ArgumentParser(description="Tier the archive: SQLite -> Parquet")
    parser.add_argument("--keep-days", type=int, default=HOT_KEEP_DAYS,
                        help="completed days to keep in hot SQLite")
    parser.add_argument("--raw-keep-days", type=int, default=RAW_KEEP_DAYS,
                        help="days of raw XML to keep")
    args = parser.parse_args(argv)
    compact(keep_days=args.keep_days, raw_keep_days=args.raw_keep_days)
    return 0


if __name__ == "__main__":
    sys.exit(main())
