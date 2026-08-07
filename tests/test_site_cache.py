"""The route geometry is expensive and slow to change, so it is cached.

Measured on the live archive, one export took 2 257 seconds against a ten
minute schedule, and 545 of those were spent redrawing the same lines. The
cache is what stops the export from outgrowing its own timetable; these tests
pin the three things it has to get right.
"""

import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from punktlig import site


class NetworkCacheTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cache = Path(self.tmp.name) / "cache" / "network.json"
        self.built = 0

        def fake_build(conn, db_path=None, recent_polls=600):
            self.built += 1
            return {"stops": [[10.75, 59.91, "Jernbanetorget"]],
                    "routes": [{"line": "5", "mode": "metro", "path": [0]}]}

        self.real_build = site.build_network
        site.build_network = fake_build
        self.addCleanup(lambda: setattr(site, "build_network", self.real_build))

    def network(self, max_age=3600):
        return site.network(None, cache=self.cache, max_age=max_age)

    def test_builds_once_then_reads_the_cache(self):
        first = self.network()
        second = self.network()
        self.assertEqual(first, second)
        self.assertEqual(self.built, 1, "the second call should not have rebuilt")
        self.assertTrue(self.cache.exists())

    def test_rebuilds_once_the_cache_is_too_old(self):
        self.network()
        old = time.time() - 10_000
        os.utime(self.cache, (old, old))
        self.network(max_age=3600)
        self.assertEqual(self.built, 2)

    def test_a_damaged_cache_is_rebuilt_rather_than_raised(self):
        """A half written file must cost time, not an export."""
        self.network()
        self.cache.write_text("{not json", encoding="utf-8")
        result = self.network()
        self.assertEqual(self.built, 2)
        self.assertIn("routes", result)
        # and the damaged file is replaced, so it costs one run and not every run
        self.assertEqual(json.loads(self.cache.read_text(encoding="utf-8")), result)

    def test_no_cache_path_means_always_fresh(self):
        site.network(None, cache=None)
        site.network(None, cache=None)
        self.assertEqual(self.built, 2)


if __name__ == "__main__":
    unittest.main()
