"""One heavy DuckDB job at a time, machine-wide.

Two DuckDB processes working the archive at once crash this machine: the
symptom is an access violation inside the extension rather than a Python
exception, so nothing is raised, nothing is logged, and the job simply
disappears. It happened on 2026-07-29 when a metadata rebuild overlapped the
site export, and again on 2026-08-02 when the site task, which now runs every
ten minutes, landed on top of an hour-long replay.

The lock is held by the operating system rather than by a file we write, so a
process that is killed or crashes releases it immediately. A PID file would
have to guess whether a stale owner is still alive, and guessing wrong either
blocks every future run or defeats the point.

The two sides want opposite behaviour on contention. A replay is asked for
once and must not be skipped, so it waits. The site export runs again in ten
minutes anyway, so it gives up instead of queueing behind an hour of work.
"""

import os
import sys
import time
from contextlib import contextmanager
from pathlib import Path

from .config import DATA_DIR


def step_aside():
    """Run this process at background priority for as long as it lives.

    The collector shares one disk with the replay and the training run, and
    those read gigabytes at a time. Collection then starves: polls that take
    six seconds started arriving minutes apart, and the archive grew holes
    that no amount of retrying can fill afterwards. A missed poll is gone for
    good; a slower training run is only slower. On Windows the background
    mode lowers I/O priority as well as CPU, which is the part that matters
    here, and it is released automatically when the process exits.
    """
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        PROCESS_MODE_BACKGROUND_BEGIN = 0x00100000
        try:
            kernel32 = ctypes.windll.kernel32
            # The process handle is a pointer. Left to ctypes' default of a
            # C int it is truncated on a 64-bit build, the call fails, and
            # the priority silently stays where it was.
            kernel32.GetCurrentProcess.restype = wintypes.HANDLE
            kernel32.SetPriorityClass.argtypes = [wintypes.HANDLE, wintypes.DWORD]
            kernel32.SetPriorityClass.restype = wintypes.BOOL
            kernel32.SetPriorityClass(kernel32.GetCurrentProcess(),
                                      PROCESS_MODE_BACKGROUND_BEGIN)
        except Exception:
            pass
    else:
        try:
            os.nice(10)
        except Exception:
            pass

# Two separate concerns, so two locks. The heavy analysis jobs must not run
# at the same time as each other; the collector must not run at the same time
# as another collector. They never contend with one another.
LOCK_NAME = "duckdb.lock"
COLLECTOR_LOCK = "collector.lock"

if sys.platform == "win32":
    import msvcrt

    def _grab(handle):
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False

    def _drop(handle):
        try:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
else:
    import fcntl

    def _grab(handle):
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False

    def _drop(handle):
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass


def lock_path(data_dir=None, name=LOCK_NAME):
    return Path(data_dir or DATA_DIR) / name


@contextmanager
def heavy(label, wait=True, poll_seconds=20, data_dir=None, log=print,
          name=LOCK_NAME):
    """Hold the machine-wide DuckDB lock for the duration of the block.

    Yields True when the lock was taken. With `wait=False` it yields False
    instead of blocking, and the caller is expected to do nothing at all;
    running anyway is the exact failure this module exists to prevent.
    """
    path = lock_path(data_dir, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+b")
    try:
        handle.seek(0)
        if not _grab(handle):
            if not wait:
                log(f"{label}: another DuckDB job holds the lock, skipping this run")
                yield False
                return
            log(f"{label}: waiting for the DuckDB lock")
            while not _grab(handle):
                time.sleep(poll_seconds)
        handle.seek(0)
        handle.truncate()
        handle.write(f"{label} pid={os.getpid()}\n".encode())
        handle.flush()
        if name == LOCK_NAME:
            # Analysis jobs yield the disk to the collector. The collector
            # takes a different lock and keeps its normal priority.
            step_aside()
        try:
            yield True
        finally:
            _drop(handle)
    finally:
        handle.close()
