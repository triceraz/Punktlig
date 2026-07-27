"""Is collection actually working right now?

On 2026-07-27 the collector ran for five and a half hours without writing a
single row. The scheduled task said Running, the process existed, the log
kept growing. Every signal that describes the process was healthy and the
only signal that mattered, the archive, was not.

So health is measured on the archive: how long since a poll landed, and
whether recent polls carried rows or only errors. Exit code 1 means
something is wrong, which is what makes this usable from a scheduler.
"""

import argparse
import sys
from datetime import datetime, timedelta, timezone

from . import db
from .config import DATA_DIR, DB_PATH

# A poll a minute means anything older than this is already several misses.
MAX_AGE = timedelta(minutes=10)
# How far back to look when judging whether polls are succeeding.
RECENT = timedelta(minutes=30)
MAX_ERROR_SHARE = 0.5

LOG_PATH = DATA_DIR / "health.log"


def check(db_path=DB_PATH, now=None):
    """Return a verdict on the archive: is data still arriving?"""
    now = now or datetime.now(timezone.utc)
    conn = db.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT polled_at, error FROM poll WHERE feed = 'et' "
            "ORDER BY polled_at DESC LIMIT 200"
        ).fetchall()
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

    return {
        "ok": not problems,
        "age_minutes": age.total_seconds() / 60,
        "recent_polls": len(recent),
        "error_share": error_share,
        "problems": problems,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description="Check that collection is alive")
    parser.add_argument("--log", action="store_true",
                        help="append the verdict to data/health.log")
    args = parser.parse_args(argv)

    result = check()
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if result["ok"]:
        line = (f"{stamp} OK last poll {result['age_minutes']:.1f} min ago, "
                f"{result['recent_polls']} polls in the last half hour")
    else:
        line = f"{stamp} PROBLEM {'; '.join(result['problems'])}"

    print(line, flush=True)
    if args.log:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
