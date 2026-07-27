"""Rebuilding archive rows from the raw XML files.

The raw directory is the insurance policy: when a write to the database
fails, the response bytes are still on disk. These tests pin the contract
that reparsing them yields the same rows the collector would have written,
that it is safe to run twice, and that it never touches polls that already
exist.
"""

import gzip
import tempfile
import unittest
from pathlib import Path

from punktlig import db
from punktlig.reparse import reparse

FIXTURE = Path(__file__).parent / "fixtures" / "sample_et.xml"
DAY = "2026-07-25"


class ReparseTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.archive = base / "archive.db"
        self.raw = base / "raw"
        self.day_dir = self.raw / "et" / DAY
        self.day_dir.mkdir(parents=True)
        # An empty parquet dir: nothing is compacted unless a test says so.
        self.parquet = base / "parquet"

        # The mode filter runs off the line table, exactly as in collection.
        conn = db.connect(self.archive)
        conn.executemany(
            "INSERT INTO line (line_ref, mode) VALUES (?, ?)",
            [("RUT:Line:12", "tram"), ("RUT:Line:31", "bus")],
        )
        conn.commit()
        conn.close()

    def _write(self, name):
        (self.day_dir / name).write_bytes(gzip.compress(FIXTURE.read_bytes()))

    def _rows(self, sql):
        conn = db.connect(self.archive)
        self.addCleanup(conn.close)
        return conn.execute(sql).fetchall()

    def test_poll_and_calls_recovered_with_time_from_filename(self):
        self._write("100030_p1.xml.gz")
        stats = reparse(db_path=self.archive, raw_dir=self.raw, days=[DAY],
                        parquet_dir=self.parquet)
        self.assertEqual(stats["polls"], 1)

        polls = self._rows("SELECT polled_at, feed, pages, n_journeys FROM poll")
        self.assertEqual(len(polls), 1)
        self.assertEqual(polls[0][0], f"{DAY}T10:00:30+00:00")
        self.assertEqual(polls[0][1], "et")
        self.assertEqual(polls[0][2], 1)
        # Only the tram journey survives the mode filter; the bus is dropped.
        self.assertEqual(polls[0][3], 1)
        calls = self._rows("SELECT DISTINCT line_ref FROM call_snapshot")
        self.assertEqual(calls, [("RUT:Line:12",)])

    def test_rerun_adds_nothing(self):
        self._write("100030_p1.xml.gz")
        reparse(db_path=self.archive, raw_dir=self.raw, days=[DAY],
                        parquet_dir=self.parquet)
        before = self._rows("SELECT COUNT(*) FROM call_snapshot")[0][0]
        stats = reparse(db_path=self.archive, raw_dir=self.raw, days=[DAY],
                        parquet_dir=self.parquet)
        self.assertEqual(stats["polls"], 0)
        self.assertEqual(stats["skipped"], 1)
        self.assertEqual(self._rows("SELECT COUNT(*) FROM call_snapshot")[0][0], before)
        self.assertEqual(self._rows("SELECT COUNT(*) FROM poll")[0][0], 1)

    def test_pages_of_one_poll_become_one_poll(self):
        # Each page is saved with its own timestamp; a new poll starts at _p1.
        self._write("100030_p1.xml.gz")
        self._write("100032_p2.xml.gz")
        self._write("100130_p1.xml.gz")
        stats = reparse(db_path=self.archive, raw_dir=self.raw, days=[DAY],
                        parquet_dir=self.parquet)
        self.assertEqual(stats["polls"], 2)
        polls = self._rows("SELECT polled_at, pages FROM poll ORDER BY polled_at")
        self.assertEqual(polls[0], (f"{DAY}T10:00:30+00:00", 2))
        self.assertEqual(polls[1], (f"{DAY}T10:01:30+00:00", 1))

    def test_existing_live_poll_is_left_alone(self):
        # A raw file is stamped when the response arrived, seconds after the
        # poll it belongs to started. A live poll close enough in time means
        # the collector was writing then, so the file is already represented.
        conn = db.connect(self.archive)
        db.insert_poll(conn, polled_at=f"{DAY}T10:00:25.123456+00:00", feed="et", dataset="RUT")
        conn.close()
        self._write("100030_p1.xml.gz")
        stats = reparse(db_path=self.archive, raw_dir=self.raw, days=[DAY],
                        parquet_dir=self.parquet)
        self.assertEqual(stats["polls"], 0)
        self.assertEqual(self._rows("SELECT COUNT(*) FROM poll")[0][0], 1)

    def test_compacted_day_is_refused(self):
        # Once a day lives in parquet its rows are gone from SQLite, so
        # reparsing it there would duplicate the day across both tiers.
        self._write("100030_p1.xml.gz")
        parquet = self.parquet / "polls"
        parquet.mkdir(parents=True)
        (parquet / f"{DAY}.parquet").write_bytes(b"")
        stats = reparse(
            db_path=self.archive, raw_dir=self.raw, days=[DAY],
            parquet_dir=self.parquet,
        )
        self.assertEqual(stats["polls"], 0)
        self.assertEqual(stats["refused"], [DAY])
        self.assertEqual(self._rows("SELECT COUNT(*) FROM poll")[0][0], 0)


if __name__ == "__main__":
    unittest.main()

