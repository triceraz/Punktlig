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

Raw gzipped XML responses are also archived under `data/raw/` so the parsed schema can be rebuilt or extended later.

## ML plan

1. Baselines: the timetable itself, and Entur's own expected times
2. Model: gradient boosted trees (LightGBM) predicting delay per (vehicle snapshot, future stop), with the horizon as a feature
3. Features: current delay and its trend, schedule slack per segment, calendar (rush hour, weekday, school holidays), network state (vehicle ahead on the line, shared tunnel/track load), per-departure delay history, weather forecasts
4. Uncertainty: quantile regression for calibrated arrival intervals, which the official feed does not offer at all
5. Evaluation: time-based split, MAE per prediction horizon, beat-rate vs. Entur, calibration plots, ablation study per feature group

## Roadmap

- [x] Collector: SIRI-ET delta polling, mode filtering, weather and deviation snapshots
- [x] Dataset builder: replay the archive into training rows without lookahead
- [ ] Baselines + LightGBM, ablations, learning curve
- [ ] Backtest dashboard: error per horizon vs. Entur
- [ ] Serverless collection (Vercel function + external cron + hosted libSQL)
- [ ] Live site: realtime map, model vs. Entur per stop, uncertainty bands

## Data sources and attribution

- Realtime and route data: [Entur](https://developer.entur.org/) (NLOD licence). Not affiliated with or endorsed by Entur
- Weather forecasts: [MET Norway](https://api.met.no/) (CC BY 4.0)

## License

MIT
