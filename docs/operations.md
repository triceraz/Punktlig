# Running the collector on a desktop that is also used

Punktlig collects continuously from one machine. Everything here is a rule
learned by losing data, and each one names the incident so a future change
knows what it would be undoing.

## The machine must never sleep

```
powercfg /change standby-timeout-ac 0
powercfg /change hibernate-timeout-ac 0
```

Neither needs administrator rights. The screen timeout is separate and stays
as it was, so the monitors still switch off; only suspension is disabled.

Verify with:

```
powercfg /query SCHEME_CURRENT SUB_SLEEP STANDBYIDLE
```

`Current AC Power Setting Index: 0x00000000` means never.

**Why not a wake lock.** A guard process holding `ES_SYSTEM_REQUIRED` was
tried and did not work: the desktop suspended at 20:53 on 2026-08-03 while
the guard was running and had logged that it was holding the machine awake.
Fifteen hours of collection were lost. The cause was never established, and
that is the point: a mechanism whose failures are invisible is worse than a
setting that can be read back in one command. The guard was removed rather
than debugged.

Two earlier gaps have the same shape. On 2026-08-03 the machine slept at
16:54 and lost two and a half hours, because the guard then in place watched
for a list of module names that did not include the collector. A missed poll
cannot be recovered from anywhere.

## One collector, and one of each heavy job

Three locks, each guarding one thing, held by the operating system so a
killed owner releases immediately:

| Lock | Held by | Reason |
| --- | --- | --- |
| `collector.lock` | the collector | Restarts orphaned the previous python: the scheduler refuses a second task instance but knows nothing about the process it lost. Three collectors ended up sharing one database and polling Entur three times over. |
| `duckdb.lock` | site export, replay, recovery | Two DuckDB jobs at once take the machine down with an access violation inside the extension: no Python exception, no log, the job simply disappears. |
| `fitting.lock` | training, quantiles | Nine gigabytes of feature matrix between two runs. These read plain SQLite, so they deliberately do **not** take the DuckDB lock; holding it made the site stop publishing for the length of a training run. |

Analysis jobs put themselves in background priority when they take a lock.
The collector keeps normal priority: a slower training run costs nothing, a
missed poll is gone for good.

## The write-ahead log has to be folded back in

SQLite cannot checkpoint while a reader holds an older snapshot, and the site
export reads the archive every ten minutes. Nothing ever asked, so the log
reached two gigabytes; opening the archive then took longer than the
collector's busy timeout and it died on its own first statement, repeatedly.
The collector now checkpoints every thirty polls, and a blocked checkpoint is
logged rather than treated as a failure.

That fixed the asking, not the being refused. The log climbed back to 670 MB
because the export held a read for most of every ten minute window, and the
checkpoint had nowhere to land. See the next rule: the two are one problem.

## A job on a schedule has to fit inside it

The export ran every ten minutes and took thirty-eight. Runs were skipped by
the lock, the page that advertises realtime showed a picture up to forty
minutes old, and the checkpoint above never got a gap to run in.

It had no timing at all, so the only way to learn which part had grown was to
guess. It reports per phase now:

```
[2257s: features 1672s, modell 19s, nett 545s, resten 20s]
```

Both large phases answered questions that do not change on that timescale.
Route geometry is the shape of the lines; segment runtimes are running means
over days. Both are cached, geometry for six hours and history for one, and
the export drops to about a minute.

**What made the history cache safe to add is a number, not an assumption.**
The one part of the aggregate that does move within an hour is the recent
network state, the mean delay per stop and per line over the last half hour.
The ablation table puts that whole feature group at 0.43 seconds of a 52.5
second error, so an hour of staleness costs at most a fraction of that. A
cache with an unmeasured cost on the model would not have been worth an
export that finishes on time.

**The replay never reads either cache.** A training run has to aggregate the
archive it was actually given, so the cache is opt-in by path rather than on
by default, and a cached file built for a different bucket size or format is
refused rather than read. Those would be wrong answers, not slow ones.

## A stream can die on its own, and the machine will look healthy

Flytoget was refused with `HTTP 429` on every cycle for seven hours on
2026-08-06 while everything else kept collecting. Three faults lined up:

- **The poll order was fixed.** A cycle spends its request budget as it goes,
  so whichever codespace is last pays for everything ahead of it. FLT was
  last, so FLT always paid. The secondaries rotate a step per cycle now: a
  sustained limit costs each stream a turn instead of one of them everything.
- **SJN was in the list and had never returned anything.** 376 polls, zero
  calls, zero training rows: SJ Nord runs Trondheim to Bodø and publishes
  nothing in this region. It was spending a quarter of the secondary budget
  at Flytoget's direct expense.
- **The health check could not see it.** It asked how old the newest poll
  was, and with Ruter polling every minute the answer is always seconds. A
  codespace failing alone is caught per stream and writes no row at all,
  which is invisible to a question about the newest row overall.

Health now judges each configured stream against its own measured cadence,
and the cadence is measured rather than configured so the check cannot drift
out of step with a setting it never reads. Only configured streams are
judged: switching one off is not a fault, and the first version of the check
reported `PROBLEM` every hour about SJN, which had been removed on purpose.
An alarm that is always on is an alarm nobody reads.

Reading the configured list also meant the list had to exist once. It lived
in both `run-collector.cmd` and `run-site.cmd`, removing SJN updated one of
them, and the export went on asking for a stream nobody collected. All four
tasks read `punktlig-env.cmd` now.

## Where things live

- archive, parquet, raw XML, logs: `D:\punktlig-data`
- credentials for publishing: `D:\punktlig-data\punktlig.env`, outside the repository
- scheduled tasks: `Punktlig Collector`, `Compact`, `Health`, `Site`

The collector task runs as S4U, which hides its command line from an
unelevated session; its lock file is the reliable liveness test, since it
cannot be deleted while the collector holds it.

The site task keeps an interactive logon because pushing needs the user's
credential store, and runs through `run-hidden.vbs` so no console window
appears every ten minutes.
