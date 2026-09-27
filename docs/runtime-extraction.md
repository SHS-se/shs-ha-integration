# Shared runtime and snapshot rehearsal

Status: M2a implemented, 27 September 2026. This is the first part of step 2 in
[the app architecture](app-container-design.md), not household migration.

## Usage

HA imports its canonical package without changing its runtime composition:

```python
from .shs_core.execution_storage import ExecutionStorage
store = ExecutionStorage(path, hass.async_add_executor_job, legacy)
```

The app imports the identical packaged code without importing HA:

```python
from shs_core.execution_storage import read_execution_snapshot
snapshot = read_execution_snapshot(staging / 'execution.sqlite')
# Typed metadata/session/revision; cannot finish legacy import or dispatch.
```

An operator runs the rehearsal as a short-lived process in the app container:

```sh
python -m shs_app.migration_check \
  --storage /homeassistant/.storage --entry ENTRY_ID \
  --destination /data/rehearsal-UNIQUE_ID
```

The destination must not already exist. A successful run writes `report.json`
last. Failure leaves evidence but no success report. The report records that HA
still owns control and that the independently captured databases are **not** a
coherent household export. Neither source files nor permissions are changed.

## Problem and shape

The observation app had no executable accounting core. Simply copying its owner's
files would not move the runtime. Thirteen existing modules now form a closed
standard-library package at `custom_components/shs_energy/shs_core`: accounting,
energy ledger, physical models, command reducer/checkpoints, async effect host,
native transition adapter, serialization, profiling and the two SQLite stores.
The reducer still has no storage or framework dependency. HACS installs this
subtree normally. The container build copies the exact package, checks hashes
against the bundled companion, and smoke-imports it without HA.

HA-facing composition, configuration, recorder adaptation, cloud exchange,
ScheduledController, BatteryRuntime, writer fences and the legacy importer stay
in the integration. Imports have moved; decision policy and SQL formats have not.
No old import wrappers or alternate implementation remain. Serialization uses
record type names, not Python module paths.

Both writable stores and read-only readers share their decoder. Existing owner
load operations retain their import/cleanup behavior; the new readers use SQLite
`mode=ro` and `query_only` and never invoke it. Missing, uncommitted or unsupported
snapshots fail explicitly. ExecutionSnapshot is an immutable typed tuple containing
metadata, session, revision, retained trace origin, cleanup state and file size.

The one-shot app worker captures execution and verification using SQLite's backup
API, including committed WAL content. It checks integrity/schema, hashes ordered
rows with bounded memory, reopens using the canonical decoders, and checks the
rows are unchanged afterwards. It records receipt/revisions and a canonical
accounting digest at a fixed evidence time. It does not replay all retained traces;
the report explicitly records zero replayed traces. The worker releases its
full-history allocations on exit; this does not solve runtime memory scaling.

## Synthesis decision

Claude Opus 5.5 High and the independent Codex architecture candidate reviewed the
same grounded brief. Use **Codex's physical embedded package** as the base: its
boundary is explicit, dependency-tested and usable by both runtimes. Adapt
Claude's shared decoder, hash-proven packaging and explicit non-coherent snapshot
report. Reject generated AST-selected module assembly: it preserves fewer import
edits today but leaves the runtime boundary implicit. Reject adding a new HA
snapshot endpoint and UI workflow for this one-off operator rehearsal; these would
add lifecycle and permission surfaces before the real migration gateway exists.

## Tradeoffs and evidence

- We accept source under the integration subtree in exchange for unchanged HACS
  distribution and one canonical implementation.
- We accept mechanical import changes in exchange for an explicit package boundary
  and no standalone-import alternatives inside core.
- We accept full-history hydration in a separate one-shot process in exchange for
  checking the real format before runtime ownership moves.
- We accept independent per-database snapshots in exchange for leaving control
  operational during preparation; they cannot authorize migration activation.

Pre-extraction fixture hashes pin the serialized session, planner accounting and
crash-recovery replay. Packaged-core tests run in an isolated subprocess. Regression
coverage includes committed WAL/uncommitted writes, legacy cleanup preservation,
unknown schemas, corruption/count mismatch, interrupted capture and source
immutability. Existing accounting, late-correction and controller tests still run.

## Remaining work

Coordinator/cloud/tariff work and non-battery decision policy still need extraction.
The all-device authority journal, coordinated export barrier, JSON inventory and
import, post-export evidence, gateway, activation and HA entity projection remain
prerequisites to actual cutover. The app cannot yet control devices. Old unreferenced
files (`battery_policy_delivery`, `control_agreement`) found during live inventory
are preserved, not silently imported. No source cleanup is part of this milestone.

## Live HAOS rehearsal evidence

Validated on 27 September 2026 using app `0.1.0-beta.3` against databases written
by the still-running `0.9.0-beta.50` integration:

- SQLite backups: 310,894,592 execution bytes and 56,057,856 verification bytes.
- Execution revision 288065, receipt 487671; 342,709 meters, 144,962 observations,
  800 admissions and 6,165 retained traces. No pending attempts in this checkpoint.
- Verification revision 26044, with 4,324 records.
- Backup API completed in 1.491 seconds; full rehearsal in 47.923 seconds.
  The one-shot worker peaked at 474,276 KiB RSS (about 463 MiB).
- Both stores passed integrity, schema, canonical reopen and unchanged-content
  checks. Beta.50's original code independently reopened the **same frozen copy**:
  its serialized-session and accounting digests exactly matched the app core's.
  This comparison used the same captured accounting timestamp, not two moving
  live states. It took 31.066 seconds.

The private snapshots/report are retained at `/data/rehearsal-20260927-beta50` in
the app. They are rehearsal evidence, not active app state or a migration export.
All JSON stores, HA configuration, permissions and live database ownership remain
in HA. These measurements also confirm that full-history reopen is too expensive
to perform inside normal dashboard queries.
