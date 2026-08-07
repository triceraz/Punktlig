"""Is collection actually working right now?

On 2026-07-27 the collector ran for five and a half hours without writing a
single row. The scheduled task said Running, the process existed, the log
kept growing. Every signal that describes the process was healthy and the
only signal that mattered, the archive, was not.

So health is measured on the archive: how long since a poll landed, and
whether recent polls carried rows or only errors. Exit code 1 means
something is wrong, which is what makes this usable from a scheduler.

That check was written for a total stall, and on 2026-08-06 it missed the
other kind. Flytoget was refused with 429 on every cycle for seven hours
while Ruter kept polling every minute, so the newest poll was always
seconds old and the verdict stayed OK. One operator's share of the archive
had quietly stopped arriving and nothing said so. A codespace failing
alone is not a failed poll: the exception is caught per stream and no row
is written at all, which is invisible to a question asked about the newest
row overall. Each stream is therefore also judged on its own.
"""

import argparse
import sys
from datetime import datetime, timedelta, timezone

from . import db
from .config import DATA_DIR, DATASETS, DB_PATH

# A poll a minute means anything older than this is already several misses.
MAX_AGE = timedelta(minutes=10)
# How far back to look when judging whether polls are succeeding.
RECENT = timedelta(minutes=30)
MAX_ERROR_SHARE = 0.5
# How many of its own cycles a single stream may miss before it counts as
# stopped. Generous, because one refused poll is ordinary and the feed says
# so with a 429 that the client already retries.
STREAM_MISSES = 6
# Long enough to measure a slow stream's cadence, short enough that a
# codespace added yesterday is judged on how it behaves today.
STREAM_WINDOW = timedelta(hours=24)
MIN_POLLS_TO_JUDGE = 5
# What stands in for a cadence that cannot be measured yet.
UNKNOWN_CADENCE_GRACE = 3600

LOG_PATH = DATA_DIR / "health.log"


def stream_ages(conn, now):
    """Per codespace: how long since one of its polls succeeded, and how
    often it normally manages one.

    The cadence is measured rather than read from configuration. The
    secondaries run on a different clock from the primary, that clock is an
    environment variable this module never sees, and a check that has to be
    kept in step with a setting elsewhere is a check that will one day be out
    of step with it.

    Success is the only thing asked about, not whether rows came back. A
    codespace with no service at four in the morning still answers, and
    reading it as dead would cry wolf every night. A rate-limited one does
    not answer at all, and writes no row: that absence is the signal.

    Only the codespaces currently being collected are judged. Switching one
    off is not a fault, and the first version of this check could not tell
    the difference: SJN was removed deliberately and the verdict then read
    PROBLEM every hour for as long as the archive remembered it. An alarm
    that is always on is an alarm nobody reads, which would have cost more
    than the outage it was written for.
    """
    wanted = set(DATASETS)
    since = (now - STREAM_WINDOW).isoformat()
    seen = {}
    for dataset, polled_at in conn.execute(
        "SELECT dataset, polled_at FROM poll WHERE feed = 'et' AND error IS NULL "
        "AND polled_at > ? ORDER BY dataset, polled_at", (since,)
    ):
        if wanted and dataset not in wanted:
            continue
        seen.setdefault(dataset, []).append(datetime.fromisoformat(polled_at))

    out = {}
    for dataset in sorted(wanted or seen):
        times = seen.get(dataset, [])
        # A configured codespace with nothing at all is the loudest case, not
        # the quietest: skipping it for want of history is how a stream that
        # has been dead longer than the window disappears from the check
        # entirely, which is the failure this function exists to prevent.
        if not times:
            out[dataset] = {"age_seconds": None, "cadence_seconds": None, "polls": 0}
            continue
        age = (now - times[-1]).total_seconds()
        if len(times) < MIN_POLLS_TO_JUDGE:
            out[dataset] = {"age_seconds": age, "cadence_seconds": None,
                            "polls": len(times)}
            continue
        gaps = sorted((b - a).total_seconds() for a, b in zip(times, times[1:]))
        out[dataset] = {"age_seconds": age,
                        "cadence_seconds": gaps[len(gaps) // 2],
                        "polls": len(times)}
    return out


def check(db_path=DB_PATH, now=None):
    """Return a verdict on the archive: is data still arriving?"""
    now = now or datetime.now(timezone.utc)
    conn = db.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT polled_at, error FROM poll WHERE feed = 'et' "
            "ORDER BY polled_at DESC LIMIT 200"
        ).fetchall()
        streams = stream_ages(conn, now)
    finally:
        conn.close()

    if not rows:
        return {"ok": False, "age_minutes": None, "recent_polls": 0,
                "error_share": None, "problems": ["the archive has no polls at all"]}

    latest = datetime.fromisoformat(rows[0][0])
    age = now - latest
    recent = [r for r in rows if now - datetime.fromisoformat(r[0]) <= RECENT]
    failed = [r for r in recent if r[1]]
    error_share = len(failed) / len(recent) if recent else None

    problems = []
    if age > MAX_AGE:
        problems.append(
            f"no new polls for {age.total_seconds() / 60:.0f} minutes"
        )
    if error_share is not None and error_share > MAX_ERROR_SHARE:
        problems.append(
            f"{error_share:.0%} of the last {len(recent)} polls failed "
            f"(latest: {failed[0][1]})"
        )
    if not recent:
        problems.append("no polls at all in the last half hour")

    for dataset, s in sorted(streams.items()):
        if not s["polls"]:
            problems.append(
                f"{dataset} is configured but has not answered once in "
                f"{STREAM_WINDOW.total_seconds() / 3600:.0f} hours"
            )
        elif s["cadence_seconds"] is None:
            # Too few polls to know its rhythm, so an hour stands in for it.
            if s["age_seconds"] > UNKNOWN_CADENCE_GRACE:
                problems.append(
                    f"{dataset} has answered only {s['polls']} times, "
                    f"last {s['age_seconds'] / 60:.0f} minutes ago"
                )
        else:
            allowed = max(MAX_AGE.total_seconds(),
                          s["cadence_seconds"] * STREAM_MISSES)
            if s["age_seconds"] > allowed:
                problems.append(
                    f"{dataset} has not answered for "
                    f"{s['age_seconds'] / 60:.0f} minutes, and normally "
                    f"manages one poll every {s['cadence_seconds'] / 60:.0f} min"
                )

    return {
        "ok": not problems,
        "age_minutes": age.total_seconds() / 60,
        "recent_polls": len(recent),
        "error_share": error_share,
        "streams": streams,
        "problems": problems,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description="Check that collection is alive")
    parser.add_argument("--log", action="store_true",
                        help="append the verdict to data/health.log")
    args = parser.parse_args(argv)

    result = check()
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    # Naming every stream on the OK line too, so the log shows which ones were
    # being watched. A verdict that only says OK cannot tell you afterwards
    # whether it was looking at the stream that later went quiet.
    streams = " ".join(
        f"{name}:" + ("aldri" if s["age_seconds"] is None
                      else f"{s['age_seconds'] / 60:.0f}m")
        for name, s in sorted(result.get("streams", {}).items())
    )
    if result["ok"]:
        line = (f"{stamp} OK last poll {result['age_minutes']:.1f} min ago, "
                f"{result['recent_polls']} polls in the last half hour"
                + (f" [{streams}]" if streams else ""))
    else:
        line = (f"{stamp} PROBLEM {'; '.join(result['problems'])}"
                + (f" [{streams}]" if streams else ""))

    print(line, flush=True)
    if args.log:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
