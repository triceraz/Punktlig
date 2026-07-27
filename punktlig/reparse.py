"""Rebuild archive rows from the raw XML the collector already saved.

The collector writes two things per poll: the response bytes to
`data/raw/et/<day>/<HHMMSS>_p<page>.xml.gz`, and the parsed rows to SQLite.
When the database write fails but the file lands, the observation is not
lost, it is merely unparsed. This module puts it back.

It fills holes only. A day already moved to parquet is refused, because its
rows no longer live in SQLite and re-inserting them there would duplicate
the day across both storage tiers. A poll close enough in time to an
existing one is skipped, because a writing collector had already recorded
that observation.

Requires no network access: everything comes off disk.
"""

import argparse
import gzip
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import db, siri
from .config import DATASET, DB_PATH, MODES, PARQUET_DIR, RAW_DIR
from .lines import line_modes

# The raw file is stamped when the response arrived, up to a poll interval
# after the poll started. Any existing poll inside this window means the
# collector was writing at the time, so the file is already represented.
MATCH_WINDOW = timedelta(seconds=45)


def _log(msg):
    print(msg, flush=True)


def _poll_groups(day_dir):
    """Group a day's page files into polls. A new poll starts at page 1."""
    files = sorted(day_dir.glob("*.xml.gz"), key=lambda p: p.name)
    groups = []
    for path in files:
        stamp, _, page = path.name.partition("_")
        if page.startswith("p1.") or not groups:
            groups.append((stamp, [path]))
        else:
            groups[-1][1].append(path)
    return groups


def _stamp_to_time(day, stamp):
    return datetime.strptime(f"{day} {stamp}", "%Y-%m-%d %H%M%S").replace(tzinfo=timezone.utc)


def reparse(db_path=DB_PATH, raw_dir=RAW_DIR, days=None, dataset=DATASET,
            parquet_dir=PARQUET_DIR, dry_run=False):
    """Parse raw ET files back into the archive. Returns a stats dict."""
    et_dir = Path(raw_dir) / "et"
    if not et_dir.is_dir():
        return {"polls": 0, "calls": 0, "skipped": 0, "refused": []}

    if days is None:
        days = sorted(d.name for d in et_dir.iterdir() if d.is_dir())

    conn = db.connect(db_path)
    try:
        modes = line_modes(conn)
        if not modes:
            raise RuntimeError(
                "the line table is empty, so the mode filter cannot run; "
                "let the collector refresh lines first"
            )
        known = sorted(
            datetime.fromisoformat(row[0])
            for row in conn.execute("SELECT polled_at FROM poll WHERE feed = 'et'")
        )

        stats = {"polls": 0, "calls": 0, "skipped": 0, "refused": []}
        for day in days:
            day_dir = et_dir / day
            if not day_dir.is_dir():
                continue
            if (Path(parquet_dir) / "polls" / f"{day}.parquet").exists():
                stats["refused"].append(day)
                _log(f"{day}: already compacted to parquet, refusing to reparse")
                continue

            for stamp, paths in _poll_groups(day_dir):
                polled_at = _stamp_to_time(day, stamp)
                if any(abs(polled_at - t) <= MATCH_WINDOW for t in known):
                    stats["skipped"] += 1
                    continue

                calls, journeys, dropped = [], 0, 0
                for path in paths:
                    parsed, _ = siri.parse_et(gzip.decompress(path.read_bytes()))
                    for journey in parsed:
                        if modes.get(journey["line_ref"]) not in MODES:
                            dropped += 1
                            continue
                        journeys += 1
                        meta = {k: v for k, v in journey.items() if k != "calls"}
                        calls.extend({**meta, **call} for call in journey["calls"])

                if dry_run:
                    stats["polls"] += 1
                    stats["calls"] += len(calls)
                    continue

                poll_id = db.insert_poll(
                    conn,
                    polled_at=polled_at.isoformat(),
                    feed="et",
                    dataset=dataset,
                    pages=len(paths),
                    n_journeys=journeys,
                    n_calls=len(calls),
                    n_dropped=dropped,
                )
                db.insert_calls(conn, [dict(c, poll_id=poll_id) for c in calls])
                known.append(polled_at)
                stats["polls"] += 1
                stats["calls"] += len(calls)
        return stats
    finally:
        conn.close()


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Rebuild archive rows from saved raw XML"
    )
    parser.add_argument("--day", action="append", dest="days",
                        help="operating day to reparse (repeatable); default all")
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would be inserted, write nothing")
    args = parser.parse_args(argv)

    stats = reparse(days=args.days, dry_run=args.dry_run)
    verb = "would recover" if args.dry_run else "recovered"
    _log(
        f"{verb} {stats['polls']} polls, {stats['calls']} calls "
        f"({stats['skipped']} already present)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
