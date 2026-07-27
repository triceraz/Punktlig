"""The check that notices when collection has quietly stopped.

The 2026-07-27 outage looked healthy from the outside: the task said
Running and the process existed, while nothing had been written for hours.
Health therefore has to be measured on the archive, not on the process.
"""

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from punktlig import db
from punktlig.health import check


def utc(minutes_ago):
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()


class HealthTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "archive.db"

    def _seed(self, polls):
        conn = db.connect(self.path)
        for minutes_ago, error in polls:
            db.insert_poll(conn, polled_at=utc(minutes_ago), feed="et",
                           dataset="RUT", error=error, n_calls=100)
        conn.close()

    def test_fresh_polls_are_healthy(self):
        self._seed([(m, None) for m in range(0, 20)])
        result = check(self.path)
        self.assertTrue(result["ok"])
        self.assertLess(result["age_minutes"], 5)

    def test_a_stale_archive_is_not_healthy(self):
        self._seed([(m, None) for m in range(120, 140)])
        result = check(self.path)
        self.assertFalse(result["ok"])
        self.assertIn("no new polls", " ".join(result["problems"]).lower())

    def test_polls_that_only_record_errors_are_not_healthy(self):
        # Exactly the outage signature: the loop keeps running and keeps
        # logging, but every poll fails, so nothing lands in the archive.
        self._seed([(m, "database is locked") for m in range(0, 20)])
        result = check(self.path)
        self.assertFalse(result["ok"])
        self.assertIn("failed", " ".join(result["problems"]).lower())

    def test_an_empty_archive_is_not_healthy(self):
        db.connect(self.path).close()
        result = check(self.path)
        self.assertFalse(result["ok"])

    def test_a_few_errors_among_good_polls_are_tolerated(self):
        polls = [(m, None) for m in range(0, 20)]
        polls[5] = (5, "IncompleteRead")
        self._seed(polls)
        self.assertTrue(check(self.path)["ok"])


if __name__ == "__main__":
    unittest.main()
