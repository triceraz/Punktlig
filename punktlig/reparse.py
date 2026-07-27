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

from . import db, siri  # noqa: F401  (siri used by both feeds)
from .config import DATASET, DB_PATH, MODES, PARQUET_DIR, RAW_DIR
from .lines import line_modes

# The raw file is stamped when the response arrived, up to a poll interval
# after the poll started. Any existing poll inside this window means the
# collector was writing at the time, so the file is already represented.
MATCH_WINDOW = timedelta(seconds=45)


def _log(msg):
    print(msg, flush=True)


def _parse_name(name):
    """(stamp, dataset, page) from HHMMSS[_DATASET]_pN.xml.gz.

    Files written before the collector polled several codespaces carry no
    dataset in the name, so both shapes have to read.
    """
    parts = name.split(".")[0].split("_")
    if len(parts) >= 3:
        return parts[0], parts[1], parts[2]
    if len(parts) == 2:
        return parts[0], None, parts[1]
    return parts[0], None, "p1"


def _poll_groups(day_dir):
    """Group a day's page files into polls: a new poll starts at page 1,
    and each codespace is polled separately."""
    files = sorted(day_dir.glob("*.xml.gz"), key=lambda p: p.name)
    groups, current = [], None
    for path in files:
        stamp, dataset, page = _parse_name(path.name)
        # A dataset of None is a real value (files written before the
        # collector polled several codespaces), so emptiness is the test
        # for "no group yet", not the dataset itself.
        if page == "p1" or not groups or current != dataset:
            groups.append((stamp, dataset, [path]))
            current = dataset
        else:
            groups[-1][2].append(path)
    return groups


def _stamp_to_time(day, stamp):
    return datetime.strptime(f"{day} {stamp}", "%Y-%m-%d %H%M%S").replace(tzinfo=timezone.utc)


def reparse_sx(conn, raw_dir, days, dry_run=False):
    """Parse raw SX files into the situation table. Same hole-filling rule."""
    sx_dir = Path(raw_dir) / "sx"
    if not sx_dir.is_dir():
        return 0
    known = sorted(
        datetime.fromisoformat(row[0])
        for row in conn.execute("SELECT DISTINCT polled_at FROM situation")
    )
    added = 0
    for day in days:
        day_dir = sx_dir / day
        if not day_dir.is_dir():
            continue
        for path in sorted(day_dir.glob("*.xml.gz")):
            polled_at = _stamp_to_time(day, _parse_name(path.name)[0])
            if any(abs(polled_at - t) <= MATCH_WINDOW for t in known):
                continue
            situations = siri.parse_sx(gzip.decompress(path.read_bytes()))
            if not dry_run:
                db.insert_situations(conn, polled_at.isoformat(), situations)
            known.append(polled_at)
            added += len(situations)
    return added


def reparse(db_path=DB_PATH, raw_dir=RAW_DIR, days=None, dataset=DATASET,
            parquet_dir=PARQUET_DIR, dry_run=False):
    """Parse raw ET and SX files back into the archive. Returns a stats dict."""
    et_dir = Path(raw_dir) / "et"
    if not et_dir.is_dir():
        return {"polls": 0, "calls": 0, "skipped": 0, "refused": [], "situations": 0}

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
        # Polls are matched per codespace: each has its own stream, so one
        # codespace already collected says nothing about another.
        known = {}
        for polled_at, poll_dataset in conn.execute(
            "SELECT polled_at, dataset FROM poll WHERE feed = 'et'"
        ):
            known.setdefault(poll_dataset, []).append(datetime.fromisoformat(polled_at))
        for value in known.values():
            value.sort()

        stats = {"polls": 0, "calls": 0, "skipped": 0, "refused": [], "situations": 0}
        stats["situations"] = reparse_sx(conn, raw_dir, days, dry_run=dry_run)
        for day in days:
            day_dir = et_dir / day
            if not day_dir.is_dir():
                continue
            if (Path(parquet_dir) / "polls" / f"{day}.parquet").exists():
                stats["refused"].append(day)
                _log(f"{day}: already compacted to parquet, refusing to reparse")
                continue

            for stamp, file_dataset, paths in _poll_groups(day_dir):
                polled_at = _stamp_to_time(day, stamp)
                seen = known.get(file_dataset or dataset, [])
                if any(abs(polled_at - t) <= MATCH_WINDOW for t in seen):
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
                    dataset=file_dataset or dataset,
                    pages=len(paths),
                    n_journeys=journeys,
                    n_calls=len(calls),
                    n_dropped=dropped,
                )
                db.insert_calls(conn, [dict(c, poll_id=poll_id) for c in calls])
                known.setdefault(file_dataset or dataset, []).append(polled_at)
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
        f"{verb} {stats['polls']} polls, {stats['calls']} calls, "
        f"{stats['situations']} situations ({stats['skipped']} already present)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
