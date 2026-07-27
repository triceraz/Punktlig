"""Prediction intervals: how late might this actually be?

The point model answers "how late will it be". This answers "how late could
it plausibly be", by fitting the same features to three quantiles of the
delay distribution instead of its middle. The official feed publishes a
single number and no uncertainty at all, so this is a capability it does
not have rather than an improvement on one it does.

Two things are reported. Pinball loss is the objective each quantile model
minimises, and it is the honest way to compare quantile predictions.
Coverage is the check that matters to a passenger: of all the arrivals, how
many actually landed inside the interval we drew. An interval that claims
80 percent and holds 55 is decoration, not uncertainty.
"""

import argparse
import json
import sys
from datetime import datetime, timezone

import lightgbm as lgb
import numpy as np

from .config import DATA_DIR
from .dataset import OUT_PATH as DATASET_PATH
from .report import BUCKETS, fmt
from .train import CATEGORICAL, FEATURES, PARAMS, day_split, encode, load_rows

MODEL_DIR = DATA_DIR / "model-quantiles"
QUANTILES = (0.1, 0.5, 0.9)


def pinball(y, pred, alpha):
    """Mean pinball loss: under-prediction costs alpha, over-prediction 1-alpha."""
    total = 0.0
    for actual, guess in zip(y, pred):
        diff = actual - guess
        total += alpha * diff if diff >= 0 else (alpha - 1) * diff
    return total / len(y) if y else None


def coverage(y, lo, hi):
    """Share of actual values that landed inside the interval, bounds included."""
    if not len(y):
        return None
    inside = sum(1 for actual, a, b in zip(y, lo, hi) if a <= actual <= b)
    return inside / len(y)


def train_quantiles(dataset_path, valid_days=1, quantiles=QUANTILES):
    rows = load_rows(dataset_path)
    result = day_split(rows, valid_days)
    if result:
        rows, split, valid_dates = result
        print(f"day split: validating on {', '.join(valid_dates)}")
    else:
        split = int(len(rows) * 0.8)
        print("WARNING: too few operating dates for a day split; using 80/20")

    X, y, _, vocabs = encode(rows, split)
    cat_idx = [FEATURES.index(c) for c in CATEGORICAL]
    dtrain = lgb.Dataset(X[:split], label=y[:split], feature_name=FEATURES,
                         categorical_feature=cat_idx, free_raw_data=False)
    dvalid = dtrain.create_valid(X[split:], label=y[split:])

    preds, boosters = {}, {}
    for alpha in quantiles:
        params = dict(PARAMS, objective="quantile", alpha=alpha)
        booster = lgb.train(params, dtrain, num_boost_round=2000, valid_sets=[dvalid],
                            callbacks=[lgb.early_stopping(100, verbose=False),
                                       lgb.log_evaluation(0)])
        preds[alpha] = booster.predict(X[split:], num_iteration=booster.best_iteration)
        boosters[alpha] = booster
        print(f"  q{alpha:g}: best iteration {booster.best_iteration}")

    return rows, split, X, y, preds, boosters, vocabs


def evaluate(rows, split, y, preds, quantiles=QUANTILES):
    """Coverage and interval width per horizon, plus pinball loss per quantile."""
    horizon = np.array([r[0] for r in rows], dtype=float)[split:]
    yv = y[split:]
    lo, hi = preds[min(quantiles)], preds[max(quantiles)]
    nominal = (max(quantiles) - min(quantiles)) * 100

    print(f"\n{'horizon':>10} | {'n':>7} | {'coverage':>9} | {'target':>7} | "
          f"{'width':>9} | {'median err':>10}")
    print("-" * 68)
    summary = {}
    for a, b in BUCKETS:
        m = (horizon >= a * 60) & (horizon < b * 60)
        if not m.sum():
            continue
        cov = coverage(yv[m], lo[m], hi[m])
        width = float(np.mean(hi[m] - lo[m]))
        med_err = float(np.mean(np.abs(yv[m] - preds[0.5][m])))
        summary[f"{a}-{b}min"] = {"n": int(m.sum()), "coverage": cov,
                                  "width_sec": width, "median_mae": med_err}
        print(f"{a:>3}-{b:<3}min | {int(m.sum()):>7} | {cov * 100:>8.1f}% | "
              f"{nominal:>6.0f}% | {fmt(width)} | {fmt(med_err)}")

    print(f"\noverall coverage {coverage(yv, lo, hi) * 100:.1f}% against a "
          f"{nominal:.0f}% target")
    for alpha in quantiles:
        print(f"  pinball q{alpha:g}: {pinball(list(yv), list(preds[alpha]), alpha):.2f}")
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description="Train quantile models for arrival intervals")
    parser.add_argument("--dataset", default=str(DATASET_PATH))
    parser.add_argument("--out", default=str(MODEL_DIR))
    parser.add_argument("--valid-days", type=int, default=1)
    args = parser.parse_args(argv)

    rows, split, _, y, preds, boosters, vocabs = train_quantiles(
        args.dataset, valid_days=args.valid_days
    )
    print(f"rows: {len(rows)} (train {split}, valid {len(rows) - split})")
    summary = evaluate(rows, split, y, preds)

    from pathlib import Path

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    for alpha, booster in boosters.items():
        booster.save_model(str(out_dir / f"punktlig-q{alpha:g}.txt"),
                           num_iteration=booster.best_iteration)
    (out_dir / "punktlig-quantiles.meta.json").write_text(
        json.dumps({"trained_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "quantiles": list(QUANTILES), "features": FEATURES,
                    "vocabs": vocabs, "validation": summary}, indent=2)
    )
    print(f"\nmodels saved to {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
