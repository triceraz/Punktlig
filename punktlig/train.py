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
    "stop_recent_delay_sec", "line_recent_delay_sec",
    "obs_age_sec", "since_last_stop_sec",
]
# Measured as a net wash on the 2026-07-26 day split (helps 20-45 min, hurts
# the shorter horizons). Parked until the archive has enough history for a
# re-measurement; include with --include-parked.
PARKED = [
    "headway_ahead_sec", "delay_ahead_sec",
    # Deviation counts barely move within a day: most open situations are
    # long-running planned works. Collection continues, because incidents
    # will vary once the archive spans more than a couple of days.
    "sx_line_active", "sx_network_active",
]
CATEGORICAL = ["line_ref", "direction", "stop_ref"]
FEATURES = NUMERIC + CATEGORICAL

PARAMS = {
    "objective": "l1",  # optimises MAE directly, robust to delay outliers
    # Measured on a frozen dataset against 0.05/50: slower learning with
    # larger leaves wins once the archive passes a million rows.
    "learning_rate": 0.02,
    "num_leaves": 63,
    "min_data_in_leaf": 200,
    "verbosity": -1,
    "seed": 7,
}


ROW_FILTER = (
    f"ABS(label_delay_sec) < {MAX_ABS_DELAY} AND horizon_sec < {MAX_HORIZON}"
    " AND entur_pred_delay_sec IS NOT NULL AND current_delay_sec IS NOT NULL"
)


def load_rows(dataset_path):
    conn = db.connect(dataset_path)
    sql = f"""
        SELECT {', '.join(FEATURES)}, label_delay_sec, entur_pred_delay_sec, operating_date
        FROM training_row
        WHERE {ROW_FILTER}
        ORDER BY polled_at
    """
    return conn.execute(sql).fetchall()


def operator_rows(dataset_path):
    """Usable training rows per codespace, e.g. {"RUT": 20270400, "VYG": ...}.

    The codespace is the prefix of the line reference, which is how Entur
    names the source of a stream: RUT is Ruter, VYG is Vy, GOA Go-Ahead,
    FLT Flytoget, SJN SJ.
    """
    conn = db.connect(dataset_path)
    try:
        return {
            prefix: count
            for prefix, count in conn.execute(
                "SELECT substr(line_ref, 1, instr(line_ref, ':') - 1) AS codespace, "
                f"COUNT(*) FROM training_row WHERE {ROW_FILTER} "
                "GROUP BY 1 ORDER BY 2 DESC"
            )
            if prefix
        }
    finally:
        conn.close()


def load_matrix(dataset_path, valid_days, valid_on=None):
    """Stream the dataset straight into float32 matrices.

    `load_rows` materialises every row as a tuple of Python objects, which
    costs several hundred bytes per row once object headers are counted; at
    eighteen million rows that is more memory than the machine has. Here the
    only per-row Python work is copying values into preallocated arrays, so
    the whole dataset costs rows times features times four bytes and nothing
    else grows with it.

    The day split moves into SQL: validation dates are found first, the
    vocabularies are built from the other days only, and one ordered scan
    delivers training rows before validation rows, both in time order.
    Returns (X, y, entur, split, vocabs, valid_dates, n) where valid_dates is
    empty when the archive spans too few days to hold one out.
    """
    conn = db.connect(dataset_path)
    try:
        dates = [d for (d,) in conn.execute(
            f"SELECT DISTINCT operating_date FROM training_row WHERE {ROW_FILTER}"
            " ORDER BY 1")]
        if valid_on:
            # Named days must exist, or the split silently validates on
            # nothing and every reported number is the training error.
            missing = [d for d in valid_on if d not in dates]
            if missing:
                raise SystemExit(
                    f"no rows for {', '.join(missing)}; the dataset holds "
                    f"{', '.join(str(d) for d in dates)}"
                )
            valid_dates = list(valid_on)
        else:
            valid_dates = dates[-valid_days:] if len(dates) > valid_days else []
        quoted = ", ".join(f"'{d}'" for d in valid_dates) or "''"
        is_valid = f"(operating_date IN ({quoted}))"

        vocabs = {}
        for col in CATEGORICAL:
            values = [v for (v,) in conn.execute(
                f"SELECT DISTINCT {col} FROM training_row"
                f" WHERE {ROW_FILTER} AND NOT {is_valid} AND {col} IS NOT NULL"
                " ORDER BY 1")]
            vocabs[col] = {v: i + 1 for i, v in enumerate(values)}  # 0 stays "unknown"

        n = conn.execute(
            f"SELECT COUNT(*) FROM training_row WHERE {ROW_FILTER}").fetchone()[0]
        split = conn.execute(
            f"SELECT COUNT(*) FROM training_row WHERE {ROW_FILTER} AND NOT {is_valid}"
        ).fetchone()[0]

        X = np.full((n, len(FEATURES)), np.nan, dtype=np.float32)
        y = np.empty(n, dtype=np.float32)
        entur = np.empty(n, dtype=np.float32)
        # The codespace each row came from, carried alongside so the result
        # can be broken down by source without a second pass over the data.
        # It is read as its own column rather than recovered from the line_ref
        # vocabulary, whose indices say nothing about which operator a line
        # belongs to.
        sources = np.empty(n, dtype=object)
        cursor = conn.execute(f"""
            SELECT {', '.join(FEATURES)}, label_delay_sec, entur_pred_delay_sec,
                   substr(line_ref, 1, instr(line_ref, ':') - 1)
            FROM training_row
            WHERE {ROW_FILTER}
            ORDER BY {is_valid}, polled_at
        """)
        n_numeric = len(NUMERIC)
        i = 0
        while True:
            batch = cursor.fetchmany(50_000)
            if not batch:
                break
            for row in batch:
                for j in range(n_numeric):
                    if row[j] is not None:
                        X[i, j] = row[j]
                for j, col in enumerate(CATEGORICAL):
                    X[i, n_numeric + j] = vocabs[col].get(row[n_numeric + j], 0)
                y[i] = row[-3]
                entur[i] = row[-2]
                sources[i] = row[-1] or "?"
                i += 1
        return X, y, entur, split, vocabs, valid_dates, n, sources
    finally:
        conn.close()


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


def evaluate(X, split, y, entur, pred):
    """Validation MAE per horizon bucket: all baselines plus the model."""
    horizon = X[:, FEATURES.index("horizon_sec")].astype(float)
    current = X[:, FEATURES.index("current_delay_sec")].astype(float)
    print(f"\n{'horizon':>10} | {'n':>7} | {'timetable':>9} | {'naive':>9} | {'entur':>9} | {'model':>9}")
    print("-" * 70)
    summary = {}
    for lo, hi in BUCKETS:
        mask = (horizon[split:] >= lo * 60) & (horizon[split:] < hi * 60)
        n = int(mask.sum())
        if not n:
            continue
        yv = y[split:][mask]
        # Plain floats: numpy's float32 survives arithmetic and printing but
        # refuses json, which cost a finished training run its meta file once.
        stats = {
            "n": n,
            "timetable": float(mae(list(np.abs(yv)))),
            "naive": float(mae(list(np.abs(yv - current[split:][mask])))),
            "entur": float(mae(list(np.abs(yv - entur[split:][mask])))),
            "model": float(mae(list(np.abs(yv - pred[mask])))),
        }
        summary[f"{lo}-{hi}min"] = stats
        print(
            f"{lo:>3}-{hi:<3}min | {n:>7} | {fmt(stats['timetable'])} | "
            f"{fmt(stats['naive'])} | {fmt(stats['entur'])} | {fmt(stats['model'])}"
        )
    print("\nValidation slice only (latest 20% of events). MAE in seconds.")
    return summary


TRAIN_CODESPACES = {"VYG", "GOA", "SJN", "FLT"}
SOURCE_NAMES = {"RUT": "Ruter", "VYG": "Vy", "GOA": "Go-Ahead",
                "FLT": "Flytoget", "SJN": "SJ"}


def by_source(y, entur, pred, current, sources, split):
    """The same comparison, split by which operator's stream the row came from.

    Trains are a small share of the rows and are polled a fifth as often, so
    they can move the weighted figure hardly at all while behaving quite
    differently underneath it. Reporting the split is the difference between
    knowing that and guessing at it.
    """
    valid = sources[split:]
    out = {}
    print(f"\n{'source':>10} | {'n':>8} | {'naive':>9} | {'entur':>9} | {'model':>9}")
    print("-" * 58)
    for code in sorted(set(valid), key=lambda c: -(valid == c).sum()):
        mask = valid == code
        n = int(mask.sum())
        if n < 1000:
            continue
        stats = {
            "n": n,
            "naive": float(mae(list(np.abs(y[split:][mask] - current[split:][mask])))),
            "entur": float(mae(list(np.abs(y[split:][mask] - entur[split:][mask])))),
            "model": float(mae(list(np.abs(y[split:][mask] - pred[mask])))),
        }
        out[code] = stats
        label = SOURCE_NAMES.get(code, code)
        print(f"{label:>10} | {n:>8} | {fmt(stats['naive'])} | "
              f"{fmt(stats['entur'])} | {fmt(stats['model'])}")

    rail = [c for c in out if c in TRAIN_CODESPACES]
    if rail and "RUT" in out:
        total = sum(out[c]["n"] for c in rail)
        blend = lambda key: sum(out[c][key] * out[c]["n"] for c in rail) / total
        print(f"\n  trains together: {total} rows, "
              f"naive {blend('naive'):.1f}s, entur {blend('entur'):.1f}s, "
              f"model {blend('model'):.1f}s")
    return out


def main(argv=None):
    parser = argparse.ArgumentParser(description="Train LightGBM on the replay dataset")
    parser.add_argument("--dataset", default=str(DATASET_PATH))
    parser.add_argument("--out", default=str(MODEL_DIR))
    parser.add_argument("--valid-days", type=int, default=1,
                        help="validate on the last N operating dates")
    parser.add_argument("--valid-date", action="append", metavar="YYYY-MM-DD",
                        help="validate on this operating date instead of the "
                             "last one. The newest date is often a part-day, "
                             "which makes for a small and hour-skewed "
                             "validation set; naming a complete day gives a "
                             "number worth comparing. Repeatable")
    parser.add_argument("--exclude", default="",
                        help="comma-separated features to drop, for ablation runs "
                             "on identical data (horizon_sec cannot be dropped)")
    parser.add_argument("--include-parked", action="store_true",
                        help="re-measure with the parked features included")
    parser.add_argument("--with-entur", action="store_true",
                        help="blending variant: add Entur's own prediction as a "
                             "feature; the default model stays independent of it")
    parser.add_argument("--alpha", type=float,
                        help="fit this quantile of the delay instead of its median. "
                             "Above 0.5 the model leans towards announcing a later "
                             "arrival, which is the failure passengers mind least")
    args = parser.parse_args(argv)

    if args.alpha is not None:
        PARAMS.update(objective="quantile", alpha=args.alpha)
        print(f"objective: quantile at alpha {args.alpha:g}")

    if args.include_parked:
        NUMERIC.extend(PARKED)
        FEATURES[:] = NUMERIC + CATEGORICAL
    if args.with_entur:
        NUMERIC.append("entur_pred_delay_sec")
        FEATURES[:] = NUMERIC + CATEGORICAL

    if args.exclude:
        dropped = {f.strip() for f in args.exclude.split(",") if f.strip()}
        if "horizon_sec" in dropped:
            print("horizon_sec is needed for bucketing and cannot be excluded")
            return 1
        NUMERIC[:] = [f for f in NUMERIC if f not in dropped]
        CATEGORICAL[:] = [f for f in CATEGORICAL if f not in dropped]
        FEATURES[:] = NUMERIC + CATEGORICAL
        print(f"ablation: excluded {', '.join(sorted(dropped))}")

    # One fit at a time: two of these together hold nine gigabytes of feature
    # matrix between them. They read plain SQLite, so the site export is free
    # to keep publishing meanwhile.
    from .joblock import FITTING_LOCK, heavy

    with heavy("train", name=FITTING_LOCK):
        return _train(args)


def _train(args):
    X, y, entur, split, vocabs, valid_dates, n, sources = load_matrix(
        args.dataset, args.valid_days, args.valid_date)
    if n < 1000:
        print(f"only {n} usable rows; collect more data before training")
        return 1
    if valid_dates:
        print(f"day split: validating on {', '.join(valid_dates)}")
    else:
        split = int(n * 0.8)
        print(
            "WARNING: dataset spans too few operating dates for a day split; "
            "falling back to an 80/20 time split. Same-day validation shares "
            "conditions with training, so treat these numbers as optimistic."
        )
    print(f"rows: {n} (train {split}, valid {n - split})")

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
    summary = evaluate(X, split, y, entur, pred)
    per_source = by_source(y, entur, pred, X[:, FEATURES.index("current_delay_sec")],
                           sources, split)

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
                "rows": n,
                "features": FEATURES,
                "vocabs": vocabs,
                "validation": summary,
                # How the measurement is composed, so the site can state the
                # mix instead of asserting a remembered ratio. The page said
                # "nine in ten" while the real share was ninety-nine in a
                # hundred, which is the failure mode of writing a number into
                # copy and letting the data move underneath it.
                "operators": operator_rows(args.dataset),
                # The same comparison per operator, so the trains can be read
                # separately from the buses they are averaged into.
                "by_source": per_source,
                # Share of total gain per feature, so the site can show what
                # the model actually leans on rather than a hand-written list.
                "importance": {
                    name: round(float(gain) / max(1.0, float(sum(g for _, g in gains))), 4)
                    for name, gain in gains
                },
            },
            indent=2,
        )
    )
    print(f"\nmodel saved to {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
