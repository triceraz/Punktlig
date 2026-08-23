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
    _background(BACKGROUND_BEGIN, nice=10)


# Windows takes both of these through SetPriorityClass on the current process.
BACKGROUND_BEGIN = 0x00100000
BACKGROUND_END = 0x00200000


def _background(mode, nice):
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        try:
            kernel32 = ctypes.windll.kernel32
            # The process handle is a pointer. Left to ctypes' default of a
            # C int it is truncated on a 64-bit build, the call fails, and
            # the priority silently stays where it was.
            kernel32.GetCurrentProcess.restype = wintypes.HANDLE
            kernel32.SetPriorityClass.argtypes = [wintypes.HANDLE, wintypes.DWORD]
            kernel32.SetPriorityClass.restype = wintypes.BOOL
            kernel32.SetPriorityClass(kernel32.GetCurrentProcess(), mode)
        except Exception:
            pass
    else:
        try:
            os.nice(nice)
        except Exception:
            pass


@contextmanager
def at_full_speed():
    """Leave background mode for a stretch that is compute, not disk.

    Background mode exists to keep gigabyte-sized reads off the disk the
    collector is writing to. On Windows it throttles the CPU to idle as well,
    and on a step that is pure arithmetic that is not a courtesy but a
    twenty-seven fold slowdown: loading and predicting both quantile models
    over 119 194 rows costs 4.4 seconds at normal priority and 119.7 in
    background mode, measured on this machine rather than assumed.

    That was most of the site export. Inference is a few seconds of CPU on six
    cores and reads nothing, so it cannot starve a poll the way a replay
    reading the whole archive can. The mode is taken up again afterwards, so
    everything that does touch the disk keeps yielding to the collector.

    On POSIX this is a no-op in practice: `nice` only goes up without
    privileges, so the process stays where `step_aside` put it. Said here
    rather than discovered later, since the Windows path is the one this
    project runs on and the difference is silent.
    """
    _background(BACKGROUND_END, nice=-10)
    try:
        yield
    finally:
        _background(BACKGROUND_BEGIN, nice=10)

# Three separate concerns, so three locks, because a lock that guards more
# than its reason costs something real. Holding the DuckDB lock through a
# three-hour training run kept the site export from publishing for three
# hours, and training does not touch DuckDB at all.
#
#   duckdb    the export, the replay and the recovery, which crash the
#             machine when two of them run at once
#   fitting   training and the quantile ladder, which are memory-heavy but
#             read plain SQLite and can safely run beside an export
#   collector one collector, machine-wide
LOCK_NAME = "duckdb.lock"
FITTING_LOCK = "fitting.lock"
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
          name=LOCK_NAME, background=True):
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
        if name != COLLECTOR_LOCK and background:
            # Analysis jobs yield the disk to the collector. The collector
            # keeps its normal priority: a missed poll cannot be recovered,
            # a slower training run can.
            #
            # `background=False` is for jobs that are arithmetic, not bulk
            # disk. Windows' background mode drops memory priority along with
            # CPU and I/O, and a LightGBM fit under it sat for four days with
            # 56 CPU-hours consumed and a 30 MB working set for a matrix
            # measured in gigabytes: every page it touched was evicted before
            # it came back. Such a job never finishes; it just burns.
            step_aside()
        try:
            yield True
        finally:
            _drop(handle)
    finally:
        handle.close()
