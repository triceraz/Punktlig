"""Analysis jobs must yield the disk to the collector.

One disk carries the archive, the replay's spill and the training set. When
the replay and the training read gigabytes, collection starves: polls that
take six seconds started arriving minutes apart, leaving holes in the archive
that no retry can fill afterwards. A slower training run costs nothing by
comparison, so the heavy jobs run at background priority.
"""

import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from punktlig.joblock import COLLECTOR_LOCK, LOCK_NAME, step_aside

ROOT = str(Path(__file__).resolve().parents[1])


def priority_after(lock_name, data_dir):
    """Priority class after taking the given lock, from a known starting point.

    The child would otherwise inherit whatever priority the test runner
    happens to have, which makes "did it drop" unanswerable. It is put at
    normal first, so the reading afterwards is about the lock and nothing
    else.
    """
    code = textwrap.dedent(f"""
        import sys
        sys.path.insert(0, {ROOT!r})
        if sys.platform == "win32":
            import ctypes
            from ctypes import wintypes
            k = ctypes.windll.kernel32
            k.GetCurrentProcess.restype = wintypes.HANDLE
            k.SetPriorityClass.argtypes = [wintypes.HANDLE, wintypes.DWORD]
            k.GetPriorityClass.argtypes = [wintypes.HANDLE]
            k.GetPriorityClass.restype = wintypes.DWORD
            k.SetPriorityClass(k.GetCurrentProcess(), 0x00000020)  # NORMAL
        from punktlig.joblock import heavy
        with heavy("t", data_dir={str(data_dir)!r}, name={lock_name!r},
                   log=lambda *a: None):
            if sys.platform == "win32":
                print(k.GetPriorityClass(k.GetCurrentProcess()))
            else:
                import os
                print(os.nice(0))
    """)
    return int(subprocess.run([sys.executable, "-c", code],
                              capture_output=True, text=True, check=True).stdout)


class PriorityTest(unittest.TestCase):
    NORMAL = 0x00000020        # NORMAL_PRIORITY_CLASS
    BELOW_NORMAL = 0x00004000  # what background mode leaves behind

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def test_a_heavy_job_drops_below_normal(self):
        got = priority_after(LOCK_NAME, self.dir)
        if sys.platform == "win32":
            self.assertNotEqual(got, self.NORMAL)
        else:
            self.assertGreater(got, 0)

    def test_the_collector_keeps_its_priority(self):
        got = priority_after(COLLECTOR_LOCK, self.dir)
        if sys.platform == "win32":
            self.assertEqual(got, self.NORMAL)
        else:
            self.assertEqual(got, 0)

    def test_stepping_aside_never_raises(self):
        step_aside()  # idempotent and harmless in this process too


if __name__ == "__main__":
    unittest.main()
