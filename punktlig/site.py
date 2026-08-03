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
from .config import DB_PATH, PARQUET_DIR
from .predict import predict, upcoming_rows

OUT = Path(__file__).resolve().parent.parent / "web" / "data.json"
ON_TIME = 60.0

# The map is about Oslo and Akershus. Trains run to Bergen and Trondheim, and
# letting them set the bounds shrinks the city everyone came to look at into a
# smudge, so anything outside this box is left off the drawing.
BBOX = (59.55, 60.35, 10.20, 11.45)  # south, north, west, east


def _inside(lat, lon):
    return BBOX[0] <= lat <= BBOX[1] and BBOX[2] <= lon <= BBOX[3]


# Entur publishes one colour per transport mode, so every metro line comes back
# the same orange. Ruter's own metro map gives each line its own colour, and
# that colouring is the thing an Osloite recognises before reading a word, so
# the five lines are named here. Read off Ruter's official metro map.
METRO_COLOURS = {
    "1": "#1B9AD8",  # Frognerseteren - Bergkrystallen
    "2": "#EC700C",  # Østerås - Ellingsrudåsen
    "3": "#B14FA0",  # Kolsås - Mortensrud
    "4": "#00437B",  # Vestli - Bergkrystallen
    "5": "#3EA845",  # Sognsvann - Vestli
}

# Where a line has no colour of its own, the mode decides. Bus is deliberately
# dim: 377 bus lines drawn as brightly as five metro lines is porridge.
MODE_COLOURS = {
    "metro": "#EC700C", "tram": "#0B91EF", "rail": "#5B7FDE",
    "water": "#00A0A0", "bus": "#33404F",
}
MODE_WEIGHT = {"metro": 3.4, "tram": 2.2, "rail": 1.8, "water": 1.4, "bus": 0.7}

ROUTES_SQL = """
WITH recent AS (
    SELECT c.line_ref, c.direction, c.journey_ref, c.operating_date,
           c.order_no, c.stop_ref
    FROM src.call_snapshot c
    WHERE c.poll_id >= (SELECT MAX(poll_id) - {polls} FROM src.poll)
      AND c.stop_ref IS NOT NULL AND c.order_no IS NOT NULL
), sized AS (
    SELECT line_ref, direction, journey_ref, operating_date,
           COUNT(DISTINCT order_no) AS stops
    FROM recent GROUP BY ALL
), best AS (
    SELECT * EXCLUDE (rn) FROM (
        SELECT *, row_number() OVER (
            PARTITION BY line_ref, direction ORDER BY stops DESC) AS rn
        FROM sized
    ) WHERE rn = 1
), ordered AS (
    SELECT DISTINCT b.line_ref, b.direction, r.order_no, r.stop_ref
    FROM best b
    JOIN recent r
      ON r.line_ref = b.line_ref AND r.direction IS NOT DISTINCT FROM b.direction
     AND r.journey_ref = b.journey_ref
     AND r.operating_date IS NOT DISTINCT FROM b.operating_date
)
SELECT line_ref, direction, list(stop_ref ORDER BY order_no) AS path
FROM ordered GROUP BY line_ref, direction
"""


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


def network(conn, db_path=DB_PATH, recent_polls=600):
    """The lines as they actually run: one path of real positions per route.

    Drawn from the archive rather than from a schematic, so the shape on
    screen is the shape on the ground. Each line and direction contributes the
    longest journey pattern seen recently, which is the full route rather than
    a short-turn variant.
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
    meta = {
        ref: (mode or "bus", code, colour)
        for ref, mode, code, colour in conn.execute(
            "SELECT line_ref, mode, public_code, colour FROM line"
        )
    }

    duck = duckdb.connect()
    try:
        duck.execute("SET memory_limit='1GB'")
        duck.execute("INSTALL sqlite; LOAD sqlite;")
        duck.execute(f"ATTACH '{db_path}' AS src (TYPE sqlite, READ_ONLY)")
        found = duck.execute(ROUTES_SQL.format(polls=recent_polls)).fetchall()
    finally:
        duck.close()

    used, stops = {}, []

    def idx(ref):
        if ref not in used:
            lon, lat, name = quays[ref]
            used[ref] = len(stops)
            stops.append([lon, lat, name])
        return used[ref]

    routes, seen = [], set()
    for line_ref, direction, path in found:
        mode, code, colour = meta.get(line_ref, ("bus", None, None))
        points = [idx(r) for r in path if r in quays]
        if len(points) < 2:
            continue
        key = (line_ref, tuple(points))
        if key in seen:
            continue
        seen.add(key)
        routes.append({
            "line": code or (line_ref or "").split(":")[-1],
            "mode": mode,
            "colour": (METRO_COLOURS.get(code) if mode == "metro" else None)
                      or (f"#{colour}" if colour else None)
                      or MODE_COLOURS.get(mode, "#33404F"),
            "weight": MODE_WEIGHT.get(mode, 0.7),
            "path": points,
        })
    routes.sort(key=lambda r: MODE_WEIGHT.get(r["mode"], 0))
    return {"stops": stops, "routes": routes}


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
    heading_for = {}  # the last stop on the journey, which is what a sign would say
    for row in rows:
        key = (row["journey_ref"], row["operating_date"])
        current = by_journey.get(key)
        # Several polls now feed in, because each one is only a delta. The
        # newest sighting of a journey wins outright, and within that sighting
        # the nearest stop ahead is where the vehicle is. Taking the lowest
        # stop number across all polls would put vehicles back at stations
        # they left ten minutes ago.
        rank = (row["polled_at"], -row["order_no"])
        if current is None or rank > (current["polled_at"], -current["order_no"]):
            by_journey[key] = row
        tail = heading_for.get(key)
        if tail is None or row["order_no"] > tail[0]:
            heading_for[key] = (row["order_no"], row["stop_name"])

    origins = _origin_stops(conn, list(by_journey.values()))
    out = []
    for key, row in by_journey.items():
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
            # Where it is now, where it is heading, and how long it has to get
            # there, so the page can keep moving it between exports instead of
            # jumping once a minute.
            "lon": round(source[0] + (target[0] - source[0]) * fraction, 5),
            "lat": round(source[1] + (target[1] - source[1]) * fraction, 5),
            "tlon": round(target[0], 5),
            "tlat": round(target[1], 5),
            "eta": round(row["horizon_sec"]),
            "line": (row["line_ref"] or "").split(":")[-1],
            "mode": modes.get(row["line_ref"], "bus"),
            "stop": row["stop_name"],
            # Destination as the sign would show it; the vehicle's own next
            # stop when the journey has no later calls left in the poll.
            "dest": heading_for[key][1] or row["stop_name"],
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
        # What the model actually leans on, for the site's method page.
        # Older metas predate the field; the page just skips the figure then.
        "importance": meta.get("importance"),
        # Rows per codespace, so the page can say how the measurement is
        # composed rather than repeat a ratio somebody typed once.
        "operators": meta.get("operators"),
    }


def archive(conn, parquet_dir=PARQUET_DIR):
    """The size of what has been collected. Part of showing the work."""
    hot = conn.execute("SELECT COUNT(*) FROM call_snapshot").fetchone()[0]
    polls, first, last = conn.execute(
        "SELECT COUNT(*), MIN(polled_at), MAX(polled_at) FROM poll WHERE feed = 'et'"
    ).fetchone()
    cold = 0
    try:
        import duckdb

        files = _parquet_files(parquet_dir, "calls")
        if files:
            con = duckdb.connect()
            cold = con.execute(
                f"SELECT COUNT(*) FROM read_parquet({files!r})").fetchone()[0]
            con.close()
    except Exception:
        cold = 0
    return {
        "calls": hot + cold,
        "polls": polls,
        "since": first,
        "until": last,
        "lines": conn.execute("SELECT COUNT(*) FROM line").fetchone()[0],
        "stops": conn.execute("SELECT COUNT(*) FROM quay").fetchone()[0],
    }


def _parquet_files(parquet_dir, sub):
    from .dataset import _parquet_files as inner

    return inner(parquet_dir, sub)


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
            "archive": archive(conn),
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

    # Credentials live beside the archive, outside the repository.
    from .config import DATA_DIR
    from .publish import load_env

    load_env(Path(DATA_DIR) / "punktlig.env")

    # This runs every ten minutes, so a run that collides with an hour-long
    # replay is simply skipped: the next one is never far away, and two
    # DuckDB jobs at once take the machine down rather than queueing.
    from .joblock import heavy

    with heavy("site", wait=False) as got:
        if not got:
            return 0
        payload = build(out=args.out, model_dir=args.model)

    size = Path(args.out).stat().st_size / 1e6
    print(f"{len(payload['network']['stops'])} stops, "
          f"{len(payload['network']['routes'])} routes, "
          f"{len(payload['vehicles'])} vehicles -> {args.out} ({size:.1f} MB)")

    # The export is state, not source. Publishing it to object storage keeps
    # it out of the repository's history, where ten-minute commits had grown
    # to three quarters of every commit ever made. A machine without
    # credentials still writes its local copy and simply says so.
    from . import publish

    try:
        print(f"published -> {publish.upload(payload)}")
    except publish.NotConfigured as exc:
        print(f"not published: {exc}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
