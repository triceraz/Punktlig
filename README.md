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

# fit arrival intervals (10th, 50th and 90th percentile) and check coverage
.venv/bin/python -m punktlig.quantiles
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
| `PUNKTLIG_DATASET` | `RUT` | Entur codespaces to poll, comma separated. Trains live under their own codespaces (`VYG`, `GOA`, `SJN`, `FLT`) rather than the local authority |
| `PUNKTLIG_AUTHORITY` | `RUT:Authority:RUT` | Authorities used to resolve line to transport mode, comma separated. Every codespace being polled needs one, or its journeys are dropped as unknown |
| `PUNKTLIG_SECONDARY_EVERY` | `120` | Seconds between polls of the codespaces after the first. The feed rate limits a client across all of them, so only the primary one runs every cycle |
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

Whether collection is actually working is a question about the archive, not about the process. A scheduled task can report Running while nothing has been written for hours, which is exactly what happened here once. `python3 -m punktlig.health` therefore judges the archive: how long since a poll landed, and whether recent polls carried rows or only errors. It exits non-zero when something is wrong, so a scheduler can run it and leave a trail.

The collector is built to fail loudly rather than quietly: repeated database errors trigger a reconnect and then a non-zero exit, so a scheduler restarts a clean process instead of leaving one that looks healthy but writes nothing. Response bodies are read under a wall-clock deadline, because a socket timeout only bounds a single read and a trickling response can otherwise hang a poll indefinitely.

Raw gzipped XML responses are also archived under `data/raw/` so the parsed schema can be rebuilt or extended later.

## ML plan

1. Baselines: the timetable itself, and Entur's own expected times
2. Model: gradient boosted trees (LightGBM) predicting delay per (vehicle snapshot, future stop), with the horizon as a feature
3. Features: current delay and its trend, schedule slack per segment, calendar (rush hour, weekday, school holidays), network state (vehicle ahead on the line, shared tunnel/track load), per-departure delay history, weather forecasts
4. Uncertainty: quantile regression for calibrated arrival intervals, which the official feed does not offer at all
5. Evaluation: time-based split, MAE per prediction horizon, beat-rate vs. Entur, calibration plots, ablation study per feature group

## Results

Validation MAE in seconds on a day split: trained on 2026-07-25 and 2026-07-26, validated on 2026-07-27, a Monday including the morning rush. 167 383 validation rows, none of them seen during training or used for feature selection.

| Horizon | n | Timetable | Naive | Entur | Model | Model with Entur as input |
|---|---|---|---|---|---|---|
| 0-5 min | 39 570 | 77.4 | 32.0 | 31.7 | 22.8 | 21.6 |
| 5-10 min | 34 882 | 79.5 | 42.0 | 44.5 | 32.1 | 31.1 |
| 10-20 min | 50 671 | 83.3 | 53.0 | 56.8 | 43.4 | 42.5 |
| 20-45 min | 42 260 | 88.8 | 68.9 | 73.3 | 59.0 | 58.0 |
| Weighted | 167 383 | 82.3 | 50.3 | 52.5 | 40.1 | 39.1 |

The model beats Entur on every horizon, by 23 percent weighted, and the margin holds through rush hour. Two days of training data is still a small archive, and these numbers will move as it grows.

Entur tracks the naive baseline closely and falls behind it beyond five minutes, which is the pattern the project set out to test: the official estimate largely propagates the current delay forward instead of modelling how it evolves.

### Which direction the errors go

Mean absolute error hides the direction of a miss, and the two directions are not equally annoying. Arriving later than announced means the countdown reaches zero with nothing there; arriving earlier means the vehicle may leave before a passenger who trusted the display gets there. Signed error, on the same validation day, with a 30 second tolerance:

| Predictor | Bias | Median | Came early | On time | Came late |
|---|---|---|---|---|---|
| Naive | -15.8 | -5.0 | 21.1 % | 45.9 % | 33.0 % |
| Entur | -4.5 | 0.0 | 26.1 % | 45.4 % | 28.5 % |
| Model | -16.8 | -5.6 | 15.2 % | 55.1 % | 29.6 % |
| Model with Entur as input | -13.9 | -3.7 | 16.0 % | 56.4 % | 27.6 % |

Entur is the best calibrated of the four: its median error is exactly zero and its bias is only four seconds. There is no sign of a deliberate lean towards promising a later arrival than expected. Our model is more often on time (55 against 45 percent) but leans optimistic, which is what an L1 objective does on a right-skewed delay distribution: it fits the conditional median, and the long tail of large delays sits above it.

That lean is adjustable. Fitting a quantile above the median shifts every prediction later on purpose, and `--alpha` does exactly that:

| Objective | MAE | Bias | Came early | On time | Came late |
|---|---|---|---|---|---|
| Entur | 52.46 | -4.3 | 25.9 % | 45.0 % | 29.1 % |
| L1, the median | 40.10 | -15.4 | 15.7 % | 55.5 % | 28.7 % |
| Quantile 0.55 | 39.75 | -9.9 | 19.1 % | 55.2 % | 25.8 % |
| Quantile 0.60 | 39.92 | -3.6 | 23.2 % | 54.2 % | 22.6 % |
| Quantile 0.65 | 40.55 | 1.8 | 26.9 % | 52.9 % | 20.2 % |
| Quantile 0.70 | 42.83 | 10.1 | 33.4 % | 49.5 % | 17.1 % |

Quantile 0.6 cuts the countdown-lied failures by a fifth, from 28.7 to 22.6 percent, for no meaningful cost in mean error. That looks free, and it partly is, but one caveat is load bearing: training runs on a weekend and validation on a Monday, so validation delays are larger than training delays and a small upward shift flatters itself. The trade needs re-measuring once training and validation days are comparable. The default stays on the median, which is the honest MAE-optimal claim.

## Uncertainty

`python3 -m punktlig.quantiles` fits the 10th, 50th and 90th percentile of the delay, which turns a point estimate into an arrival window. Entur publishes no uncertainty at all, so this is a capability the official feed lacks rather than a sharper version of one it has.

| Horizon | n | Coverage | Target | Interval width |
|---|---|---|---|---|
| 0-5 min | 39 570 | 81.3 % | 80 % | 72.9 s |
| 5-10 min | 34 882 | 76.4 % | 80 % | 93.1 s |
| 10-20 min | 50 671 | 76.4 % | 80 % | 125.8 s |
| 20-45 min | 42 260 | 75.8 % | 80 % | 167.5 s |

Overall coverage is 77.4 percent against the 80 percent the interval claims, so the bands are slightly too narrow, and honestly labelled as such. Width grows with the horizon, which is the expected shape: the further ahead the question, the less anyone can know.

### Answering the passenger's actual question

A passenger does not ask for the 80th percentile. They ask whether the tram will be here within two minutes. That is the same distribution read the other way round: instead of fixing a probability and asking for a time, fix the time and ask for the probability.

`python3 -m punktlig.quantiles --ladder` fits seven quantiles from the 5th to the 95th, repairs any crossings, and reads probabilities off the ladder by interpolation. The median rung is the main prediction; the rest describe how wrong it might be.

Whether those probabilities are true is a separate question from whether the model is accurate, and it needs its own check. Grouping every validation row by what we claimed, against what happened:

| We said | n | Actually happened |
|---|---|---|
| 0-20 % | 73 986 | 0.4 % |
| 20-40 % | 1 752 | 23.7 % |
| 40-60 % | 1 970 | 43.3 % |
| 60-80 % | 3 076 | 73.0 % |
| 80-100 % | 12 397 | 97.0 % |

Read the middle rows first: when the model says roughly half, it happens roughly half the time. The claim holds where it is hardest to be right. The top band under-promises, arriving 97 percent of the time where the average claim is nearer 90, which is the safer direction to be wrong in but still a miscalibration worth naming.

The general version of the same check asks how the outcomes spread across the whole distribution, not just one deadline. A calibrated model puts a tenth of them in each tenth of its predicted range; this one produces 9.6, 9.1, 10.0, 7.4, 7.5, 12.1, 9.9, 12.1, 9.9 and 12.4 percent.

## Tuning

Every parameter change was measured on one frozen dataset, one change at a time, against the same validation day.

| Change | Weighted MAE | Verdict |
|---|---|---|
| Starting point: 63 leaves, learning rate 0.05, min 50 rows per leaf | 40.25 | |
| 127 leaves | 40.35 | rejected |
| 255 leaves | 40.38 | rejected |
| Feature and row sampling at 0.8 | 40.52 | rejected |
| L2 penalty 5 | 40.33 | rejected |
| Min 200 rows per leaf | 40.19 | kept |
| Learning rate 0.03 | 40.13 | kept |
| Learning rate 0.02 with min 200 rows per leaf | 40.10 | adopted |
| Predicting the change in delay instead of the delay | 40.05 | not adopted: the weighted gain is inside the noise and it doubles training time, though it clearly wins the 0-5 minute bucket (22.2 against 22.8) and deserves a second look as a per-horizon variant |

Capacity increases all made it worse, which is the expected shape for two days of data: the limit is the archive, not the model.

### Is a simple model enough?

Beating the official system means little if a linear model on the same features does it too, so here is the whole field on the same validation day:

| Predictor | MAE |
|---|---|
| Timetable, pretending every vehicle is on time | 82.51 |
| Naive, carrying the current delay forward | 49.78 |
| Entur | 52.46 |
| Ridge regression on the numeric features | 50.53 |
| Gradient boosted trees | 40.10 |

The ridge result is the interesting one: a linear model cannot even beat carrying the current delay forward. Whatever structure the trees are finding is not a weighted sum of these features, it is interactions and thresholds, which is the argument for the model class rather than an assumption about it.

### Where the model wins, and where it loses

Averages hide the cases that matter. Broken down by how much delay the vehicle has already accumulated:

| Situation | n | Model | Entur |
|---|---|---|---|
| Running on time | 97 322 | 35.3 | 51.7 |
| 1-3 minutes late | 59 136 | 46.2 | 53.9 |
| 3-6 minutes late | 9 965 | 47.5 | 50.7 |
| More than 6 minutes late | 739 | **82.6** | **63.7** |

The advantage comes almost entirely from vehicles that are running normally, and it disappears exactly where a passenger cares most. On the badly delayed vehicles the model is 30 percent worse than the official estimate. Those are 0.4 percent of the rows and the model has barely seen such cases, so it pulls them back towards typical behaviour. It is also the situation where an operator knows things we cannot observe: whether the vehicle is being held, turned short, or taken out of service.

The same shape appears along the journey. With one or two stops behind it the model errs by 48.9 seconds against Entur's 55.4; with more than twenty stops behind it, 30.8 against 46.7. Our features are history, so the model is weakest exactly when there is no history yet.

### How much would more data buy?

Trained on the most recent slice of the archive, validated on the same untouched day:

| Share of training data | Rows | MAE | Better than Entur |
|---|---|---|---|
| 10 % | 84 880 | 43.94 | 16.2 % |
| 25 % | 212 200 | 42.81 | 18.4 % |
| 50 % | 424 400 | 40.93 | 22.0 % |
| 75 % | 636 600 | 40.56 | 22.7 % |
| 100 % | 848 799 | 40.10 | 23.6 % |

The curve is still falling. Doubling the archive from a half to a full share bought 0.83 seconds, while every parameter change tested here bought 0.15 seconds in total. More history is worth roughly five times more than more tuning, which settles where the effort belongs.

One caveat on reading it: the slices come from the same two days, so this measures the value of more rows, not the value of more variety. New days bring weather, incidents and weekday patterns the archive has never seen, and that is a different kind of gain than this curve can show.

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
| Freshness (age of the feed's own report, time since the last stop passed) | 40.81 | 40.25 | stays |
| Deviation messages (situations in force for the line and the network) | 40.26 | 40.46 | parked: almost no within-day variation yet, collection continues |

The freshness group was added to test a specific hypothesis. The blending variant wins because Entur sees the vehicle between stops and we only see it at stops, so telling the model how stale its picture is should recover part of that edge. It did: on identical data the gap between the independent model and the blend fell from 1.45 to 1.16 seconds, about a fifth of it, while both models improved.

How far that can go was measured directly. A model trained to predict Entur's own prediction from our features alone reproduces it to 15.9 seconds, against a spread of 54.6 seconds in Entur's predictions, so most of what the official system outputs is already computable from what we archive. The part that is not correlates with our remaining error at 0.10, which is about one percent of our error variance: real, but small. That one percent is the whole blending advantage, and it is what the freshness group ate into.

After these rounds the model is ahead of Entur on weighted validation MAE: 53.69 against 55.01 seconds, winning the 5-10 minute horizon by 3.9 seconds and 20-45 by 3.0, while 0.2 and 0.5 seconds behind on 0-5 and 10-20. The noise-floor round confirmed the earlier hypothesis: the 20-45 regression was one-off runtimes polluting the path sums. Two capacity experiments on the same frozen data (num_leaves 127, learning rate 0.03) were both worse overall and were rejected.

There is also a blending variant (`--with-entur`) that adds Entur's own prediction as a feature, so the model learns to correct the official estimate instead of starting from zero. On the same frozen dataset it beats Entur on every horizon: 27.0 vs 30.7, 40.5 vs 46.7, 61.9 vs 63.5 and 77.0 vs 79.1 seconds, weighted 51.76 vs 55.01. The default model stays independent of Entur's estimate, which is the stronger standalone claim; the blend shows how much value the model adds on top of the production system.

All of this is measured on one Sunday with one Saturday of training data, so treat it as a first honest signal, not a result.

## Roadmap

- [x] Collector: SIRI-ET delta polling, mode filtering, weather and deviation snapshots
- [x] Dataset builder: replay the archive into training rows without lookahead
- [x] Storage tiering: verified parquet compaction, raw retention, mixed-source replay
- [x] Training pipeline: LightGBM vs. all baselines on a time-based split
- [x] Quantile intervals with a coverage check
- [ ] Real results on weeks of data: ablations, learning curve, per-line error analysis
- [ ] Backtest dashboard: error per horizon vs. Entur
- [ ] Serverless collection (Vercel function + external cron + hosted libSQL)
- [ ] Live site: realtime map, model vs. Entur per stop, uncertainty bands

## Data sources and attribution

- Realtime and route data: [Entur](https://developer.entur.org/) (NLOD licence). Not affiliated with or endorsed by Entur
- Weather forecasts: [MET Norway](https://api.met.no/) (CC BY 4.0)

## License

MIT
