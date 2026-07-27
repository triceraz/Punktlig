# Scaling the replay to months of archive

The replay that built every result in the README holds its history indexes
in memory. That was invisible while the archive was one line group in one
city, and it stopped being invisible the moment collection widened to every
mode and every train operator. This is the plan for the version that does
not care how large the archive gets.

## What actually broke

The indexes behind three feature groups keep one entry per observation, as
Python objects, so that every as-of-T lookup is exact:

- segment runtimes, for schedule slack
- stop passings, for bunching
- delay levels per stop and per line, for network state

Measured cost is roughly one gigabyte per four million archived calls.

| Archive | Calls | Index memory |
|---|---|---|
| Two days | 25 million | 6.8 GB |
| A week | 60 million | around 16 GB |
| A month | 250 million | around 65 GB |
| Three months | 750 million | around 190 GB |

The machine collecting this has 16 GB, so the two-day archive already fails
and no amount of trimming the window rescues it: even a single day of the
current feed is four gigabytes of index.

Sampling journeys, which the builder now supports, buys one measurement. It
is not a fix, because feature quality depends on seeing all the traffic. A
segment's typical runtime and a stop's recent delay level are averages over
whatever passed, and averaging a quarter of the vehicles measures something
different from what the model will meet in production. Sampling belongs in
training, where dropping rows is unbiased, not in feature construction.

## What the measurements said

Two attempts to fix this in Python, both measured rather than assumed:

**Counting into time buckets instead of storing every observation.** Memory
should then follow the calendar rather than the traffic. It did help, from
6.8 GB down to 3.1 GB on a comparable run, but not enough. The archive has
6 194 stops, 235 lines and 11 925 line-direction-stop combinations, so at
fifteen-minute buckets over two days the index still holds millions of
entries, and a Python dictionary entry holding a count and a sum costs a
couple of hundred bytes. Multiply by a month and the saving is irrelevant.

**Sorting on disk rather than in memory.** The replay reads every call in
chronological order per journey, which no index can satisfy because the
timestamp lives in the poll table, so SQLite sorts twenty-five million rows.
Setting `temp_store=FILE` was worth doing and is kept, but it was not the
cause: memory still climbed with the pragma confirmed active and no
temporary files written.

The conclusion is the one below, now with evidence: aggregating history in
Python dictionaries is the wrong shape for this problem no matter how the
buckets are drawn, because the cost is per entity per bucket and both grow.
The aggregation belongs in a database.

## The shape of the fix

Move feature construction into DuckDB, which the project already depends on
for reading Parquet. It is columnar, it spills to disk rather than dying,
and it has the exact primitive this problem is made of: `ASOF JOIN`, which
means "the most recent row at or before this timestamp". Every no-lookahead
rule in the replay is that sentence.

- Running means per segment become a window function over observations
  ordered by the time they became known.
- The weather join, the deviation join and the segment history join all
  become `ASOF JOIN`, so the rule is enforced by the query engine rather
  than by hand-written bisect calls.
- Training rows land in Parquet rather than SQLite: columnar, compressed,
  and read back in chunks.

Memory then depends on DuckDB's buffer size, which is configurable, rather
than on how much history exists.

## Keeping it honest

The Python replay stays as the reference implementation. The tests that pin
the no-lookahead guarantees run against it today, and the new builder has to
produce byte-identical rows on the same synthetic archive before it replaces
anything. That is the whole point of having a small, fully specified test
scenario: a rewrite of this size should be provable, not merely plausible.

## Order of work

1. Unify the sources into one DuckDB relation over hot SQLite and Parquet.
2. Vehicle state per snapshot: current stop, current delay, trend, counts.
3. Ground truth and the strictly-later-snapshot rule.
4. Weather and deviations, as ASOF joins.
5. Segment history and the path sum for schedule slack, the heaviest step,
   since it expands each row into one edge per stop remaining.
6. Row-for-row equivalence test against the Python replay.
7. Parquet output and chunked training.

Steps 1 to 4 are mechanical. Step 5 is where the real work is: the path sum
turns fourteen million rows into something closer to a hundred million
edges, and it decides whether this is fast enough to run daily.
