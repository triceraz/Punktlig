"""Put the trains back into days that were already compacted.

The SIRI parser only understood the wrapped form of a journey reference, so
every Vy and Go-Ahead row was archived with `journey_ref` empty. Every
consumer filters those rows out, which means the trains were collected and
then discarded: they never reached a training set or a map.

`reparse` repairs the hot database, but refuses a day that has moved to
parquet, and rightly so, since re-inserting it into SQLite would put the day
in both storage tiers at once. This module repairs the parquet instead.

The day's raw responses are parsed again with the fixed parser, the calls
are attached to the poll they belong to by matching the time they arrived,
and the day's file is rewritten as its usable rows plus the recovered ones.
Rows without a journey reference are dropped: they can never be read by
anything, and keeping them would duplicate every train.

Nothing is overwritten until the replacement has been written and counted,
and the original is kept as `.bak` for the caller to remove.
"""

import argparse
import csv
import gzip
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import db, siri
from .config import DATASET, DB_PATH, MODES, PARQUET_DIR, RAW_DIR
from .lines import line_modes
from .reparse import MATCH_WINDOW, _parse_name, _poll_groups, _stamp_to_time

# The columns the compactor writes, in order and with their types. The
# replacement file has to match both: a union of mismatched types either
# fails outright or silently coerces a column for the rest of the archive.
CALL_COLUMNS = [
    ("journey_ref", "VARCHAR"), ("operating_date", "VARCHAR"),
    ("poll_id", "BIGINT"), ("polled_at", "VARCHAR"), ("line_ref", "VARCHAR"),
    ("direction", "VARCHAR"), ("call_type", "VARCHAR"), ("stop_ref", "VARCHAR"),
    ("stop_name", "VARCHAR"), ("order_no", "BIGINT"), ("aimed_arr", "VARCHAR"),
    ("expected_arr", "VARCHAR"), ("actual_arr", "VARCHAR"),
    ("aimed_dep", "VARCHAR"), ("expected_dep", "VARCHAR"),
    ("actual_dep", "VARCHAR"), ("cancelled", "BIGINT"),
    ("call_cancelled", "BIGINT"), ("recorded_at", "VARCHAR"),
    ("operator_ref", "VARCHAR"), ("monitored", "BIGINT"),
]
CALL_NAMES = [name for name, _ in CALL_COLUMNS]

# An empty CSV field is ambiguous between "no value" and "the empty string",
# so absent values are written as a token no stop name or timestamp contains.
NULL_TOKEN = r"\N"


def _log(message):
    print(message, flush=True)


def poll_index(duck, parquet_dir, day):
    """(dataset, arrival time) for every poll of the day, from its parquet."""
    path = Path(parquet_dir) / "polls" / f"{day}.parquet"
    if not path.exists():
        return {}
    rows = duck.execute(
        "SELECT poll_id, dataset, polled_at FROM read_parquet(?) WHERE feed = 'et'",
        [str(path)],
    ).fetchall()
    index = {}
    for poll_id, dataset, polled_at in rows:
        when = polled_at if isinstance(polled_at, datetime) else datetime.fromisoformat(polled_at)
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        # The stored text is kept verbatim: every consumer sorts on it, and
        # a reformatted timestamp would order differently from its neighbours.
        index.setdefault(dataset, []).append((when, poll_id, polled_at))
    for value in index.values():
        value.sort(key=lambda item: item[0])
    return index


def match_poll(index, dataset, when):
    """(poll_id, polled_at) for this response, or None if the poll is unknown."""
    best, best_gap = None, MATCH_WINDOW
    for stored, poll_id, text in index.get(dataset, []):
        gap = abs(stored - when)
        if gap <= best_gap:
            best, best_gap = (poll_id, text), gap
    return best


def recovered_calls(day, raw_dir, modes, index, sources=None, allowed=None):
    """Yield the day's raw responses as call rows that carry a poll_id.

    A generator rather than a list: a single day holds around two million
    train calls, and materialising that many dictionaries costs more than a
    gigabyte before any of it reaches the database.

    `allowed` is the set of transport modes to keep, defaulting to whatever
    this process is configured to collect. It is a parameter rather than a
    read of the global because the default is tram and metro only: repairing
    trains while reading that default would find every train and discard it
    again, which is precisely the bug being repaired.
    """
    allowed = set(MODES if allowed is None else allowed)
    day_dir = Path(raw_dir) / "et" / day
    if not day_dir.is_dir():
        return

    for stamp, file_dataset, paths in _poll_groups(day_dir):
        dataset = file_dataset or DATASET
        if sources and dataset not in sources:
            continue
        match = match_poll(index, dataset, _stamp_to_time(day, stamp))
        if match is None:
            continue
        poll_id, polled_at = match
        for path in paths:
            journeys, _ = siri.parse_et(gzip.decompress(path.read_bytes()))
            for journey in journeys:
                if modes.get(journey["line_ref"]) not in allowed:
                    continue
                if not journey["journey_ref"]:
                    continue
                meta = {k: v for k, v in journey.items() if k != "calls"}
                for call in journey["calls"]:
                    yield {**meta, **call,
                           "poll_id": poll_id, "polled_at": polled_at}


def rewrite_day(duck, parquet_dir, day, rows, replaced_polls):
    """Swap the calls of the replaced polls for the freshly parsed ones.

    Selection is by poll rather than by missing identity, which is what makes
    a rerun safe: after the first pass the recovered rows have identity, so a
    rule of "keep everything identified" would append them a second time.
    Every row of a train poll is re-derived from the raw response, so the old
    ones can go wholesale.
    """
    path = Path(parquet_dir) / "calls" / f"{day}.parquet"
    if not path.exists():
        raise FileNotFoundError(path)

    ids = ", ".join(str(int(p)) for p in sorted(replaced_polls)) or "NULL"
    keep = f"journey_ref IS NOT NULL AND poll_id NOT IN ({ids})"
    before = duck.execute(
        f"SELECT COUNT(*) FROM read_parquet('{path.as_posix()}')"
    ).fetchone()[0]
    usable = duck.execute(
        f"SELECT COUNT(*) FROM read_parquet('{path.as_posix()}') WHERE {keep}"
    ).fetchone()[0]

    # Rows go out through a CSV rather than through executemany. Measured on
    # this archive, parameter binding managed 42 rows a second against 23 000
    # a second for parsing them, which put a single day at seven hours; the
    # database's own CSV reader ingests the same rows in seconds.
    staged = path.with_suffix(".parquet.staging.csv")
    added = 0
    with open(staged, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        for row in rows:
            writer.writerow(
                [NULL_TOKEN if row.get(name) is None else _coerce(row.get(name), kind)
                 for name, kind in CALL_COLUMNS]
            )
            added += 1

    # DuckDB will not bind the destination of a COPY, so the paths go into
    # the statement itself; forward slashes keep Windows separators out of a
    # SQL string literal.
    fresh = path.with_suffix(".parquet.new")
    columns = ", ".join(CALL_NAMES)
    spec = ", ".join(f"'{name}': '{kind}'" for name, kind in CALL_COLUMNS)
    # The dialect is stated rather than sniffed. Left to guess, DuckDB decided
    # this file had no quote character at all, and the first stop name
    # containing a comma, "Tangen i Sannidal (Bø, Lunde, Drangedal,
    # Neslandsvatn)", was split into extra columns.
    staged_sql = (
        f"read_csv('{staged.as_posix()}', header=false, "
        f"columns={{{spec}}}, nullstr='{NULL_TOKEN}', "
        "delim=',', quote='\"', escape='\"', auto_detect=false)"
    )
    kept = f"SELECT {columns} FROM read_parquet('{path.as_posix()}') WHERE {keep}"
    source = f"{kept} UNION ALL SELECT {columns} FROM {staged_sql}" if added else kept
    try:
        duck.execute(
            f"COPY ({source}) TO '{fresh.as_posix()}' "
            "(FORMAT PARQUET, COMPRESSION ZSTD)"
        )
        after = duck.execute(
            "SELECT COUNT(*) FROM read_parquet(?)", [str(fresh)]
        ).fetchone()[0]
        if after != usable + added:
            fresh.unlink(missing_ok=True)
            raise RuntimeError(
                f"{day}: replacement holds {after} rows, expected {usable + added}"
            )
        shutil.move(str(path), str(path) + ".bak")
        shutil.move(str(fresh), str(path))
    finally:
        staged.unlink(missing_ok=True)
    return {"before": before, "dropped": before - usable, "added": added, "after": after}


def _coerce(value, kind):
    """Match the compactor's own conversion, which came through SQLite.

    Booleans were stored as 0 and 1 there, and the integer columns must stay
    integers so the rewritten file unions with the rest of the archive.
    """
    if value is None:
        return None
    if kind == "BIGINT":
        if isinstance(value, bool):
            return int(value)
        try:
            return int(value)
        except (TypeError, ValueError):
            return None
    if isinstance(value, bool):
        return "1" if value else "0"
    return str(value)


def recover(day, db_path=DB_PATH, raw_dir=RAW_DIR, parquet_dir=PARQUET_DIR,
            sources=("VYG", "GOA", "SJN"), allowed=("rail",), dry_run=False):
    import duckdb

    conn = db.connect(db_path)
    try:
        modes = line_modes(conn)
        if not modes:
            raise RuntimeError("the line table is empty, so the mode filter cannot run")
    finally:
        conn.close()

    duck = duckdb.connect()
    try:
        duck.execute("SET memory_limit='2GB'")
        duck.execute(f"SET temp_directory='{Path(parquet_dir).parent}'")
        index = poll_index(duck, parquet_dir, day)
        if not index:
            _log(f"{day}: no compacted polls, nothing to repair here")
            return {}
        rows = recovered_calls(day, raw_dir, modes, index,
                               sources=sources, allowed=allowed)
        replaced = {poll_id for source in sources
                    for _, poll_id, _ in index.get(source, [])}
        if dry_run:
            counted = sum(1 for _ in rows)
            _log(f"{day}: would recover {counted:,} calls across "
                 f"{len(replaced)} polls")
            return {"added": counted, "polls": len(replaced)}
        _log(f"{day}: replacing {len(replaced)} polls")
        stats = rewrite_day(duck, parquet_dir, day, rows, replaced)
        _log(f"{day}: {stats['before']} -> {stats['after']} rows "
             f"(dropped {stats['dropped']} without identity, added {stats['added']})")
        return stats
    finally:
        duck.close()


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Recover trains in days that were already compacted"
    )
    parser.add_argument("--day", action="append", dest="days", required=True,
                        help="compacted day to repair (repeatable)")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    from .joblock import heavy

    with heavy("recover"):
        for day in args.days:
            recover(day, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
