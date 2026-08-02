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


def hold_in_another_process(data_dir, seconds):
    """Start a process that takes the lock and keeps it for a while."""
    code = textwrap.dedent(f"""
        import sys, time
        sys.path.insert(0, {str(Path(__file__).resolve().parents[1])!r})
        from punktlig.joblock import heavy
        with heavy("other", data_dir={str(data_dir)!r}, log=lambda *a: None):
            print("holding", flush=True)
            time.sleep({seconds})
    """)
    proc = subprocess.Popen([sys.executable, "-c", code],
                            stdout=subprocess.PIPE, text=True)
    proc.stdout.readline()  # wait until the lock is actually held
    return proc


def stop(proc, data_dir):
    """Kill the holder and wait until its handle is really gone.

    Killing a process releases its lock immediately, but Windows keeps the
    file handle for a moment afterwards, which is long enough to fail the
    temporary-directory cleanup and turn a passing test into an error.
    """
    proc.kill()
    proc.wait(timeout=10)
    if proc.stdout:
        proc.stdout.close()
    path = lock_path(data_dir)
    for _ in range(100):
        try:
            with open(path, "r+b"):
                return
        except FileNotFoundError:
            return
        except PermissionError:
            time.sleep(0.05)


class JobLockTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

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
        # owner can wedge the next run.
        with heavy("after", wait=False, data_dir=self.dir, log=lambda *a: None) as got:
            self.assertTrue(got)

    def test_the_lock_file_lives_in_the_data_directory(self):
        with heavy("a", data_dir=self.dir, log=lambda *a: None):
            self.assertTrue(lock_path(self.dir).exists())


if __name__ == "__main__":
    unittest.main()
