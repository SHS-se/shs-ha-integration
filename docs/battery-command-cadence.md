# Battery command cadence and performance verification

App `0.1.0-beta.36`, integration `0.9.0-beta.77`.

The previous steady-demand replay (8.7× processing CPU and 28× checkpoint
reduction) was not representative of the deployed workload. Its small, fresh
database and immediate command readbacks omitted both continuous demand changes
and the accumulated objective/evidence archive. Those figures must not be used
as an estimate of container CPU or disk I/O.

## Runtime behaviour

- Complete sensor captures and meter receipts retain their exact evidence.
  Ordinary captures use the archival path even while commands are outstanding.
- The existing five-second refresh explicitly chooses the battery target.
  Command acknowledgements and deadline wakes progress physical work without
  selecting another target. Actual native control changes, confirmation,
  authority withdrawal, override and recovery still progress promptly.
- A pending proposal survives a new power/SOC capture with the same acknowledged
  starting controls. Changed native starting controls fence that proposal and
  immediately prepare from the current controls.
- If the final live binding check rejects a held target, unsent work is cancelled
  until the next decision. It cannot continually propose that invalid target.
  Already issued effects remain independently journalled.
- Physical send checks retain the same power binding, current permissions,
  headroom, native guards and validity window. Historical objective outcome
  reporting affects only replan messages, so it runs for explicit decisions and
  views, rather than repeatedly during physical send checks.
- The select still owns release. Restart, missing readings, lost plans and faults
  hold the last sent setting; leaving Controlling for Verification restores the
  captured baseline.

The resource log now reports `decisions/min` and `reducer_events/min` alongside
process CPU, receipt rate, backlog, checkpoint saves, query groups and kernel
write bytes. The `decision` CPU span is contained within `reduce`; do not add
those overlapping CPU totals. Archival captures have their own `evidence_ingest`
span. A normal active run should show about twelve scheduled decisions per
minute, plus genuine plan, authority or recovery decisions.

## Matched replay

`scripts/benchmark-command-churn.py` loads either fresh indexed storage or a
private, frozen SQLite copy. `--checkout` chooses the complete implementation,
including its app and core. No Home Assistant connection or real writes occur.
The archive is copied into a temporary directory and never modified.

The simulation publishes mixed power/SOC/cumulative-meter reports each second,
with small changing household demand. Adapter work takes 200 ms; service
acceptance takes 200 ms; native control publication follows five seconds later.
Reports overlap those in-flight operations. Ready actor callbacks and storage
transactions finish before virtual time advances; workers waiting for a timed
result stay in flight. The simulation does not call `host.idle()` after every
input, or use the Rig helper that refreshes every sensor timestamp. Initial
synthetic publications and plan admission are startup work, measured separately.

The frozen plan was holding the battery. Both versions therefore receive the
same explicitly simulated demand-following replacement while retaining the
original archive, authority and prior accounting facts. This tests command churn
with real archive size; it is not a replay of the original production plan.

Run the same harness against both trees, three times each:

```sh
python scripts/benchmark-command-churn.py --checkout /path/to/baseline \
  --archive /path/to/frozen.sqlite --seconds 60 --readback-delay-ms 5000
python scripts/benchmark-command-churn.py --checkout /path/to/candidate \
  --archive /path/to/frozen.sqlite --seconds 60 --readback-delay-ms 5000
```

The report separates startup and measured replay CPU, transactions, query groups,
command count, unresolved attempts/workers, command-stage occupancy, measurement
hash and archive hash. It fails on runtime faults. On Linux it also reports
`/proc/self/io` charged write bytes; on macOS that field is explicitly null.
Checkpoint counts and JSON sizes are never substitutes for physical disk writes.

## Results, 3 October 2026

Baseline: `7b8cbe7`, app beta.35. Candidate: the changes described above.
Python 3.13 on the development Mac; three 60-second simulations per case.

The frozen database is 789,221,376 bytes, with roughly 581,000 meter records,
244,000 observations and 819 admissions. SHA-256:
`07af470a5c17f7abd34b0dab9e453608bcfe81943aa0dfd68a0f2dba30564b96`.

| Workload | Baseline CPU median (range) | Candidate CPU median (range) | Baseline saves | Candidate saves |
| --- | --- | --- | --- | --- |
| Fresh storage | 2,597 ms (2,586–2,605) | 352 ms (314–374) | 1,025 | 56 |
| Frozen archive | 14,651 ms (14,572–15,159) | 1,106 ms (1,065–1,137) | 1,024 | 56 |

The archive replay gives **13.2× lower process CPU and 18.3× fewer checkpoint
saves**. Query groups fell from 175,374 to 4,392. The fresh replay gives 7.4×
lower CPU, illustrating why a small database understates the accumulated cost.

Both archive versions preserve all 240 new meter records and 60 new SOC facts;
the measurement fields, times and source metadata have the same hash. Transport
receipt ordinals and event IDs differ because the implementations issue different
numbers of commands and consequently receive different control publications.
No command feedback is fabricated as delivered energy.

Both cases consistently issue 55 commands and retain 55 unresolved attempts in
the baseline, versus 11 commands and one outstanding attempt in the candidate.
The baseline ends with four in-flight workers, the candidate with one. Time in
`needs_transition`/`executing` is 22.1 seconds versus 4.5 seconds (37% versus 8%).
These are internal simulated command stages, not a measurement of the HA select.

**These figures establish a replay improvement, not the deployed acceptance
result.** Physical disk writes were unavailable on macOS. Whole-app CPU also
includes gateway intake, scheduled devices, plan exchange and projections absent
from this fixture. Do not promise a tenfold container CPU or physical-I/O
reduction from these numbers.

After deployment, compare several settled one-minute windows with similar
battery activity and source rates. Record the running version, process CPU,
kernel write-byte deltas, scheduled decisions, reducers, saves, queries, backlog
and command inventory. Confirm that backlog and unresolved effects stay bounded.
Exclude startup and diagnostics exports. Keep the existing pre-change live
baseline (roughly 30–37% of one core and 600–680 checkpoint saves/minute) separate
from replay measurements; activity differences prevent a controlled comparison
until matched post-change windows are collected.

Verification: 1,081 core tests and 103 app tests passed. The continuity, battery
continuity, pool switch-gap and Verification handover tests passed unchanged.
New regressions cover physical/economic separation, delayed accepted readback,
proposal survival and fencing, guard loss, cancellation until the decision clock,
unchanged physical binding, and batched durability with an outstanding command.
