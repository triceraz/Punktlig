"""Export what the website draws: the network, the vehicles, and the score.

Three things, in one file the page can fetch:

  network   every stop with a position, and the segments between them, so the
            map is drawn from our own archive rather than borrowed from a
            background map service
  vehicles  everything running right now, placed between the stop it last
            passed and the one it is expected at next, with both predictions
  score     the comparison in words a passenger would use, not in mean
            absolute error

Positions are interpolated. The feed tells us when a vehicle passed a stop
and when it is expected at the next one, not where it is in between, so a
vehicle is drawn along that line by the clock. It is an approximation and
the page says so.
"""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import db
from .config import DB_PATH
from .predict import predict, upcoming_rows

OUT = Path(__file__).resolve().parent.parent / "web" / "data.json"
ON_TIME = 60.0

# The map is about Oslo and Akershus. Trains run to Bergen and Trondheim, and
# letting them set the bounds shrinks the city everyone came to look at into a
# smudge, so anything outside this box is left off the drawing.
BBOX = (59.55, 60.35, 10.20, 11.45)  # south, north, west, east


def _inside(lat, lon):
    return BBOX[0] <= lat <= BBOX[1] and BBOX[2] <= lon <= BBOX[3]


EDGES_SQL = """
SELECT DISTINCT a.stop_ref, b.stop_ref, l.mode
FROM src.call_snapshot a
JOIN src.call_snapshot b
  ON b.poll_id = a.poll_id AND b.journey_ref = a.journey_ref
 AND b.operating_date = a.operating_date AND b.order_no = a.order_no + 1
LEFT JOIN src.line l ON l.line_ref = a.line_ref
WHERE a.stop_ref IS NOT NULL AND b.stop_ref IS NOT NULL
  AND a.poll_id >= (SELECT MAX(poll_id) - {polls} FROM src.poll)
"""


def network(conn, db_path=DB_PATH, recent_polls=400):
    """Stops with positions, and the segments lines actually run between.

    The self-join that finds consecutive stops is a scan over the whole
    archive, which SQLite does slowly and DuckDB does in seconds. It only
    needs recent polls: the shape of the network is the same yesterday as
    today, and a stop pair seen once is a stop pair.
    """
    import duckdb

    quays = {
        ref: (round(lon, 5), round(lat, 5), name)
        for ref, lat, lon, name in conn.execute(
            "SELECT quay_ref, lat, lon, COALESCE(stop_place_name, name) FROM quay "
            "WHERE lat IS NOT NULL"
        )
        if _inside(lat, lon)
    }
    duck = duckdb.connect()
    try:
        duck.execute("SET memory_limit='1GB'")
        duck.execute("INSTALL sqlite; LOAD sqlite;")
        duck.execute(f"ATTACH '{db_path}' AS src (TYPE sqlite, READ_ONLY)")
        pairs = duck.execute(EDGES_SQL.format(polls=recent_polls)).fetchall()
    finally:
        duck.close()

    used, index, stops = {}, [], []
    def idx(ref):
        if ref not in used:
            lon, lat, name = quays[ref]
            used[ref] = len(stops)
            stops.append([lon, lat, name])
        return used[ref]

    edges = []
    seen = set()
    for a, b, mode in pairs:
        if a not in quays or b not in quays or (a, b) in seen:
            continue
        seen.add((a, b))
        edges.append([idx(a), idx(b), mode or "bus"])
    return {"stops": stops, "edges": edges}


def vehicles(conn, rows, now=None):
    """One entry per running vehicle: where it is, and what each side predicts."""
    now = now or datetime.now(timezone.utc)
    quays = {
        ref: (lon, lat) for ref, lat, lon in
        conn.execute("SELECT quay_ref, lat, lon FROM quay WHERE lat IS NOT NULL")
        if _inside(lat, lon)
    }
    modes = dict(conn.execute("SELECT line_ref, mode FROM line"))

    by_journey = {}
    for row in rows:
        key = (row["journey_ref"], row["operating_date"])
        current = by_journey.get(key)
        if current is None or row["order_no"] < current["order_no"]:
            by_journey[key] = row

    origins = _origin_stops(conn, list(by_journey.values()))
    out = []
    for row in by_journey.values():
        target = quays.get(row["stop_ref"])
        if not target:
            continue
        expected = datetime.fromisoformat(row["entur_expected_ts"])
        polled = datetime.fromisoformat(row["polled_at"])
        # Travelled fraction from the last passed stop towards the next one.
        started = polled - (polled - polled)  # keep tz, start at poll time
        if row["since_last_stop_sec"] is not None:
            started = polled - _seconds(row["since_last_stop_sec"])
        span = (expected - started).total_seconds()
        fraction = 0.0 if span <= 0 else max(0.0, min(1.0, (now - started).total_seconds() / span))

        origin = origins.get(
            (row["poll_id"], row["journey_ref"], row["operating_date"],
             row["current_order"])
        ) or row["stop_ref"]
        source = quays.get(origin, target)
        if not _inside(target[1], target[0]):
            continue
        out.append({
            "lon": round(source[0] + (target[0] - source[0]) * fraction, 5),
            "lat": round(source[1] + (target[1] - source[1]) * fraction, 5),
            "line": (row["line_ref"] or "").split(":")[-1],
            "mode": modes.get(row["line_ref"], "bus"),
            "stop": row["stop_name"],
            "in_min": round(row["horizon_sec"] / 60, 1),
            "entur": round(row["entur_pred_delay_sec"]),
            "model": round(row["model_pred_delay_sec"]),
            "now": round(row["current_delay_sec"]),
        })
    return out


def _seconds(value):
    from datetime import timedelta

    return timedelta(seconds=float(value))


def _origin_stops(conn, rows):
    """The stop each vehicle last passed, looked up once for all of them."""
    polls = {row["poll_id"] for row in rows}
    if not polls:
        return {}
    marks = ", ".join("?" for _ in polls)
    found = {}
    for poll_id, journey, date, order_no, stop_ref in conn.execute(
        f"SELECT poll_id, journey_ref, operating_date, order_no, stop_ref "
        f"FROM call_snapshot WHERE poll_id IN ({marks}) AND call_type = 'recorded'",
        list(polls),
    ):
        found[(poll_id, journey, date, order_no)] = stop_ref
    return found


def score(model_dir):
    """The validation comparison, in the terms a passenger would use."""
    meta = json.loads(Path(model_dir, "punktlig-lgbm.meta.json").read_text())
    buckets = meta["validation"]
    total = sum(b["n"] for b in buckets.values())
    weighted = {
        name: sum(b[name] * b["n"] for b in buckets.values()) / total
        for name in ("timetable", "naive", "entur", "model")
    }
    return {
        "rows": total,
        "trained_on": meta["rows"],
        "trained_at": meta["trained_at"],
        "weighted": {k: round(v, 1) for k, v in weighted.items()},
        "buckets": [
            {"horizon": k, "n": v["n"],
             "timetable": round(v["timetable"], 1), "naive": round(v["naive"], 1),
             "entur": round(v["entur"], 1), "model": round(v["model"], 1)}
            for k, v in buckets.items()
        ],
    }


def build(out=OUT, model_dir=None, db_path=DB_PATH):
    from .predict import MODEL_DIR

    from .config import DATASETS

    model_dir = Path(model_dir or MODEL_DIR)
    # Every codespace in one pass: the history indexes are built from the whole
    # archive, so asking for them once rather than once per codespace is the
    # difference between a gigabyte and a wedged machine.
    rows = predict(upcoming_rows(datasets=DATASETS), model_dir=model_dir)
    conn = db.connect(db_path)
    try:
        payload = {
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "network": network(conn, db_path=db_path),
            "vehicles": vehicles(conn, rows),
            "score": score(model_dir),
        }
    finally:
        conn.close()

    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                   encoding="utf-8")
    return payload


def main(argv=None):
    parser = argparse.ArgumentParser(description="Export the site's data file")
    parser.add_argument("--out", default=str(OUT))
    parser.add_argument("--model", default=None)
    args = parser.parse_args(argv)

    payload = build(out=args.out, model_dir=args.model)
    size = Path(args.out).stat().st_size / 1e6
    print(f"{len(payload['network']['stops'])} stops, "
          f"{len(payload['network']['edges'])} edges, "
          f"{len(payload['vehicles'])} vehicles -> {args.out} ({size:.1f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
