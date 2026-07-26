"""Train the first model: LightGBM gradient boosted trees on the replayed dataset.

The split is time-based, never random: train on the earliest 80 percent of
prediction events, validate on the latest 20 percent. A random split would
leak, because rows from the same journey seconds apart would land on both
sides of the split and the model would be graded on near-duplicates of its
own training data.

Categorical vocabularies are also built from the training slice only; values
first seen in validation map to an unknown bucket, exactly as a truly new
line or stop would in production.
"""

import argparse
import json
import sys
from datetime import datetime, timezone

import lightgbm as lgb
import numpy as np

from . import db
from .config import DATA_DIR
from .dataset import OUT_PATH as DATASET_PATH
from .report import BUCKETS, MAX_ABS_DELAY, MAX_HORIZON, fmt, mae

MODEL_DIR = DATA_DIR / "model"

NUMERIC = [
    "horizon_sec", "horizon_stops", "order_no", "dow", "hour",
    "current_delay_sec", "delay_trend_sec", "n_recorded",
    "fc_air_temp", "fc_precip_mm", "fc_wind_mps",
    "sched_runtime_sec", "seg_slack_sec",
]
CATEGORICAL = ["line_ref", "direction", "stop_ref"]
FEATURES = NUMERIC + CATEGORICAL

PARAMS = {
    "objective": "l1",  # optimises MAE directly, robust to delay outliers
    "learning_rate": 0.05,
    "num_leaves": 63,
    "min_data_in_leaf": 50,
    "verbosity": -1,
    "seed": 7,
}


def load_rows(dataset_path):
    conn = db.connect(dataset_path)
    sql = f"""
        SELECT {', '.join(FEATURES)}, label_delay_sec, entur_pred_delay_sec, operating_date
        FROM training_row
        WHERE ABS(label_delay_sec) < {MAX_ABS_DELAY} AND horizon_sec < {MAX_HORIZON}
          AND entur_pred_delay_sec IS NOT NULL AND current_delay_sec IS NOT NULL
        ORDER BY polled_at
    """
    return conn.execute(sql).fetchall()


def day_split(rows, valid_days, date_of=lambda r: r[-1]):
    """Journey-atomic split: the last `valid_days` operating dates become validation.

    Splitting on operating date rather than poll time guarantees that no
    journey contributes rows to both sides, so a journey running across the
    boundary cannot leak validation-period outcomes into training.
    Returns (reordered_rows, split_index, validation_dates), or None when the
    dataset does not span enough dates for the split to mean anything.
    """
    dates = sorted({date_of(r) for r in rows})
    if len(dates) <= valid_days:
        return None
    valid_dates = set(dates[-valid_days:])
    train_rows = [r for r in rows if date_of(r) not in valid_dates]
    valid_rows = [r for r in rows if date_of(r) in valid_dates]
    return train_rows + valid_rows, len(train_rows), sorted(valid_dates)


def encode(rows, split):
    """Rows -> float matrix. Vocabularies come from the training slice only."""
    vocabs = {c: {} for c in CATEGORICAL}
    for row in rows[:split]:
        for j, col in enumerate(CATEGORICAL):
            value = row[len(NUMERIC) + j]
            if value is not None and value not in vocabs[col]:
                vocabs[col][value] = len(vocabs[col]) + 1  # 0 stays "unknown"

    X = np.full((len(rows), len(FEATURES)), np.nan)
    for i, row in enumerate(rows):
        for j in range(len(NUMERIC)):
            if row[j] is not None:
                X[i, j] = row[j]
        for j, col in enumerate(CATEGORICAL):
            X[i, len(NUMERIC) + j] = vocabs[col].get(row[len(NUMERIC) + j], 0)
    y = np.array([row[-3] for row in rows], dtype=float)
    entur = np.array([row[-2] for row in rows], dtype=float)
    return X, y, entur, vocabs


def evaluate(rows, split, y, entur, pred):
    """Validation MAE per horizon bucket: all baselines plus the model."""
    horizon = np.array([row[0] for row in rows], dtype=float)
    current = np.array([row[NUMERIC.index("current_delay_sec")] for row in rows], dtype=float)
    print(f"\n{'horizon':>10} | {'n':>7} | {'timetable':>9} | {'naive':>9} | {'entur':>9} | {'model':>9}")
    print("-" * 70)
    summary = {}
    for lo, hi in BUCKETS:
        mask = (horizon[split:] >= lo * 60) & (horizon[split:] < hi * 60)
        n = int(mask.sum())
        if not n:
            continue
        yv = y[split:][mask]
        stats = {
            "n": n,
            "timetable": mae(list(np.abs(yv))),
            "naive": mae(list(np.abs(yv - current[split:][mask]))),
            "entur": mae(list(np.abs(yv - entur[split:][mask]))),
            "model": mae(list(np.abs(yv - pred[mask]))),
        }
        summary[f"{lo}-{hi}min"] = stats
        print(
            f"{lo:>3}-{hi:<3}min | {n:>7} | {fmt(stats['timetable'])} | "
            f"{fmt(stats['naive'])} | {fmt(stats['entur'])} | {fmt(stats['model'])}"
        )
    print("\nValidation slice only (latest 20% of events). MAE in seconds.")
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description="Train LightGBM on the replay dataset")
    parser.add_argument("--dataset", default=str(DATASET_PATH))
    parser.add_argument("--out", default=str(MODEL_DIR))
    parser.add_argument("--valid-days", type=int, default=1,
                        help="validate on the last N operating dates")
    parser.add_argument("--exclude", default="",
                        help="comma-separated features to drop, for ablation runs "
                             "on identical data (horizon_sec cannot be dropped)")
    args = parser.parse_args(argv)

    if args.exclude:
        dropped = {f.strip() for f in args.exclude.split(",") if f.strip()}
        if "horizon_sec" in dropped:
            print("horizon_sec is needed for bucketing and cannot be excluded")
            return 1
        NUMERIC[:] = [f for f in NUMERIC if f not in dropped]
        CATEGORICAL[:] = [f for f in CATEGORICAL if f not in dropped]
        FEATURES[:] = NUMERIC + CATEGORICAL
        print(f"ablation: excluded {', '.join(sorted(dropped))}")

    rows = load_rows(args.dataset)
    if len(rows) < 1000:
        print(f"only {len(rows)} usable rows; collect more data before training")
        return 1

    result = day_split(rows, args.valid_days)
    if result:
        rows, split, valid_dates = result
        print(f"day split: validating on {', '.join(valid_dates)}")
    else:
        split = int(len(rows) * 0.8)
        print(
            "WARNING: dataset spans too few operating dates for a day split; "
            "falling back to an 80/20 time split. Same-day validation shares "
            "conditions with training, so treat these numbers as optimistic."
        )
    X, y, entur, vocabs = encode(rows, split)
    print(f"rows: {len(rows)} (train {split}, valid {len(rows) - split})")

    cat_idx = [FEATURES.index(c) for c in CATEGORICAL]
    dtrain = lgb.Dataset(
        X[:split], label=y[:split], feature_name=FEATURES,
        categorical_feature=cat_idx, free_raw_data=False,
    )
    dvalid = dtrain.create_valid(X[split:], label=y[split:])
    booster = lgb.train(
        PARAMS, dtrain, num_boost_round=2000, valid_sets=[dvalid],
        callbacks=[lgb.early_stopping(100, verbose=False), lgb.log_evaluation(0)],
    )
    print(f"best iteration: {booster.best_iteration}")

    pred = booster.predict(X[split:], num_iteration=booster.best_iteration)
    summary = evaluate(rows, split, y, entur, pred)

    gains = sorted(
        zip(FEATURES, booster.feature_importance("gain")), key=lambda p: -p[1]
    )
    print("\ntop features by gain:")
    for name, gain in gains[:8]:
        print(f"  {name:<20} {gain:12.0f}")

    out_dir = MODEL_DIR if args.out == str(MODEL_DIR) else __import__("pathlib").Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    booster.save_model(str(out_dir / "punktlig-lgbm.txt"), num_iteration=booster.best_iteration)
    (out_dir / "punktlig-lgbm.meta.json").write_text(
        json.dumps(
            {
                "trained_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "rows": len(rows),
                "features": FEATURES,
                "vocabs": vocabs,
                "validation": summary,
            },
            indent=2,
        )
    )
    print(f"\nmodel saved to {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
