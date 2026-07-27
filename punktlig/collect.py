"""The collector: poll Entur SIRI-ET for delta updates and archive every snapshot.

Designed to run two ways with the same code path:
  - locally:      python3 -m punktlig.collect --loop
  - serverless:   an HTTP handler calling poll_once() on an external cron tick

State that must survive between invocations (the SIRI requestorId, last poll
times for secondary feeds) lives in the database, not in the process.
"""

import argparse
import gzip
import sys
import time
import uuid
from datetime import datetime, timezone

from . import db, net, siri
from .config import (
    DATASET,
    DATASETS,
    DB_PATH,
    ET_URL,
    LINES_EVERY,
    MAX_PAGES,
    MODES,
    PAGE_SIZE,
    RAW_DIR,
    SECONDARY_EVERY,
    SX_EVERY,
    SX_URL,
    WEATHER_EVERY,
)
from .lines import line_modes, refresh_lines
from .weather import fetch_weather_rows


# Consecutive failed polls before the connection is replaced, and before the
# process gives up so the scheduler can restart it.
RECONNECT_AFTER = 2
GIVE_UP_AFTER = 5

# Codespaces are polled back to back, which the feed rate limits. A short
# gap keeps a normal cycle under the limit; net.get retries if it still hits.
BETWEEN_DATASETS = 3.0


def _now():
    return datetime.now(timezone.utc)


def _log(msg):
    print(f"{_now().isoformat(timespec='seconds')} {msg}", flush=True)


def _save_raw(kind, raw, suffix=""):
    ts = _now()
    folder = RAW_DIR / kind / ts.strftime("%Y-%m-%d")
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{ts.strftime('%H%M%S')}{suffix}.xml.gz"
    path.write_bytes(gzip.compress(raw))


def _due(conn, key, every_seconds):
    last = db.kv_get(conn, key)
    if last and (_now() - datetime.fromisoformat(last)).total_seconds() < every_seconds:
        return False
    db.kv_set(conn, key, _now().isoformat())
    return True


def poll_et(conn, modes, save_raw=True, dataset=None):
    """One delta poll against SIRI-ET for one codespace, following MoreData pagination.

    Each codespace keeps its own requestorId, because the delta stream is
    per requestor: sharing one across codespaces would make each poll
    consume the other's updates.
    """
    dataset = DATASET if dataset is None else dataset
    key = f"et_requestor_{dataset}" if dataset != DATASET else "et_requestor"
    requestor = db.kv_get(conn, key)
    if not requestor:
        requestor = str(uuid.uuid4())
        db.kv_set(conn, key, requestor)

    started = time.monotonic()
    polled_at = _now().isoformat()
    kept_calls, kept_journeys, dropped, pages = [], 0, 0, 0

    while True:
        url = f"{ET_URL}?datasetId={dataset}&requestorId={requestor}&maxSize={PAGE_SIZE}"
        # No retry here: the poll loop is the retry. Retrying a rate limited
        # request inside the same cycle sends more traffic exactly when the
        # feed asked for less, and the next cycle is a minute away anyway.
        raw = net.get(url, retries=0)
        pages += 1
        if save_raw:
            _save_raw("et", raw, suffix=f"_{dataset}_p{pages}")
        journeys, more = siri.parse_et(raw)

        for journey in journeys:
            if modes.get(journey["line_ref"]) not in MODES:
                dropped += 1
                continue
            kept_journeys += 1
            meta = {k: v for k, v in journey.items() if k != "calls"}
            for call in journey["calls"]:
                kept_calls.append({**meta, **call})

        if not more or pages >= MAX_PAGES:
            break

    duration_ms = int((time.monotonic() - started) * 1000)
    poll_id = db.insert_poll(
        conn,
        polled_at=polled_at,
        feed="et",
        dataset=dataset,
        pages=pages,
        n_journeys=kept_journeys,
        n_calls=len(kept_calls),
        n_dropped=dropped,
        duration_ms=duration_ms,
    )
    for row in kept_calls:
        row["poll_id"] = poll_id
    db.insert_calls(conn, kept_calls)
    return {
        "journeys": kept_journeys,
        "calls": len(kept_calls),
        "dropped": dropped,
        "pages": pages,
        "ms": duration_ms,
    }


def poll_once(conn, save_raw=True):
    if _due(conn, "lines_fetched_at", LINES_EVERY) or not line_modes(conn):
        n = refresh_lines(conn)
        _log(f"lines: refreshed {n} lines")

    modes = line_modes(conn)
    stats = {"journeys": 0, "calls": 0, "dropped": 0, "pages": 0, "ms": 0}
    failures = []
    for i, dataset in enumerate(DATASETS):
        # Only the primary codespace is polled every cycle; the others run
        # slower so the client stays inside the feed's request budget.
        if i and not _due(conn, f"et_due_{dataset}", SECONDARY_EVERY):
            continue
        if i:
            time.sleep(BETWEEN_DATASETS)  # stay under the feed's rate limit
        try:
            one = poll_et(conn, modes, save_raw=save_raw, dataset=dataset)
        except Exception as exc:
            # One codespace being rate limited or down must not cost us the
            # others: each has its own delta stream and its own next chance.
            failures.append(f"{dataset}: {exc}")
            _log(f"et[{dataset}]: ERROR {exc}")
            continue
        if len(DATASETS) > 1:
            _log(
                f"et[{dataset}]: {one['journeys']} journeys, {one['calls']} calls "
                f"({one['dropped']} dropped, {one['pages']} page(s), {one['ms']} ms)"
            )
        for k in stats:
            stats[k] += one[k]

    if failures and len(failures) == len(DATASETS):
        raise RuntimeError("; ".join(failures))  # nothing collected: a real failure

    if _due(conn, "weather_polled_at", WEATHER_EVERY):
        try:
            rows = fetch_weather_rows()
            db.insert_weather(conn, rows)
            _log(f"weather: stored {len(rows)} forecast hours")
        except Exception as exc:  # weather failing must never stop the ET archive
            _log(f"weather: ERROR {exc}")

    if _due(conn, "sx_polled_at", SX_EVERY):
        for dataset in DATASETS:
            try:
                raw = net.get(f"{SX_URL}?datasetId={dataset}")
                _save_raw("sx", raw, suffix=f"_{dataset}")
                situations = siri.parse_sx(raw)
                n = db.insert_situations(conn, _now().isoformat(), situations)
                _log(f"sx[{dataset}]: stored {len(situations)} situations ({n} line rows)")
            except Exception as exc:
                _log(f"sx[{dataset}]: ERROR {exc}")

    return stats


def run(conn, once=False, interval=60, save_raw=True, sleep=time.sleep,
        connect=None, poll=None):
    """Poll until told to stop, surviving transient failures and quitting on stuck ones.

    A database connection can end up permanently unable to write while the
    process looks healthy (a VACUUM collision did exactly that on
    2026-07-27). Repeated failures therefore first get a fresh connection,
    and if that does not help the process exits non-zero so the scheduler
    restarts it cleanly. Raw responses are archived before any database
    write, so even a run that ends this way loses nothing that
    `punktlig.reparse` cannot put back.
    """
    connect = connect or (lambda: db.connect(DB_PATH))
    poll = poll or poll_once
    failures = 0

    while True:
        started = time.monotonic()
        try:
            stats = poll(conn, save_raw=save_raw)
            _log(
                f"et: {stats['journeys']} journeys, {stats['calls']} calls "
                f"({stats['dropped']} dropped, {stats['pages']} page(s), {stats['ms']} ms)"
            )
            failures = 0
        except Exception as exc:
            failures += 1
            _log(f"et: ERROR {exc}")
            try:
                db.insert_poll(
                    conn, polled_at=_now().isoformat(), feed="et", dataset=DATASET, error=str(exc)
                )
            except Exception:
                pass
            if failures >= GIVE_UP_AFTER:
                _log(
                    f"et: {failures} consecutive failures, exiting so the "
                    "scheduler can restart from a clean process"
                )
                return 1
            if failures >= RECONNECT_AFTER:
                _log("et: reconnecting to the database")
                try:
                    conn.close()
                except Exception:
                    pass
                conn = connect()

        if once:
            return 0
        sleep(max(5.0, interval - (time.monotonic() - started)))


def main(argv=None):
    parser = argparse.ArgumentParser(description="Punktlig data collector")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--once", action="store_true", help="run a single poll and exit")
    group.add_argument("--loop", action="store_true", help="poll forever")
    parser.add_argument("--interval", type=int, default=60, help="seconds between polls in --loop")
    parser.add_argument("--no-raw", action="store_true", help="skip archiving raw gzipped XML")
    args = parser.parse_args(argv)

    conn = db.connect(DB_PATH)
    _log(f"db: {DB_PATH} | dataset: {DATASET} | modes: {','.join(MODES)}")
    return run(conn, once=args.once, interval=args.interval, save_raw=not args.no_raw)


if __name__ == "__main__":
    sys.exit(main())

