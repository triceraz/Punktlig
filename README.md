# Punktlig

**Live: [punktlig.vercel.app](https://punktlig.vercel.app)**. A terminal you can type into, with every vehicle in Oslo and Akershus on a map you can drag and zoom, updated every ten minutes.

Can a machine learning model beat the official Norwegian public transport delay predictions?

Yes, by **32.5 percent**, measured on 6 626 611 departures the model never saw during training.

| | Mean error |
|---|---|
| The timetable, pretending nothing is ever late | 126.6 s |
| Carrying the current delay forward | 76.0 s |
| **The official realtime estimate** | **77.8 s** |
| **Punktlig** | **52.5 s** |

Entur publishes realtime "expected" arrival times for all public transport in Norway. Those estimates are largely naive propagation: a vehicle that is 4 minutes late now is assumed to be 4 minutes late at every future stop. That ignores schedule slack, rush-hour dynamics, bunching, shared-tunnel effects and weather, which is most of what actually drives how delays evolve. The table above is the evidence: the official estimate is a second and a half worse than simply assuming the delay never changes.

Punktlig continuously archives both Entur's live predictions and the eventual ground truth, then trains a model to out-predict the official estimates. The evaluation is honest: no lookahead, a day-level train/validation split, and the baseline is a real production system rather than a straw man.

It also does something the official feed does not do at all: it says how sure it is. Each departure gets its own arrival interval, and that interval holds 79.9 percent of the time against the 80 percent it promises.

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
| `PUNKTLIG_DATASET` | `RUT` | Entur codespaces to poll, comma separated. Trains live under their own codespaces (`VYG`, `GOA`, `FLT`) rather than the local authority. The secondaries are rotated each cycle, because the feed's rate limit is spent in list order and a fixed list starves whichever stream is last |
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

That check was written for a total stall, and it missed the other kind. On 2026-08-06 Flytoget was refused with 429 on every cycle for seven hours while Ruter kept polling every minute, so the newest poll in the archive was always seconds old and the verdict stayed OK. A codespace failing alone is not a failed poll: the exception is caught per stream and no row is written at all, which is invisible to a question asked about the newest row overall. Each stream is now also judged on its own measured cadence, so one operator going quiet is a problem rather than a silence.

The collector is built to fail loudly rather than quietly: repeated database errors trigger a reconnect and then a non-zero exit, so a scheduler restarts a clean process instead of leaving one that looks healthy but writes nothing. Response bodies are read under a wall-clock deadline, because a socket timeout only bounds a single read and a trickling response can otherwise hang a poll indefinitely.

Raw gzipped XML responses are also archived under `data/raw/` so the parsed schema can be rebuilt or extended later.

## ML plan

1. Baselines: the timetable itself, and Entur's own expected times
2. Model: gradient boosted trees (LightGBM) predicting delay per (vehicle snapshot, future stop), with the horizon as a feature
3. Features: current delay and its trend, schedule slack per segment, calendar (rush hour, weekday, school holidays), network state (vehicle ahead on the line, shared tunnel/track load), per-departure delay history, weather forecasts
4. Uncertainty: quantile regression for calibrated arrival intervals, which the official feed does not offer at all
5. Evaluation: time-based split, MAE per prediction horizon, beat-rate vs. Entur, calibration plots, ablation study per feature group

## Results

Validation MAE in seconds on a day split. The model is trained on 19 675 358 rows and validated on 6 626 611 departures from operating dates it never saw, with no row used for both.

| Horizon | n | Timetable | Naive | Entur | Punktlig | Gain |
|---|---|---|---|---|---|---|
| 0-5 min | 1 409 353 | 119.1 | 45.0 | 47.4 | **31.2** | -16 s |
| 5-10 min | 1 316 823 | 123.7 | 62.5 | 65.9 | **43.0** | -23 s |
| 10-20 min | 1 912 451 | 128.0 | 79.6 | 82.5 | **54.6** | -28 s |
| 20-45 min | 1 987 984 | 132.4 | 103.4 | 102.8 | **72.0** | -31 s |
| Weighted | 6 626 611 | 126.6 | 76.0 | 77.8 | **52.5** | -25 s |

The model beats Entur on every horizon, by 32.5 percent weighted, and the margin widens with the horizon: 34 percent at five minutes and still 30 percent at forty-five. Close to a stop both systems know roughly the same thing; twenty minutes out there is only pattern to go on, and that is where reading the archive pays.

Entur tracks the naive baseline closely and falls behind it below twenty minutes, which is the pattern the project set out to test: the official estimate largely propagates the current delay forward instead of modelling how it evolves.

### Per operator

The archive covers five Entur codespaces, and they are not the same problem.

| Operator | Naive | Entur | Punktlig |
|---|---|---|---|
| Ruter (bus, tram, metro, ferry) | 74.6 | 77.3 | 52.2 |
| Vy (regional and local rail) | 108.9 | 111.7 | 90.3 |
| Go-Ahead | 107.2 | 107.2 | 101.2 |
| Flytoget | 140.6 | 140.6 | 99.0 |

Trains are a different and harder problem: the naive baseline alone is 201.6 seconds on rail against 74.6 on Ruter, because a train's delay is set by things happening tens of kilometres away. The model still improves on the official estimate for every operator, but the gap it closes on Ruter is the headline, and rail is where the remaining work is.

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
| 0-5 min | 1 409 353 | 80.7 % | 80 % | 1 min 46 s |
| 5-10 min | 1 316 823 | 80.0 % | 80 % | 2 min 20 s |
| 10-20 min | 1 912 451 | 79.7 % | 80 % | 2 min 55 s |
| 20-45 min | 1 987 984 | 79.4 % | 80 % | 3 min 42 s |

Overall coverage is 79.9 percent against the 80 percent the interval claims. Width grows with the horizon, which is the expected shape: the further ahead the question, the less anyone can know.

### The average window is not any departure's window

That table is a summary, and reading it as a promise gets the interesting half of the result backwards. The bounds are predicted per departure from the same features as the point estimate, so the spread between departures is larger than the spread between horizon buckets. Measured across 300 000 validation rows:

| | Window |
|---|---|
| Narrowest quarter | under 1 min 29 s |
| Middle | 2 min 24 s |
| Widest quarter | over 3 min 40 s |
| Widest single departure | 34 min |

Even inside one horizon bucket the range runs from 69 seconds to over half an hour. A ferry on the Nesodden crossing gets a window of seconds, because it has no traffic to sit in and the model has learned that; a regional train on Jæren an hour out gets thirty minutes. Both are correct, and neither is the average.

Measuring this also caught a real bug. Independently fitted quantiles can cross, and some intervals came out negative: an upper bound below the lower one, which is not an interval and quietly flatters both the width and the coverage. The repair belongs at every point the bounds are used, not only in the ladder report where it already existed.

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
- [x] Results on the full archive: 19.7M training rows, 6.6M validation departures, no sampling
- [x] Live site: realtime map, model vs. Entur per stop, per-departure uncertainty bands
- [x] Per-operator evaluation, so rail is not hidden inside a Ruter-shaped average
- [ ] Rail as its own problem: the naive baseline is 201.6 s on trains against 74.6 on Ruter
- [ ] Re-measure the parked feature groups (bunching, deviation messages) now the archive spans weeks
- [ ] Serverless collection (Vercel function + external cron + hosted libSQL)

## Data sources and attribution

- Realtime and route data: [Entur](https://developer.entur.org/) (NLOD licence). Not affiliated with or endorsed by Entur
- Weather forecasts: [MET Norway](https://api.met.no/) (CC BY 4.0)

## License

MIT
