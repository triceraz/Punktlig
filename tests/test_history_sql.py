"""The history features, computed by the database instead of by dictionaries.

The Python index keeps one entry per entity per bucket, which costs memory
per stop and per segment and per bucket at once. These tests pin that the
SQL version answers exactly the same questions, using the same synthetic
archive as the replay tests, so the rewrite can be checked rather than
trusted.
"""

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    import duckdb  # noqa: F401
    HAVE_DUCKDB = True
except ImportError:
    HAVE_DUCKDB = False

from punktlig.dataset import HistoryIndex, _open_sources
from punktlig.history_sql import SqlHistory
from test_dataset import D, seed_archive
from test_dataset import seed_second_journey


def at(hhmmss):
    return datetime.fromisoformat(f"{D}T{hhmmss}+00:00")


@unittest.skipUnless(HAVE_DUCKDB, "duckdb not installed (analysis extra)")
class SqlHistoryMatchesPythonTest(unittest.TestCase):
    BUCKET = 60
    WINDOW = timedelta(minutes=30)

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.archive = Path(cls.tmp.name) / "archive.db"
        cls.parquet = Path(cls.tmp.name) / "none"
        seed_archive(cls.archive)
        seed_second_journey(cls.archive)

        _, rows, close = _open_sources(cls.archive, cls.parquet)
        cls.python = HistoryIndex(rows, bucket_seconds=cls.BUCKET)
        close()
        cls.sql = SqlHistory(cls.archive, cls.parquet, bucket_seconds=cls.BUCKET)

    @classmethod
    def tearDownClass(cls):
        cls.sql.close()
        cls.tmp.cleanup()

    def test_segment_runtime_matches(self):
        # The 1->2 segment on line 12 has three prior runtimes by 10:10Z.
        for moment in ("10:05:00", "10:09:00", "10:10:00", "10:12:00"):
            with self.subTest(moment=moment):
                self.assertEqual(
                    self.sql.typical(at(moment), "RUT:Line:12", "1",
                                     "NSR:Quay:1", "NSR:Quay:2"),
                    self.python.typical(at(moment), "RUT:Line:12", "1",
                                        "NSR:Quay:1", "NSR:Quay:2"),
                )

    def test_unknown_segment_is_none_in_both(self):
        self.assertIsNone(
            self.sql.typical(at("10:10:00"), "RUT:Line:99", "1", "a", "b")
        )

    def test_stop_delay_level_matches(self):
        for moment in ("10:02:00", "10:06:00", "10:10:00", "10:13:00"):
            for stop in ("NSR:Quay:1", "NSR:Quay:2", "NSR:Quay:3"):
                with self.subTest(moment=moment, stop=stop):
                    self.assertEqual(
                        self.sql.stop_recent(at(moment), stop, self.WINDOW),
                        self.python.stop_recent(at(moment), stop, self.WINDOW),
                    )

    def test_line_delay_level_matches(self):
        for moment in ("10:02:00", "10:06:00", "10:10:00", "10:13:00"):
            with self.subTest(moment=moment):
                self.assertEqual(
                    self.sql.line_recent(at(moment), "RUT:Line:12", "1", self.WINDOW),
                    self.python.line_recent(at(moment), "RUT:Line:12", "1", self.WINDOW),
                )

    def test_a_still_open_bucket_is_invisible_to_both(self):
        # The pass at 10:00:30 sits in the 10:00 bucket, which is open at
        # 10:00:30 and closed by 10:02.
        self.assertIsNone(self.python.line_recent(at("10:00:30"), "RUT:Line:12", "1", self.WINDOW))
        self.assertIsNone(self.sql.line_recent(at("10:00:30"), "RUT:Line:12", "1", self.WINDOW))


if __name__ == "__main__":
    unittest.main()
