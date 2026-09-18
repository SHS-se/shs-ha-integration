# CPU and I/O investigation

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

## Remaining measurements

After deployment, wait for beta.19's bounded cleanup to drain the old backlog,
then profile again under comparable battery activity. Compare multiple windows,
including battery inactive and active, without changing other integrations.
The profiler itself never changes battery mode.

The decision journal still rewrites its complete retained window on new decision
groups; immutable paging is a separate persistence change. Repeated meter-history
scans and index construction also remain, but occupied fewer samples than archive
serialization in this baseline. Use the next profile to determine their priority.
