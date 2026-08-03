"""The write-ahead log has to be folded back in, or it eats the archive.

SQLite cannot checkpoint while a reader holds an older snapshot, and the site
export reads the archive every ten minutes. Nothing ever asked, so the log
reached two gigabytes; opening the archive then took longer than the
collector's busy timeout, and it died on its first statement.
"""

import sqlite3
import tempfile
import unittest
from pathlib import Path

from punktlig import collect, db


class CheckpointTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "archive.db"
        self.conn = db.connect(self.path)
        self.addCleanup(self.conn.close)

    def wal_size(self):
        wal = Path(str(self.path) + "-wal")
        return wal.stat().st_size if wal.exists() else 0

    def fill(self, n=400):
        for i in range(n):
            db.insert_poll(self.conn, polled_at=f"2026-07-28T06:{i % 60:02d}:00+00:00",
                           feed="et", dataset="RUT")
        self.conn.commit()

    def test_it_shrinks_the_log(self):
        self.fill()
        self.assertGreater(self.wal_size(), 0)
        collect.checkpoint(self.conn)
        self.assertEqual(self.wal_size(), 0)

    def test_the_rows_are_still_there_afterwards(self):
        self.fill()
        before = self.conn.execute("SELECT COUNT(*) FROM poll").fetchone()[0]
        collect.checkpoint(self.conn)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM poll").fetchone()[0], before)

    def test_a_reader_holding_a_snapshot_does_not_raise(self):
        self.fill()
        reader = sqlite3.connect(self.path)
        self.addCleanup(reader.close)
        reader.execute("BEGIN")
        reader.execute("SELECT COUNT(*) FROM poll").fetchone()
        # Blocked is a normal outcome, not an error: the next attempt comes
        # round soon enough, and the collector must not fall over meanwhile.
        collect.checkpoint(self.conn)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM poll").fetchone()[0], 400)

    def test_a_broken_connection_is_survivable(self):
        self.conn.close()
        collect.checkpoint(self.conn)  # must not raise

    def test_the_loop_asks_on_schedule(self):
        asked = []
        calls = {"n": 0}

        def poll(conn, save_raw=True):
            calls["n"] += 1
            if calls["n"] > collect.CHECKPOINT_EVERY:
                raise KeyboardInterrupt
            return {"journeys": 0, "calls": 0, "dropped": 0, "pages": 1, "ms": 1}

        original = collect.checkpoint
        collect.checkpoint = lambda conn: asked.append(calls["n"])
        self.addCleanup(setattr, collect, "checkpoint", original)
        with self.assertRaises(KeyboardInterrupt):
            collect.run(self.conn, interval=0, save_raw=False,
                        sleep=lambda s: None, poll=poll)
        self.assertEqual(asked, [collect.CHECKPOINT_EVERY])


if __name__ == "__main__":
    unittest.main()
