"""Two DuckDB jobs must never run at once, and neither may deadlock.

The failure this prevents is not an exception: it is an access violation
inside the DuckDB extension that kills the process outright, so the test has
to prove exclusion rather than merely observe that nothing was raised.
"""

import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

from punktlig.joblock import heavy, lock_path


def hold_in_another_process(data_dir, seconds, name="duckdb.lock"):
    """Start a process that takes the lock and keeps it for a while."""
    code = textwrap.dedent(f"""
        import sys, time
        sys.path.insert(0, {str(Path(__file__).resolve().parents[1])!r})
        from punktlig.joblock import heavy
        with heavy("other", data_dir={str(data_dir)!r}, name={name!r},
                   log=lambda *a: None):
            print("holding", flush=True)
            time.sleep({seconds})
    """)
    proc = subprocess.Popen([sys.executable, "-c", code],
                            stdout=subprocess.PIPE, text=True)
    proc.stdout.readline()  # wait until the lock is actually held
    return proc


def stop(proc, data_dir, name="duckdb.lock"):
    """Kill the holder and close the pipe. The lock file is removed by the
    per-test cleanup, which retries until Windows lets go of the handle."""
    proc.kill()
    proc.wait(timeout=10)
    if proc.stdout:
        proc.stdout.close()


class JobLockTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        # Runs before the directory cleanup, since cleanups are last-in
        # first-out. Windows keeps a lock file handle for a moment after the
        # owner releases it, which is long enough to fail the removal.
        self.addCleanup(self._wait_for_locks)
        self.dir = Path(self.tmp.name)

    def _wait_for_locks(self):
        # Deleting is the real test. A lock file can be opened for reading
        # while another process still holds it, because python shares the
        # handle; only removal actually requires every handle to be closed,
        # which is exactly what the directory cleanup will need.
        for name in ("duckdb.lock", "collector.lock", "fitting.lock"):
            path = lock_path(self.dir, name)
            for _ in range(100):
                try:
                    path.unlink(missing_ok=True)
                    break
                except PermissionError:
                    time.sleep(0.05)

    def test_an_uncontended_lock_is_granted(self):
        with heavy("a", data_dir=self.dir, log=lambda *a: None) as got:
            self.assertTrue(got)

    def test_the_lock_is_reusable_after_release(self):
        for _ in range(3):
            with heavy("a", data_dir=self.dir, log=lambda *a: None) as got:
                self.assertTrue(got)

    def test_a_second_holder_is_refused_rather_than_blocked(self):
        proc = hold_in_another_process(self.dir, 10)
        self.addCleanup(stop, proc, self.dir)
        with heavy("site", wait=False, data_dir=self.dir, log=lambda *a: None) as got:
            self.assertFalse(got)

    def test_the_lock_survives_the_holder_being_killed(self):
        stop(hold_in_another_process(self.dir, 60), self.dir)
        # The operating system drops the lock with the process, so no stale
        # owner can wedge the next run. It does so promptly rather than
        # instantly: Windows keeps the handle for a moment after the kill,
        # which is why the collector's scheduler retries rather than giving
        # up on the first refusal.
        for _ in range(100):
            with heavy("after", wait=False, data_dir=self.dir,
                       log=lambda *a: None) as got:
                if got:
                    return
            time.sleep(0.05)
        self.fail("lock was never released after the holder was killed")

    def test_the_lock_file_lives_in_the_data_directory(self):
        with heavy("a", data_dir=self.dir, log=lambda *a: None):
            self.assertTrue(lock_path(self.dir).exists())

    def test_differently_named_locks_do_not_contend(self):
        # The collector and the replay guard different things and must never
        # wait on each other.
        with heavy("replay", data_dir=self.dir, log=lambda *a: None) as first:
            with heavy("collector", wait=False, data_dir=self.dir,
                       name="collector.lock", log=lambda *a: None) as second:
                self.assertTrue(first)
                self.assertTrue(second)

    def test_fitting_does_not_block_the_export(self):
        # A three-hour training run held the DuckDB lock and stopped the site
        # publishing for three hours, for no reason: fitting reads plain
        # SQLite and never touches DuckDB.
        from punktlig.joblock import FITTING_LOCK

        proc = hold_in_another_process(self.dir, 10, name=FITTING_LOCK)
        self.addCleanup(stop, proc, self.dir, FITTING_LOCK)
        with heavy("site", wait=False, data_dir=self.dir,
                   log=lambda *a: None) as got:
            self.assertTrue(got)

    def test_a_named_lock_still_excludes_its_own_kind(self):
        proc = hold_in_another_process(self.dir, 10, name="collector.lock")
        self.addCleanup(stop, proc, self.dir, "collector.lock")
        with heavy("collector", wait=False, data_dir=self.dir,
                   name="collector.lock", log=lambda *a: None) as got:
            self.assertFalse(got)


if __name__ == "__main__":
    unittest.main()
