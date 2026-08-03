"""Opening the archive must survive another reader holding it.

Setting the journal mode needs a brief exclusive moment and is refused
outright while another connection is reading. That is not a rare corner: the
site export scans the archive every ten minutes, and the collector opens it
on every restart. The two met, and the collector died on its first line.
"""

import sqlite3
import tempfile
import unittest
from pathlib import Path

from punktlig import db


class ConnectTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "archive.db"

    def test_a_fresh_database_ends_up_in_wal(self):
        conn = db.connect(self.path)
        self.addCleanup(conn.close)
        self.assertEqual(
            conn.execute("PRAGMA journal_mode").fetchone()[0].lower(), "wal")

    def test_opening_again_while_a_reader_holds_it_still_works(self):
        first = db.connect(self.path)
        self.addCleanup(first.close)
        db.insert_poll(first, polled_at="2026-07-28T06:00:00+00:00",
                       feed="et", dataset="RUT")
        first.commit()

        # An open read transaction is exactly what the export holds.
        reader = sqlite3.connect(self.path)
        self.addCleanup(reader.close)
        reader.execute("BEGIN")
        reader.execute("SELECT COUNT(*) FROM poll").fetchone()

        second = db.connect(self.path)
        self.addCleanup(second.close)
        self.assertEqual(
            second.execute("SELECT COUNT(*) FROM poll").fetchone()[0], 1)

    def test_the_busy_timeout_is_set_before_anything_can_block(self):
        conn = db.connect(self.path)
        self.addCleanup(conn.close)
        self.assertEqual(
            conn.execute("PRAGMA busy_timeout").fetchone()[0], db.BUSY_TIMEOUT_MS)


if __name__ == "__main__":
    unittest.main()
