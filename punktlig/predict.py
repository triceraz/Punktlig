"""Predict arrivals for the vehicles that are running right now.

The archive is at most a poll old, so the newest snapshot in it is a good
enough picture of the network to predict from, and reading it beats asking
the feed again: a second requestorId would consume the collector's own
delta stream.

Features come from `dataset.iter_rows`, the same generator that builds
training rows. That is the point of this module: whatever the model was
trained on, it is served exactly that, with the label left empty because
the answer has not happened yet.
"""

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import db
from .config import DB_PATH, PARQUET_DIR
from .dataset import (
    HistoryIndex,
    ROW_COLS,
    SituationIndex,
    WeatherIndex,
    _open_sources,
    _situation_rows,
    iter_rows,
)

MODEL_DIR = Path(DB_PATH).parent / "model"


# The feed is polled with a requestorId, so most responses are deltas: only
# the journeys whose estimates changed since last time. A single poll is
# therefore not a picture of the network, it is a list of what moved, and
# reading only the newest one showed 34 vehicles on a map that should have
# had four hundred. Recent polls are stitched together instead, newest value
# winning per journey, which is what the delta stream is meant to be used for.
RECENT_POLLS_MINUTES = 20


def latest_poll(conn, dataset=None):
    """The newest successful ET poll, optionally within one codespace."""
    sql = ("SELECT poll_id, polled_at, dataset FROM poll "
           "WHERE feed = 'et' AND error IS NULL AND n_calls > 0")
    params = []
    if dataset:
        sql += " AND dataset = ?"
        params.append(dataset)
    return conn.execute(sql + " ORDER BY polled_at DESC LIMIT 1", params).fetchone()


def recent_polls(conn, dataset, minutes=RECENT_POLLS_MINUTES):
    """Poll ids for one codespace within `minutes` of its newest poll.

    Anchored to the newest poll rather than to the clock, so a gap in
    collection yields the last complete picture instead of nothing at all.
    """
    newest = latest_poll(conn, dataset)
    if not newest:
        return []
    # The cutoff is computed here rather than by SQLite's datetime(), which
    # returns "2026-08-03 17:37:18" while the column holds
    # "2026-08-03T17:37:18.123456+00:00". Compared as text the T sorts after
    # the space, so every row of the same date passed the filter and polls
    # four hours old were treated as current traffic.
    cutoff = (datetime.fromisoformat(newest[1])
              - timedelta(minutes=minutes)).isoformat()
    # An unrecorded call count is not the same as an empty poll: rows written
    # before the counter existed, and rows repaired by reparse, carry NULL.
    # A genuinely empty poll costs nothing here, since it contributes no rows.
    sql = ("SELECT poll_id FROM poll WHERE feed = 'et' AND error IS NULL "
           "AND COALESCE(n_calls, 1) > 0 AND polled_at > ?")
    params = [cutoff]
    if dataset:  # absent means every codespace, as it does for latest_poll
        sql += " AND dataset = ?"
        params.append(dataset)
    return [row[0] for row in conn.execute(sql + " ORDER BY polled_at", params)]


def upcoming_rows(archive_path=DB_PATH, parquet_dir=PARQUET_DIR, dataset=None,
                  datasets=None):
    """Feature rows for every stop still ahead of every running vehicle.

    `datasets` asks for several codespaces at once, which matters because the
    history indexes are built from the whole archive: doing that once for five
    codespaces rather than five times is the difference between a gigabyte and
    a wedged machine.
    """
    wanted = list(datasets) if datasets else [dataset]
    conn = db.connect(archive_path)
    try:
        poll_ids = []
        for name in wanted:
            poll_ids.extend(recent_polls(conn, name))
        if not poll_ids:
            return []
        marks = ", ".join("?" for _ in poll_ids)
        cursor = conn.execute(
            "SELECT c.journey_ref, c.operating_date, c.poll_id, p.polled_at, "
            "       c.line_ref, c.direction, c.call_type, c.stop_ref, c.stop_name, "
            "       c.order_no, c.aimed_arr, c.expected_arr, c.actual_arr, "
            "       c.aimed_dep, c.expected_dep, c.actual_dep, "
            "       c.cancelled, c.call_cancelled, c.recorded_at "
            "FROM call_snapshot c JOIN poll p ON p.poll_id = c.poll_id "
            f"WHERE c.poll_id IN ({marks}) "
            "AND c.journey_ref IS NOT NULL AND c.order_no IS NOT NULL "
            "ORDER BY c.journey_ref, c.operating_date, p.polled_at, c.poll_id, c.order_no",
            poll_ids,
        ).fetchall()
    finally:
        conn.close()

    # History and weather still come from the whole archive: the features
    # are as-of-now, and now is the newest snapshot's own time. DuckDB does
    # the aggregation in well under a minute where the Python pre-pass grew
    # past the site task's ten-minute cadence and got itself terminated.
    try:
        from .history_sql import SqlHistory

        history = SqlHistory(archive_path, parquet_dir)
    except ImportError:  # duckdb is an analysis extra; answers are identical
        _, history_rows, close_history = _open_sources(archive_path, parquet_dir)
        history = HistoryIndex(history_rows)
        close_history()
    weather_rows, _, close_weather = _open_sources(archive_path, parquet_dir)
    weather = WeatherIndex(weather_rows)
    close_weather()
    situations = SituationIndex(_situation_rows(archive_path))

    return [
        dict(zip(ROW_COLS, row))
        for row in iter_rows(iter(cursor), history, weather, situations, require_label=False)
    ]


def predict(rows, model_dir=None):
    """Attach a model prediction to each row. Requires the analysis extras."""
    import lightgbm as lgb
    import numpy as np

    model_dir = Path(model_dir or MODEL_DIR)
    booster = lgb.Booster(model_file=str(model_dir / "punktlig-lgbm.txt"))
    meta = json.loads((model_dir / "punktlig-lgbm.meta.json").read_text())
    vocabs = meta["vocabs"]

    # The feature list comes from the meta file, not from the training module:
    # training variants add or drop features, and serving a booster with any
    # other column layout than it was fitted on is silently wrong at best.
    features = meta["features"]

    X = np.full((len(rows), len(features)), np.nan)
    for i, row in enumerate(rows):
        for j, name in enumerate(features):
            if name in vocabs:
                X[i, j] = vocabs[name].get(row.get(name), 0)
            else:
                value = row.get(name)
                if value is not None:
                    X[i, j] = value

    for row, value in zip(rows, booster.predict(X)):
        row["model_pred_delay_sec"] = float(value)
    return rows


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Predict arrivals for vehicles running right now"
    )
    parser.add_argument("--dataset", help="limit to one codespace, e.g. RUT")
    parser.add_argument("--line", help="limit to one line reference")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--json", action="store_true", help="print rows as JSON")
    args = parser.parse_args(argv)

    rows = upcoming_rows(dataset=args.dataset)
    if args.line:
        rows = [r for r in rows if r["line_ref"] == args.line]
    if not rows:
        print("no running vehicles with stops ahead in the archive yet")
        return 1

    rows = predict(rows)
    rows.sort(key=lambda r: r["horizon_sec"])
    age = (datetime.now(timezone.utc)
           - datetime.fromisoformat(rows[0]["polled_at"])).total_seconds()
    print(f"{len(rows)} predictions from a snapshot {age:.0f}s old\n")

    if args.json:
        print(json.dumps(rows[:args.limit], indent=2))
        return 0

    print(f"{'line':>14} | {'stop':<24} | {'in':>6} | {'entur':>7} | {'model':>7}")
    print("-" * 70)
    for row in rows[:args.limit]:
        print(f"{row['line_ref'].split(':')[-1]:>14} | {(row['stop_name'] or '')[:24]:<24} | "
              f"{row['horizon_sec'] / 60:>5.1f}m | "
              f"{row['entur_pred_delay_sec']:>6.0f}s | "
              f"{row['model_pred_delay_sec']:>6.0f}s")
    print("\nDelay in seconds against the timetable. Positive is late.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
