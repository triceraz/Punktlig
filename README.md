# Punktlig

Can a machine learning model beat the official Norwegian public transport delay predictions?

Entur publishes realtime "expected" arrival times for all public transport in Norway. Those estimates are largely naive propagation: a vehicle that is 4 minutes late now is assumed to be 4 minutes late at every future stop. That ignores schedule slack, rush-hour dynamics, bunching, shared-tunnel effects and weather, which is most of what actually drives how delays evolve.

Punktlig continuously archives both Entur's live predictions and the eventual ground truth, then trains a model to out-predict the official estimates. The evaluation is honest: no lookahead, measured against a real production baseline.

## Why this dataset is unique

Entur does not publish historical predictions. What the official system expected at 08:00 about an arrival at 08:25 exists only in the moment, unless someone archives it. This collector snapshots every prediction append-only, which makes the archive both the training data and the evaluation baseline:

- Estimated calls are Entur's predictions for stops ahead (the baseline to beat)
- Recorded calls are what actually happened (the labels)
- Every snapshot is timestamped, so backtests can replay history using only information available at prediction time

## Architecture

```
Entur SIRI-ET  (delta poll ~1 min) ──┐
Entur SIRI-SX  (deviations, hourly) ─┼─→ append-only archive (SQLite) ─→ dataset builder ─→ model ─→ backtest vs. Entur
MET Norway     (forecasts, hourly) ──┘
```

Zero runtime dependencies, pure Python standard library. The collector runs as a local loop or as a serverless function triggered by an external cron (state lives in the database, not the process).

## Quickstart

```bash
# single poll (creates data/punktlig.db)
python3 -m punktlig.collect --once

# collect continuously
python3 -m punktlig.collect --loop

# build training rows from the archive, then evaluate the baselines
python3 -m punktlig.dataset
python3 -m punktlig.report

# run tests
python3 -m unittest discover tests
```

The collector is dependency-free. The analysis layer needs a few packages:

```bash
python3 -m venv .venv
.venv/bin/pip install duckdb lightgbm numpy

# move completed days into day-partitioned parquet, prune old raw XML
.venv/bin/python -m punktlig.compact

# train LightGBM and compare it against the baselines on a time-based split
.venv/bin/python -m punktlig.train
```

Raw responses are archived before anything is written to the database, so a failed write is recoverable:

```bash
# see what could be rebuilt from the raw files, without writing
python3 -m punktlig.reparse --dry-run

# rebuild the rows for one day
python3 -m punktlig.reparse --day 2026-07-27
```

Reparsing fills holes only. A day already moved to parquet is refused, and a poll close in time to one already in the archive is skipped, so it is safe to rerun.

Completed days are compacted from the hot SQLite database into zstd parquet, which shrinks them by an order of magnitude. The dataset builder reads both tiers transparently, so nothing changes downstream. Exports are verified by row count before any source rows are deleted.

## Configuration

Everything is configured through environment variables:

| Variable | Default | Purpose |
|---|---|---|
| `PUNKTLIG_CLIENT_NAME` | `punktlig-collector` | `ET-Client-Name` header. Set your own; Entur requires an identifying name |
| `PUNKTLIG_DATASET` | `RUT` | Entur codespace to poll (e.g. `RUT`, `NSB`, or empty for all of Norway) |
| `PUNKTLIG_AUTHORITY` | `RUT:Authority:RUT` | Authority used to resolve line to transport mode |
| `PUNKTLIG_MODES` | `tram,metro` | Which transport modes to keep (`tram,metro,bus,rail,water`) |
| `PUNKTLIG_DB` | `data/punktlig.db` | SQLite database path |
| `PUNKTLIG_LAT` / `PUNKTLIG_LON` | Oslo | Weather forecast point |
| `PUNKTLIG_MET_UA` | (placeholder) | User-Agent for MET Norway. Set your own contact info |

The scope is deliberately small to start (Oslo trams and metro). Widening to buses, trains and ferries across Norway is a config change, not a rewrite.

## Data model

| Table | Contents |
|---|---|
| `call_snapshot` | One row per (poll, journey, stop): aimed/expected/actual times as published at poll time. The core archive |
| `poll` | Poll log: pages, row counts, duration, errors |
| `weather_snapshot` | Hourly forecast snapshots. Forecasts rather than observations, to avoid leakage |
| `line` | Line to transport mode lookup from JourneyPlanner |
| `kv` | Collector state (SIRI requestorId, secondary feed timers) |

The collector is built to fail loudly rather than quietly: repeated database errors trigger a reconnect and then a non-zero exit, so a scheduler restarts a clean process instead of leaving one that looks healthy but writes nothing. Response bodies are read under a wall-clock deadline, because a socket timeout only bounds a single read and a trickling response can otherwise hang a poll indefinitely.

Raw gzipped XML responses are also archived under `data/raw/` so the parsed schema can be rebuilt or extended later.

## ML plan

1. Baselines: the timetable itself, and Entur's own expected times
2. Model: gradient boosted trees (LightGBM) predicting delay per (vehicle snapshot, future stop), with the horizon as a feature
3. Features: current delay and its trend, schedule slack per segment, calendar (rush hour, weekday, school holidays), network state (vehicle ahead on the line, shared tunnel/track load), per-departure delay history, weather forecasts
4. Uncertainty: quantile regression for calibrated arrival intervals, which the official feed does not offer at all
5. Evaluation: time-based split, MAE per prediction horizon, beat-rate vs. Entur, calibration plots, ablation study per feature group

## Ablations

Feature groups are added one at a time and measured on identical data before they are allowed to stay. Numbers are validation MAE in seconds on a day split: trained on 2026-07-25, validated on 2026-07-26 (a Sunday morning, 87 309 rows). Two operating dates is far too little data for firm conclusions; this table exists to keep the method honest from day one.

| Horizon | n | Entur | base model | with segment slack |
|---|---|---|---|---|
| 0-5 min | 21 793 | 30.7 | 38.7 | 32.0 |
| 5-10 min | 19 382 | 46.6 | 50.1 | 45.2 |
| 10-20 min | 26 996 | 62.9 | 68.9 | 67.7 |
| 20-45 min | 19 138 | 77.4 | 74.8 | 80.2 |

Segment slack is two features. The first is the scheduled remaining runtime: the aimed time at the target stop minus the aimed time at the vehicle's current stop. The second subtracts the typical observed runtime over the same path, where "typical" is a running mean over runtimes observed strictly before prediction time, so the feature obeys the same no-lookahead rule as everything else.

The group stays: weighted MAE drops from 58.5 to 56.5 seconds, and the model beats Entur on the 5-10 minute horizon for the first time. It also regresses the 20-45 minute bucket. A plausible explanation is that summing running means over many segments amplifies noise when the history is a single day; this gets re-examined as the archive grows.

Later groups, each measured the same way (two arms on one frozen dataset; the validation day keeps growing between rounds, so weighted numbers are comparable within a round, not across rounds):

| Feature group | Weighted MAE without | with | Verdict |
|---|---|---|---|
| Segment slack | 58.5 | 56.5 | stays |
| Bunching (headway and delay of the vehicle ahead) | 55.30 | 55.32 | parked: helps 20-45 min, hurts shorter horizons; re-measure with more history |
| Network state (mean delay last 30 min, per stop and per line) | 54.76 | 54.33 | stays |
| Slack noise floor (a segment mean needs 3 observations to count) | 54.23 | 53.69 | stays |

After these rounds the model is ahead of Entur on weighted validation MAE: 53.69 against 55.01 seconds, winning the 5-10 minute horizon by 3.9 seconds and 20-45 by 3.0, while 0.2 and 0.5 seconds behind on 0-5 and 10-20. The noise-floor round confirmed the earlier hypothesis: the 20-45 regression was one-off runtimes polluting the path sums. Two capacity experiments on the same frozen data (num_leaves 127, learning rate 0.03) were both worse overall and were rejected.

There is also a blending variant (`--with-entur`) that adds Entur's own prediction as a feature, so the model learns to correct the official estimate instead of starting from zero. On the same frozen dataset it beats Entur on every horizon: 27.0 vs 30.7, 40.5 vs 46.7, 61.9 vs 63.5 and 77.0 vs 79.1 seconds, weighted 51.76 vs 55.01. The default model stays independent of Entur's estimate, which is the stronger standalone claim; the blend shows how much value the model adds on top of the production system.

All of this is measured on one Sunday with one Saturday of training data, so treat it as a first honest signal, not a result.

## Roadmap

- [x] Collector: SIRI-ET delta polling, mode filtering, weather and deviation snapshots
- [x] Dataset builder: replay the archive into training rows without lookahead
- [x] Storage tiering: verified parquet compaction, raw retention, mixed-source replay
- [x] Training pipeline: LightGBM vs. all baselines on a time-based split
- [ ] Real results on weeks of data: ablations, learning curve, quantile intervals
- [ ] Backtest dashboard: error per horizon vs. Entur
- [ ] Serverless collection (Vercel function + external cron + hosted libSQL)
- [ ] Live site: realtime map, model vs. Entur per stop, uncertainty bands

## Data sources and attribution

- Realtime and route data: [Entur](https://developer.entur.org/) (NLOD licence). Not affiliated with or endorsed by Entur
- Weather forecasts: [MET Norway](https://api.met.no/) (CC BY 4.0)

## License

MIT
