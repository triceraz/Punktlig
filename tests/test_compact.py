"""Storage tiering tests: export to parquet, verify, delete, and read back.

Seeds the standard three-poll scenario twice: once on an old day (eligible
for compaction) and once on the current day (must stay in hot SQLite). After
compaction the replay must produce identical rows from the mixed sources.
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

from punktlig import db
from punktlig.dataset import build
from test_dataset import seed_archive

TODAY = datetime.now(timezone.utc).date()
OLD_DAY = (TODAY - timedelta(days=5)).isoformat()


@unittest.skipUnless(HAVE_DUCKDB, "duckdb not installed (analysis extra)")
class CompactTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.archive = base / "archive.db"
        self.parquet = base / "parquet"
        self.raw = base / "raw"
        self.old_poll_ids = seed_archive(self.archive, day=OLD_DAY)
        self.new_poll_ids = seed_archive(self.archive, day=TODAY.isoformat())

        # Raw dirs: one ancient (should be pruned), one current (should stay).
        (self.raw / "et" / "2000-01-01").mkdir(parents=True)
        (self.raw / "et" / "2000-01-01" / "x.xml.gz").write_bytes(b"old")
        (self.raw / "et" / TODAY.isoformat()).mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def _compact(self):
        from punktlig.compact import compact

        return compact(
            db_path=self.archive, parquet_dir=self.parquet, keep_days=1,
            raw_dir=self.raw, raw_keep_days=30,
        )

    def test_old_day_moves_new_day_stays(self):
        done = self._compact()
        self.assertEqual(done, [OLD_DAY])

        for name, expected in (("calls", 10), ("polls", 3), ("weather", 3)):
            file = self.parquet / name / f"{OLD_DAY}.parquet"
            self.assertTrue(file.exists(), name)
            count = duckdb.execute(
                "SELECT COUNT(*) FROM read_parquet(?)", [str(file)]
            ).fetchone()[0]
            self.assertEqual(count, expected, name)

        conn = db.connect(self.archive)
        remaining_days = [
            r[0] for r in conn.execute("SELECT DISTINCT substr(polled_at, 1, 10) FROM poll")
        ]
        self.assertEqual(remaining_days, [TODAY.isoformat()])
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM call_snapshot").fetchone()[0], 10
        )

    def test_raw_pruning(self):
        self._compact()
        self.assertFalse((self.raw / "et" / "2000-01-01").exists())
        self.assertTrue((self.raw / "et" / TODAY.isoformat()).exists())

    def test_rerun_is_a_noop(self):
        self._compact()
        self.assertEqual(self._compact(), [])

    def test_replay_reads_parquet_and_sqlite_identically(self):
        # Replay before tiering, on hot SQLite only.
        before = build(
            archive_path=self.archive, out_path=Path(self.tmp.name) / "before.db",
            parquet_dir=Path(self.tmp.name) / "no-parquet",
        )
        self._compact()
        # Replay after tiering: old day now lives in parquet, new day in SQLite.
        after_path = Path(self.tmp.name) / "after.db"
        after = build(
            archive_path=self.archive, out_path=after_path, parquet_dir=self.parquet
        )
        self.assertEqual(before, 6)  # 3 rows per seeded day
        self.assertEqual(after, 6)

        conn = db.connect(after_path)
        by_day = dict(
            conn.execute(
                "SELECT operating_date, COUNT(*) FROM training_row GROUP BY 1"
            )
        )
        self.assertEqual(by_day, {OLD_DAY: 3, TODAY.isoformat(): 3})
        # Same no-lookahead weather behaviour through the parquet path.
        temps = sorted(
            r[0] for r in conn.execute(
                "SELECT fc_air_temp FROM training_row WHERE operating_date = ?",
                (OLD_DAY,),
            )
        )
        self.assertEqual(temps, [12.0, 12.0, 99.0])


if __name__ == "__main__":
    unittest.main()
