# CPU, memory and I/O investigation

## Built-in resource diagnostics (beta.45)

The native HA diagnostics download and the controller gzip download include
`resource_profiling`. The admin-only `shs_energy.profile_resources` action returns
the same report without building the complete accounting export. Through the HA
connector, call that action or read native diagnostics at
`diagnostics_data_path="data.resource_profiling"`.

The profiler belongs to one integration load and retains 120 one-minute samples
(two hours), with cumulative operation timings, current process memory and
account/archive/queue counts. It never retains individual input events. Reload
starts a new session. Save a report before restarting when comparing a growing
process. Look at changes in the counters between samples, not just lifetime totals.

- `operations`: synchronous reducer, accounting-view, checkpoint-encoding and
  archive-page encoding/hash CPU time; archive save, checkpoint save, archive
  collection and refresh wall time. `cpu_measured=false` means CPU is **not
  measured**, not that an operation used no CPU. Awaited spans cannot attribute
  CPU to SHS because other coroutines run while they are suspended. These spans
  overlap and must not be added. Archive encoding counts page serialization and
  hashing; it is not every part of archive traversal or domain encoding.
- `process`: current RSS, lifetime RSS peak, swap, thread count and cumulative
  process CPU. `cpu_percent_one_core` is the interval CPU rate (100% = one core).
  These include all integrations; current RSS and the lifetime peak are distinct.
  Process-file reads run in the executor. Read failures appear as an explicit
  error rather than a zero memory measurement.
- `retained`: account meter/observation/admission counts, trace count, archive
  known/reachable pages, queued events and active effects. Counts are not byte
  estimates. Their slopes identify growing owners without walking the heap.

For allocation source lines, explicitly start a short capture:

```yaml
action: shs_energy.profile_resources
data:
  allocation_seconds: 30
```

Call again with no `allocation_seconds` after 30 seconds. `allocations` reports
the top 20 process allocation locations, top 20 SHS locations, traced memory and
tracer overhead. It measures allocations made **after tracing starts** that are
still alive at the end; it cannot attribute older objects. Tracing is process-wide
and adds overhead, so it is off by default and limited to 1–300 seconds. Another
active tracer causes an explicit refusal. Snapshot analysis runs in a worker;
only its bounded summary is retained. Completion, errors and unload release this
profiler's tracer. An existing tracer owned elsewhere is left alone.

For CPU call stacks or older retained memory, combine this with the external
sampler below and a local account benchmark:

```sh
python3 scripts/benchmark-account.py '/path/to/shs-controller-diagnostics.json.gz' --memory
```

The benchmark loads the immutable account locally, distinguishes cold index
construction from warmed-up operations, and optionally estimates deduplicated
reachable Python-object bytes. It reports counts/timings, never sensor values.
Use the same export and Python version to compare revisions. The estimate is
neither RSS nor peak memory, and the benchmark itself may require substantial RAM.

## 25 September 2026 investigation

`history (34).csv` contains whole-machine CPU and memory, from 31 August to
25 September. UTC time-weighted daily CPU was 13.29% on 1 September, 29.93% on
23 September and 26.88% on the partial 25 September day. Memory was 24.37%,
42.87% and 44.75% respectively. These sensors alone do not attribute usage.

The HA connector confirmed loaded beta.44, an approximately 79-second integration
setup, and a warning that the battery execution-mode selector took 1.485 seconds
to update. A 30-second external sample at 08:43 UTC measured HA Python at 19.35%
of four-core capacity. Of 1,482 stack reads, 1,186 succeeded and 352 contained SHS;
296 failed. Repeated `MeterIndex` construction, `Account.__post_init__` validation,
`BatteryRuntime._meter` scans and accounting-view scans were prominent. Samples
are wall-time observations, not per-function CPU percentages. Other integration
work also appears in the profile.

Today's controller export (50) holds 256,284 meter receipts, 109,594 state
observations and 795 admitted contracts. Full account evidence is reloaded after
restart, explaining why restarting does not remove its footprint or repeated
history-processing cost. This is retained application state, not proof that all
whole-machine memory growth is an SHS leak. The external sampler now includes
one-second RSS/CPU gauges as well as before/after RSS, lifetime peak and swap.

Beta.45 keeps exactly the existing journal/wire format and evidence:

- Account transitions validate newly received evidence and preserve the already
  validated immutable prefix. Public construction and restoration still validate
  the full history.
- Meter indexes survive unrelated observation/request updates. New receipts copy
  one stream's arrays and extend prefix sums. Late corrections recalculate the
  affected suffix; earlier snapshots retain their original answers.
- Meter source-time predecessor/same-time lookup is indexed. Event identities,
  observation lookup and physical binding checks avoid repeated full-history
  scans. Live objective definitions and acknowledged-receipt summaries are reused;
  objective outcomes still recalculate against current evidence and time.
- High-volume records use slots and share repeated vocabulary strings. Unique
  receipts and historical observations are not deleted or treated as redundant.

On the same real export with local Python 3.13, medians of seven warmed-up calls:

| Operation | beta.44 | beta.45 |
| --- | ---: | ---: |
| Live accounting view | 30.91 ms | 0.60 ms |
| Append state observation | 25.83 ms | 0.55 ms |
| Duplicate meter receipt | 3.19 ms | 0.005 ms |
| Append meter receipt | 49.55 ms | 4.19 ms |
| Account reachable objects after warm-up | 408.35 MiB | 277.09 MiB |

Cold hydration remained about nine seconds locally. First-use indexes have
construction costs; the warmed-up figures do not hide or replace those costs.
These are local measurements, not post-deployment whole-host improvements.

### Remaining scaling limit

Full audit history is still resident and grows. Tuple append and copying one
stream's index arrays still scale with retained history; late corrections can
recalculate a long suffix. This release materially reduces recurring CPU and
memory overhead, but it does **not** establish a fixed lifetime RAM limit.
Lossless bounded-RAM operation requires archive-backed history **and** historical
indexes with bounded page caches, a prepared-evidence boundary for the pure
reducer, consistent export snapshots and crash-safe migration. Arbitrarily
trimming meters, observations or objectives would change accounting semantics
and is not a substitute. The new collection slopes and allocation captures make
that remaining growth visible and attributable.

## Read-only host sampling

Run from this repository with the same HAOS SSH access as `scripts/deploy.sh`:

```sh
python3 scripts/profile-ha.py --seconds 30 --output /tmp/shs-cpu-profile.json
```

`HA_HOST` and `HA_PORT` (or `--host` / `--port`) select the host. The default is
`192.168.10.20:22222`. The script uses the running container's CPython 3.14
`_remote_debugging.RemoteUnwinder`. It installs nothing, reads no frame locals,
and does not inject code, change controls, pause HA, reload or restart anything.
The JSON is saved locally. Unsupported Python versions fail explicitly.

The report includes:

- Process CPU over the observation period, both as a percentage of one core
  and divided by the host CPU count.
- Kernel process-I/O counter deltas. `rchar` and `wchar` include cached file API
  traffic; `read_bytes` and `write_bytes` measure storage I/O charged to this
  process. All of these include other HA integrations and all Python threads.
- Main-thread stack samples, the innermost SHS frame, and inclusive SHS
  function counts. Inclusive counts overlap; do not add them together.
- The most frequent main-thread caller chains (up to 12 frames), to identify
  callers of shared routines such as `deepcopy` in SHS or other integrations.
- Failed and empty reads. Live frame mutation can make individual reads fail.
  They must not be counted as idle or non-SHS work.

Stack frequency measures sampled wall time, including blocking waits, not CPU
percentage. Process CPU excludes child processes such as go2rtc and cannot be
compared directly with the whole-machine System Monitor CPU sensor. The
installed manifest version may differ from the running code until HA restarts.

## 18 September 2026 baseline

The host was still on **0.9.0-beta.18**. The coordinator-record caching and
evidence garbage collection committed in beta.19 had not been installed.
Storage inspection found 137,352 SHS files totalling 1,100,670,561 bytes, including
a 15.75 MB decision journal, 45.09 MB sample journal and 7.18 MB coordinator record.

A 30.011-second profile on beta.18 measured:

| Measurement | Result |
| --- | ---: |
| HA Python CPU time | 20.14 seconds |
| HA Python CPU, divided by 4 cores | 16.78% |
| File API bytes read | 114,569,925 |
| File API bytes written | 213,051,364 |
| Kernel storage bytes read | 0 |
| Kernel storage bytes written | 213,774,336 |
| Attempted stack reads | 1,484 |
| Successful reads / empty reads within those | 1,150 / 157 |
| Failed reads | 334 |
| Reads containing SHS frames | 217 |

Of the successful reads, 110 contained battery checkpoint persistence, 109
contained archive sequence serialization, 44 contained mapping suggestions,
and 27 contained decision-journal flushing. These counts overlap. The sample
identifies work worth fixing; it cannot establish a precise CPU split or
attribute all process writes to SHS.

## Changes in beta.20

- Large execution-evidence chunks now retain immutable item-page identities.
  Appending a trace encodes/hashes the new item instead of rebuilding as many
  as 127 preceding large records. The on-disk page types and full retained
  history are unchanged. Small chunks stay packed. Tests cover corrections,
  chunk growth, failed persistence, garbage collection and full reload.
- Controller ownership reads household configuration directly from the durable
  record instead of constructing the editor's complete device inventory.
- Native execution-mode selects omit semantic mapping suggestions, which they
  never display. The configuration panel still gets suggestions; reviewed
  mappings, errors, inclusion defaults and permissions use the same code.
- The host profiler above makes before/after measurements repeatable without
  deploying extra in-process instrumentation.

A local Python 3.13 benchmark using a trace from diagnostics export (36),
filling a 120-item tail and appending 8 items, reduced median serialization CPU
from **4.79 ms to 0.094 ms per append** across five runs. The store discarded
pages to isolate encoding and hashing. This is not a measured whole-host CPU
reduction and excludes disk latency.

## Follow-up on beta.20

The supplied `history (27).csv` contains whole-machine CPU observations. The
time-weighted means for the 13:15, 13:30 and 13:45 UTC quarters were 14.3%, 14.3%
and 14.4%. The 14:45, 15:00 and 15:15 quarters were 28.4%, 30.2% and 32.5%.
These match the user's observation of a large difference with SHS enabled;
the CSV alone cannot attribute CPU to particular functions.

A new 30-second profile with beta.20 running measured HA Python CPU of 22.54%
when divided by four cores, and 402,882,560 kernel storage bytes written.
779 of 1,486 stack reads succeeded (187 were empty); 707 failed. 126 reads
contained SHS frames, including journal flushing, controller evaluation,
replanning and checkpoint persistence. These are overlapping samples, not a CPU
breakdown. The old evidence-file backlog had drained to about 10,000 files.

The active battery reported `handover_disposition_missing`, admitting replacement
plans roughly every 25–40 seconds. Five historical objectives were already
`forecast_complete`, but the execution assessment still demanded dispositions
for them. Planner feedback correctly omitted them because they were complete.
That mismatch caused repeated replanning, feeding controller and persistence work.

## Changes in beta.21

- Execution assessment uses the existing live-objective view. Completed forecasts
  and fulfilled objectives no longer request handover dispositions. Open,
  unresolved and missed obligations still trigger the appropriate replanning;
  the full historical audit is unchanged.
- The verification journal uses transactional SQLite storage at
  `.storage/shs_energy.verification.<entry_id>.sqlite`. Changed groups,
  configuration/slot records, removals and metadata commit atomically. New
  decisions and lifecycle events remain immediately durable; existing repeat
  and passive-sample batching and retention remain unchanged. JSON encoding and
  database work run in the executor. No connection remains open between operations.
- Existing JSON journals migrate once. The old journal is removed only after a
  successful database commit. For schema-4 journals, sample persistence is read
  back before discarding their original copy. After migration, the database is
  authoritative: corruption raises an error and never resurrects an old journal.
  This is a roll-forward change; older releases cannot read the SQLite journal.
- Control-path configuration reads resolve settings once per options revision
  and location, using HA's JSON codec for private return copies. Every read checks
  the current revision, including reads after awaits during command authorization.
- Diagnostics expose session totals under `controller_metrics.performance`:
  `verification_storage` reports commits, failures, rows written/deleted, encoded
  bytes, preparation CPU, worker CPU and commit elapsed time;
  `configuration_reads` reports reads, rebuilds and CPU. Encoded bytes are not
  physical disk I/O, and these counters do not cover all integration work.

The real journal snapshot contained 4,000 groups in a **15,480,519-byte** JSON
file. A local Python 3.13 benchmark appended a copy of its latest attempt and
expired one old group, retaining the same window. Across 15 commits, medians
were **802 JSON bytes encoded, one row written, one row removed, 1.83 ms elapsed
and 1.71 ms process CPU**. A reload matched the expected complete snapshot.
The local benchmark used the standard-library JSON encoder; HA uses its native
encoder. These numbers describe that specific record and a local SQLite database, not
HA host disk latency or a measured whole-host CPU reduction. Tests also check
that appending one group encodes one group with 100, 1,000 or 4,000 retained groups.
Delta preparation still walks the retained record references; it does not encode
all their payloads.

SQLite can leave an adjacent `-journal` file after an interrupted transaction.
Backups must capture a consistent database and any required journal, or use
SQLite's backup mechanism. Do not copy just the database during a live write.

## Next comparison

A 30-second profile provides useful information immediately; there is no
multi-minute profiler warm-up. Once HA startup has settled, collect several
30–60-second windows under comparable battery activity. Check the running version,
the replan reason/admission rate and deltas of the performance counters. Compare
battery inactive and active periods without changing other integrations. The
profiler itself never changes battery mode.

More devices still mean more evaluations and evidence. The journal now encodes
changed records rather than the whole retained history per decision, and the
completed-objective replan loop is removed. Repeated meter-history scans, inventory
construction and checkpoint work remain candidates; use the next host profile to
rank them. A lower total CPU figure still needs validation after deployment.

## Final follow-up: beta.21 host and beta.22 changes

At 16:36 UTC, after the user reported about 20 minutes of runtime, a 30-second
capture reported installed beta.21 and HA Python CPU of **13.13%** divided by
four cores, versus **22.54%** in the earlier beta.20 capture. Kernel storage
writes were **9,244,672 bytes**, versus **402,882,560 bytes** previously. A second
20-second capture measured **13.04%** CPU. These are observed process-wide
differences across time, not a controlled attribution or whole-machine sensor
readings. As always, the manifest identifies installed code, not proof of what
every loaded module is running.

The first capture had 1,378 successful reads out of 1,484 attempts, with 129
empty reads and 106 failed reads. SHS appeared in 50 samples. Archive saving
appeared in 24 and collection in 9 (overlapping inclusive counts). The expanded
caller capture identified repeated deep copies and layout hashing in Bermuda
(`ble_trilateration`) calibration, alongside UniFi and network work. These
callers explain why remaining process CPU cannot all be assigned to SHS.

Beta.22 targets the sampled archive loops without changing persistence or
retention: garbage collection uses native set difference instead of a Python
loop over every retained page, and chunk identity comparison uses native
`map`/`operator.is_` instead of a Python generator for every historical item.
A local synthetic microbenchmark with 10,000 retained pages and ten obsolete
pages measured **173 → 42 microseconds** for collection selection; a 128-item
unchanged chunk measured **2.53 → 1.25 microseconds** for identity comparison.
These are isolated operation timings, not a promised host CPU reduction.

The existing archive regression suite covers exact retained-page reachability,
bounded backlog cleanup, returning deleted content, late correction, reload,
failed publication and cancellation during deletion. Full integration tests
also run before committing. The profiler now retains caller chains for future
investigation without installing an in-process profiler or changing controls.

## Memory growth and the diagnostics download (beta.23)

The user's `history (29).csv` covers whole-machine memory. After the 16:51 UTC
restart on 18 September (beta.22), memory rose steadily from about 31% to 37% of
15.7 GB by 07:48 UTC, roughly 0.9 GB. Three diagnostics downloads added brief
3–5 point spikes. A read-only host check measured HA's Python process at 2.05 GB
RSS, with a 2.85 GB high-water mark.

Diagnostics export (38), taken at 07:39 UTC, was 338 MB of JSON. Of that,
`battery_execution.execution_traces` held 97,609 traces (223 MB). Export (37), 14
hours earlier, held 18,599. The reducer appends a trace for almost every event,
about 4,600–7,400 an hour, and nothing removed them. Decoded locally, the traces
held 256 MB of Python objects, about 2.6 KB each. The account held 93 MB, with
267 admissions, 33,298 meter receipts and 13,850 observations. On disk, 105,775
evidence page files (383 MB) remained, about 4,750 more each hour. Home
Assistant's storage manager also records every Store key that is written or
removed until it restarts. Each evidence page had its own Store, and each
checkpoint wrote several new page keys.

The download made one WebSocket message of the whole report on the event loop.
The browser then parsed, restringified and gzipped it. By the V8 string limit
(about 536 MB), the download would have failed outright within about a day.

Changes in beta.23:

- The reducer keeps the latest 8,192 traces. Once over the limit, the oldest
  1,024 leave together, which is eight whole archive chunks. Restart loads only
  those pages and decodes only those traces; decoding 97,609 had taken 3.6 s
  locally.
- The archive finds saved chunks by their first record, so trimming reuses every
  remaining page. Collection removes the rest, 500 per checkpoint.
- Evidence pages are written and read as files in the same storage layout,
  without a Store per page. Writes are atomic and raise errors instead of logging
  them. Removal unlinks a batch in one executor job.
- The panel downloads the gzip file from an admin-only HTTP view with
  `fetchWithAuth` and saves it without parsing. Under the controller lock, the
  view serializes a snapshot of the live controller state. It shares the
  verification journal's records instead of deep-copying them. The immutable
  account, command journal and traces are encoded one record at a time and gzipped
  (level 3) in a worker thread.

A local Python 3.13 benchmark on export (38) measured 2.33 s of event-loop work
for the old path. The browser work on the 338 MB JSON took another 3.2 s in
Node: parse, stringify and gzip. The new path measured 0.10 s on the event loop
and 0.93 s in the worker, producing 131 MB of JSON and a 13.1 MB file. These are
M3 timings; expect roughly 2–2.5× on the N100.

The account still grows with history. Between exports (37) and (38) it gained 56
admissions, about 22,700 meter receipts and 9,800 observations, about 19 MB of
JSON. Per-receipt O(meters) scans grow with it. The specification allows
retention that preserves settled totals and the intervals open objectives need,
but compaction changes the planner's history digest. That needs a design decision.
