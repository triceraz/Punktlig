"""The served history aggregate is cached; the trained one never is.

Rebuilding it per export cost 1 672 seconds of a 2 257 second run against a
ten minute schedule. These tests pin what the cache may and may not do: it
must not be used unless asked for, and it must refuse a file it cannot trust
rather than serve the wrong aggregate quietly.
"""

import os
import pickle
import tempfile
import time
import unittest
from array import array
from pathlib import Path

from punktlig import history_sql
from punktlig.history_sql import CACHE_FORMAT, SqlHistory


def fake_aggregate(self, archive_path, parquet_dir, bucket_seconds, memory_limit):
    """Stand in for the whole-archive scan, and count how often it ran."""
    history_sql._built = getattr(history_sql, "_built", 0) + 1
    packed = (array("q", [0]), array("q", [0, 1]), array("d", [0.0, 12.0]))
    self.segments = {("L1", "1", "A", "B"): packed}
    self.stop_delays = {"A": packed}
    self.line_delays = {("L1", "1"): packed}


class HistoryCacheTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cache = Path(self.tmp.name) / "cache" / "history.pickle"
        history_sql._built = 0
        self.real = SqlHistory._aggregate
        SqlHistory._aggregate = fake_aggregate
        self.addCleanup(lambda: setattr(SqlHistory, "_aggregate", self.real))

    def build(self, cache=None, max_age=3600, bucket_seconds=300):
        return SqlHistory(archive_path="ignored", parquet_dir=None,
                          bucket_seconds=bucket_seconds, cache=cache,
                          max_age=max_age)

    def test_without_a_cache_path_it_always_aggregates(self):
        """The replay must never be handed yesterday's archive."""
        self.build()
        self.build()
        self.assertEqual(history_sql._built, 2)

    def test_with_a_cache_path_it_aggregates_once(self):
        first = self.build(cache=self.cache)
        second = self.build(cache=self.cache)
        self.assertEqual(history_sql._built, 1)
        self.assertEqual(first.stop_delays, second.stop_delays)
        self.assertEqual(first.segments, second.segments)

    def test_a_stale_cache_is_rebuilt(self):
        self.build(cache=self.cache)
        old = time.time() - 10_000
        os.utime(self.cache, (old, old))
        self.build(cache=self.cache, max_age=3600)
        self.assertEqual(history_sql._built, 2)

    def test_a_cache_from_another_bucket_size_is_refused(self):
        """Buckets are the unit of the as-of rule; mixing them is wrong, not slow."""
        self.build(cache=self.cache, bucket_seconds=300)
        self.build(cache=self.cache, bucket_seconds=600)
        self.assertEqual(history_sql._built, 2)

    def test_a_cache_from_another_format_is_refused(self):
        self.build(cache=self.cache)
        blob = pickle.loads(self.cache.read_bytes())
        blob["format"] = CACHE_FORMAT + 1
        self.cache.write_bytes(pickle.dumps(blob))
        self.build(cache=self.cache)
        self.assertEqual(history_sql._built, 2)

    def test_a_damaged_cache_is_rebuilt_rather_than_raised(self):
        self.build(cache=self.cache)
        self.cache.write_bytes(b"not a pickle")
        result = self.build(cache=self.cache)
        self.assertEqual(history_sql._built, 2)
        self.assertIn("A", result.stop_delays)


if __name__ == "__main__":
    unittest.main()
