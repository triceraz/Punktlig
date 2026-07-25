"""First-look evaluation: how good are the baselines on the archive so far?

Three baselines, no ML yet:
  timetable: pretend delay is always 0 (ignore realtime entirely)
  naive:     assume the current delay persists unchanged to every future stop
  entur:     the official expected times published in the feed

If 'entur' tracks 'naive' closely, the core hypothesis of the project
(official predictions are mostly naive propagation) holds empirically.
"""

import sys

from . import db
from .dataset import OUT_PATH

BUCKETS = [(0, 5), (5, 10), (10, 20), (20, 45)]

# Guard rails against data glitches (a 2h "delay" is a data error, not traffic).
MAX_ABS_DELAY = 3600
MAX_HORIZON = 45 * 60


def mae(errors):
    return sum(errors) / len(errors) if errors else None


def fmt(value):
    return f"{value:7.1f}s" if value is not None else "      --"


def main():
    conn = db.connect(OUT_PATH)
    rows = conn.execute(
        """
        SELECT horizon_sec, label_delay_sec, entur_pred_delay_sec, current_delay_sec
        FROM training_row
        WHERE ABS(label_delay_sec) < ? AND horizon_sec < ?
          AND entur_pred_delay_sec IS NOT NULL AND current_delay_sec IS NOT NULL
        """,
        (MAX_ABS_DELAY, MAX_HORIZON),
    ).fetchall()

    if not rows:
        print("no labelled rows yet; let the collector run longer")
        return 1

    print(f"labelled rows: {len(rows)}\n")
    print(f"{'horizon':>10} | {'n':>7} | {'timetable':>9} | {'naive':>9} | {'entur':>9}")
    print("-" * 58)
    for lo, hi in BUCKETS:
        sub = [r for r in rows if lo * 60 <= r[0] < hi * 60]
        if not sub:
            continue
        e_timetable = mae([abs(r[1]) for r in sub])
        e_naive = mae([abs(r[1] - r[3]) for r in sub])
        e_entur = mae([abs(r[1] - r[2]) for r in sub])
        print(
            f"{lo:>3}-{hi:<3}min | {len(sub):>7} | {fmt(e_timetable)} | {fmt(e_naive)} | {fmt(e_entur)}"
        )

    print("\nMAE (mean absolute error) in seconds. Lower is better.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
