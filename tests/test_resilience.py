"""Failure handling in the collector, the HTTP layer and compaction.

These pin the lessons from the 2026-07-27 outage: a poisoned database
connection stalled writes for hours while the process looked healthy, and
a stalled HTTP response hung one poll for 17 minutes.
"""

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from punktlig import collect, db, net
from punktlig.compact import vacuum


class ReconnectTest(unittest.TestCase):
    """A failing database must be reconnected, then given up on."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "archive.db"
        self.conn = db.connect(self.path)
        self.addCleanup(self.conn.close)
        self.reconnects = 0

    def _connect(self):
        self.reconnects += 1
        conn = db.connect(self.path)
        self.addCleanup(conn.close)
        return conn

    def test_gives_up_after_repeated_failures_so_the_task_can_restart(self):
        def always_fails(conn, save_raw=True):
            raise sqlite3.OperationalError("database is locked")

        code = collect.run(
            self.conn, interval=0, sleep=lambda _: None,
            connect=self._connect, poll=always_fails,
        )
        self.assertEqual(code, 1)
        # Reconnect is attempted before giving up, and the exit is bounded.
        self.assertGreaterEqual(self.reconnects, 1)
        self.assertLessEqual(self.reconnects, collect.GIVE_UP_AFTER)

    def test_reconnect_recovers_without_giving_up(self):
        # Fail exactly often enough to trigger one reconnect, then succeed.
        # The loop is stopped from the sleep hook, which runs outside the
        # error handling and so cannot be mistaken for another failure.
        class Stop(Exception):
            pass

        polls, sleeps = {"n": 0}, {"n": 0}

        def fails_then_works(conn, save_raw=True):
            polls["n"] += 1
            if polls["n"] <= collect.RECONNECT_AFTER:
                raise sqlite3.OperationalError("database is locked")
            return {"journeys": 1, "calls": 2, "dropped": 0, "pages": 1, "ms": 5}

        def sleeper(_):
            sleeps["n"] += 1
            if sleeps["n"] >= collect.RECONNECT_AFTER + 1:
                raise Stop

        with self.assertRaises(Stop):
            collect.run(
                self.conn, interval=0, sleep=sleeper,
                connect=self._connect, poll=fails_then_works,
            )
        # One reconnect, and the run never reached the give-up threshold.
        self.assertEqual(self.reconnects, 1)
        self.assertEqual(polls["n"], collect.RECONNECT_AFTER + 1)


class HttpDeadlineTest(unittest.TestCase):
    """A response that trickles forever must not hang the poll."""

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self, size=None):
            return b"x" * 16

    def test_slow_response_hits_the_total_deadline(self):
        clock = iter([0.0, 1.0, 2.0, 999.0])
        with mock.patch.object(net.urllib.request, "urlopen", return_value=self._Resp()), \
             mock.patch.object(net.time, "monotonic", lambda: next(clock)):
            with self.assertRaises(TimeoutError):
                net.get("https://example.invalid/feed", deadline=60)


class VacuumTest(unittest.TestCase):
    """Compaction must never fail, or block, on a busy database."""

    class _LockedConn:
        def execute(self, sql):
            raise sqlite3.OperationalError("database is locked")

    def test_vacuum_is_skipped_when_the_database_is_busy(self):
        self.assertFalse(vacuum(self._LockedConn()))

    def test_vacuum_runs_when_free(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        conn = db.connect(Path(tmp.name) / "a.db")
        self.addCleanup(conn.close)
        self.assertTrue(vacuum(conn))


class BusyTimeoutTest(unittest.TestCase):
    def test_connections_wait_instead_of_failing_instantly(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        conn = db.connect(Path(tmp.name) / "a.db")
        self.addCleanup(conn.close)
        self.assertEqual(
            conn.execute("PRAGMA busy_timeout").fetchone()[0], db.BUSY_TIMEOUT_MS
        )


if __name__ == "__main__":
    unittest.main()
