"""The replay must read a still archive even while the collector writes.

DuckDB's SQLite reader opens several connections to scan in parallel and
they do not share one read transaction, so reading the live database during
an hour-long replay can tear a row across a page rewrite. That surfaced as
invalid unicode deep into a build, on data that read perfectly when the same
sources were scanned one at a time.
"""

import sqlite3
import tempfile
import unittest
from pathlib import Path

from punktlig import db
from punktlig.dataset import frozen_archive
from test_dataset import D, seed_archive


class FrozenArchiveTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.archive = Path(self.tmp.name) / "archive.db"
        seed_archive(self.archive)

    def _calls(self, path):
        conn = db.connect(path)
        try:
            return conn.execute("SELECT COUNT(*) FROM call_snapshot").fetchone()[0]
        finally:
            conn.close()

    def test_copy_holds_the_same_rows(self):
        with frozen_archive(self.archive) as still:
            self.assertNotEqual(str(still), str(self.archive))
            self.assertEqual(self._calls(still), self._calls(self.archive))

    def test_writes_during_the_replay_do_not_reach_the_copy(self):
        before = self._calls(self.archive)
        with frozen_archive(self.archive) as still:
            writer = db.connect(self.archive)
            try:
                poll_id = db.insert_poll(writer, polled_at=f"{D}T11:00:00+00:00",
                                         feed="et", dataset="RUT")
                db.insert_calls(writer, [{
                    "poll_id": poll_id, "journey_ref": "RUT:Journey:zzz",
                    "operating_date": D, "line_ref": "RUT:Line:12", "direction": "1",
                    "call_type": "expected", "stop_ref": "NSR:Quay:1",
                    "stop_name": "Sent", "order_no": 1,
                    "aimed_arr": f"{D}T11:05:00+00:00", "cancelled": 0,
                }])
                writer.commit()
            finally:
                writer.close()

            self.assertEqual(self._calls(still), before)
            self.assertGreater(self._calls(self.archive), before)

    def test_the_copy_is_removed_afterwards(self):
        with frozen_archive(self.archive) as still:
            path = Path(still)
            self.assertTrue(path.exists())
        self.assertFalse(path.exists())

    def test_the_copy_is_removed_even_when_the_replay_fails(self):
        path = None
        with self.assertRaises(sqlite3.OperationalError):
            with frozen_archive(self.archive) as still:
                path = Path(still)
                raise sqlite3.OperationalError("replay blew up")
        self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()
