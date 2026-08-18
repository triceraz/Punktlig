"""A day must be read from exactly one tier, whichever state compaction left.

Compaction deletes a day from hot SQLite only after its parquet export is
verified, but the deletion can fail on its own, and did: 2026-08-14 sat in
both tiers and the replay's UNION ALL read every one of its rows twice. The
dataset carried them at a measured ratio of 1.999, and the previous dataset
shows the same signature for its July days. These tests pin the guard, and
the bounded deletion that stops the overlap from arising at all.
"""

import tempfile
import unittest
from datetime import timezone
from pathlib import Path

from punktlig import db
from punktlig.compact import delete_day
from punktlig.dataset import _hot_exclusion, _open_sources

DAY_BOTH = "2026-08-14"   # exported and, after a failed delete, still hot
DAY_HOT = "2026-08-16"    # only in the hot archive


def seed(path, days_calls):
    """A tiny archive: one poll per day, `n` calls each."""
    conn = db.connect(path)
    for day, n in days_calls.items():
        poll_id = db.insert_poll(conn, polled_at=f"{day}T10:00:00+00:00",
                                 feed="et", dataset="RUT")
        for i in range(n):
            conn.execute(
                "INSERT INTO call_snapshot (poll_id, journey_ref, operating_date,"
                " line_ref, call_type, stop_ref, order_no, aimed_arr, actual_arr)"
                " VALUES (?, ?, ?, 'RUT:Line:1', 'recorded', ?, ?, ?, ?)",
                (poll_id, f"j{i}", day, f"NSR:Quay:{i}", i + 1,
                 f"{day}T10:0{i % 6}:00+00:00", f"{day}T10:0{i % 6}:30+00:00"),
            )
        conn.execute(
            "INSERT INTO weather_snapshot (polled_at, forecast_time, air_temp,"
            " precip_mm, wind_mps) VALUES (?, ?, 1.0, 0.0, 2.0)",
            (f"{day}T10:00:00+00:00", f"{day}T11:00:00+00:00"),
        )
    conn.commit()
    return conn


def export_to_parquet(archive, parquet_dir, day):
    """The same COPY the real compaction does, for one day of calls."""
    import duckdb

    out = Path(parquet_dir) / "calls"
    out.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    try:
        con.execute("INSTALL sqlite; LOAD sqlite;")
        con.execute(f"ATTACH '{archive}' AS src (TYPE sqlite, READ_ONLY)")
        con.execute(
            "COPY (SELECT c.journey_ref, c.operating_date, c.poll_id, p.polled_at,"
            " c.line_ref, c.direction, c.call_type, c.stop_ref, c.stop_name,"
            " c.order_no, c.aimed_arr, c.expected_arr, c.actual_arr, c.aimed_dep,"
            " c.expected_dep, c.actual_dep, c.cancelled, c.call_cancelled,"
            " c.recorded_at FROM src.call_snapshot c"
            " JOIN src.poll p ON p.poll_id = c.poll_id"
            f" WHERE substr(p.polled_at, 1, 10) = '{day}')"
            f" TO '{out / f'{day}.parquet'}' (FORMAT PARQUET)"
        )
    finally:
        con.close()


class TwoTierTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.archive = Path(self.tmp.name) / "archive.db"
        self.parquet = Path(self.tmp.name) / "parquet"
        conn = seed(self.archive, {DAY_BOTH: 4, DAY_HOT: 3})
        conn.close()
        export_to_parquet(self.archive, self.parquet, DAY_BOTH)

    def read_days(self):
        _, calls, close = _open_sources(str(self.archive), self.parquet)
        days = [row[3][:10] for row in calls]  # polled_at is column 3
        close()
        return days

    def test_a_day_in_both_tiers_is_read_once(self):
        days = self.read_days()
        self.assertEqual(days.count(DAY_BOTH), 4,
                         "the verified parquet copy, and only it")
        self.assertEqual(days.count(DAY_HOT), 3, "hot-only days are untouched")

    def test_exclusion_is_empty_without_parquet(self):
        self.assertEqual(_hot_exclusion([]), "")

    def test_recovery_suffix_files_map_to_their_day(self):
        # 2026-07-25a.parquet exists in the real archive; its day is 2026-07-25.
        sql = _hot_exclusion([r"D:\x\2026-07-25a.parquet"])
        self.assertIn("'2026-07-25'", sql)

    def test_batched_delete_removes_the_day_and_only_it(self):
        import punktlig.compact as compact_mod

        old = compact_mod.DELETE_BATCH
        compact_mod.DELETE_BATCH = 2  # force several batches over 4 rows
        try:
            conn = db.connect(self.archive)
            delete_day(conn, DAY_BOTH)
            left = [r[0] for r in conn.execute(
                "SELECT DISTINCT substr(p.polled_at, 1, 10) FROM call_snapshot c"
                " JOIN poll p ON p.poll_id = c.poll_id")]
            polls = [r[0] for r in conn.execute(
                "SELECT DISTINCT substr(polled_at, 1, 10) FROM poll")]
            conn.close()
        finally:
            compact_mod.DELETE_BATCH = old
        self.assertEqual(left, [DAY_HOT])
        self.assertEqual(polls, [DAY_HOT])


if __name__ == "__main__":
    unittest.main()
