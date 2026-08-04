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
