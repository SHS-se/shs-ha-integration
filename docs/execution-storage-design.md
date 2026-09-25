# Execution storage and process ownership

## Problem and current implementation

The retired execution archive persisted content-addressed JSON fragments,
including individual fields. A live census on 25 September 2026 found 19,876
archive files (286 MB). It was designed to reuse immutable pieces but required
file indexing, garbage collection and a separate checkpoint publication step.
The runtime also retains the complete accounting history in RAM. These are two
separate problems.

The implemented replacement is a dedicated SQLite execution database per entry:
`.storage/shs_energy.execution.<entry_id>.sqlite`. It uses standard rollback
journaling and `synchronous=FULL`; a temporary SQLite journal file during writes
is normal. There is no growing population of evidence files. Existing
verification SQLite and configuration/coordinator JSON stores keep their owners.

**This change fixes file proliferation and archive persistence. It does not yet
bound the runtime's historical Account or remove startup hydration.** Disk
history is also retained: unlimited historical corrections cannot be supported
exactly after their evidence has been arbitrarily discarded.

## Usage (caller's view)

```python
# Composition supplies the path/executor and explicit one-way legacy reader.
storage = ExecutionStorage(path, run_blocking, legacy)
restored = await storage.load()  # None, or (metadata, immutable ExecutionSession)

# Normal command checkpoint: one commit includes both state and new evidence.
await storage.save(metadata_with_home_state_shell, next_state.execution)
# Only after success can HomeHost progress to command dispatch.

# Bootstrap before device authority is available uses the same transaction.
await storage.save(metadata_with_no_checkpoint, ExecutionSession(account=account))
```

The storage module has no Home Assistant dependency. Encoding, prefix validation,
SQLite access and hydration run in the supplied executor. A serialized owner
holds a lock until a worker commits or rolls back, even if its caller cancels.
Successful worker completion updates the owner's revision before cancellation
is propagated. Database failures raise and stop command dispatch.

## Shape

The application has four logical kinds of persistent data, with six SQL tables:

| Logical structure | Tables | Update and retention |
| --- | --- | --- |
| Current controller and planning state | `head` | Replace one row atomically; stores the HomeState shell, current session fields, request anchor/captured feedback, configuration context, counts and revision. |
| Physical accounting evidence | `meters`, `observations`, `reconciliations` | Append typed meter/SOC columns and infrequent reconciliation rows. Source timestamps are provenance; ordinals preserve receipt order. |
| Accepted plans | `admissions` | Append one complete plan per row; its intervals, objectives, recovery instructions and dispositions are JSON within that row. |
| Recent diagnostics | `traces` | Append new traces and delete the dropped prefix, retaining the runtime's existing maximum of 8,192. |

The current replan request is not another history: `request_replan()` already
replaces its single anchor. Diagnostics does not write a second copy of all
accounting evidence. A plan's scalar fields never become separate files.

`ExecutionStorage.load() -> tuple[dict, ExecutionSession] | None` reconstructs
and validates the domain account, checking each table against checkpoint counts.
`save(metadata: dict, session: ExecutionSession) -> None` stores the append delta
and compact checkpoint in a single transaction. Revision checks reject a second
writer. The storage validates immutable prefixes; it does not silently overwrite
old facts. Late corrections are new meter receipts, not updates to old receipts.
Indexes on meter stream/source time/receipt, observation time/ordinal, and plan
acceptance time prepare for explicit historical queries.

Only new rows are encoded and inserted. Unchanged tuples are skipped; a changed
history's prefix is checked in the worker. That check, the runtime's tuple
creation, and current account indexes still have history-dependent costs.

### Module map

- `execution_storage.py`: transactional persistence, append/trace retention,
  revision ownership, cancellation, schema validation and storage metrics.
- `execution_migration.py`: read-only legacy archive reader and scoped cleanup;
  no old-format writer or dual-write path.
- `battery_runtime.py`: supplies the controller checkpoint and current session;
  no archive roots, page collection or read-back durability inference.
- `plan_execution.py` / `home_runtime.py`: existing domain rules and immutable
  history remain unchanged in this storage unit.
- `resource_profiling.py`: fixed-size performance samples including storage
  counters; no record-per-sample files.

### One-way migration

Before a new database exists, load the legacy checkpoint and its referenced
archive, verifying content hashes. Import all retained evidence and the checkpoint
in one transaction. Reopen the database and compare the reconstructed session and
metadata with the imported values before deleting old files. Cleanup only names
this entry's exact old checkpoint and 64-character hexadecimal evidence hashes.
Persisted migration stages distinguish unverified imports from verified imports
awaiting cleanup. A crash before verification must resume the exact comparison;
a crash during cleanup resumes deletion without requiring already removed pages.

An interrupted first transaction leaves an empty, unversioned database and can
retry import. A committed database is authoritative even if old files remain.
Unknown schema, missing committed rows, or corrupt SQLite data raises; it never
falls back to the old checkpoint. A failed import leaves the legacy files intact.
No release rollback to the old writer is supported after migration.

## Synthesis decision

The architect review used independent Codex and Claude Opus 5.5 High candidates.
Codex's relational shape is the base: typed frequent records, JSON for whole
infrequent records, and all current checkpoint context in the same database.
Claude independently supported SQLite and separating durable history from the
working account. Both recommended implementing persistence before changing
accounting semantics.

The competing complete shape was segmented NDJSON, immutable binary indexes and
an atomically replaced checkpoint manifest. It permits easy sequential appends,
but historical corrections require random-access indexes, snapshots require
segment/index generation ownership, and compaction requires reader pinning and
crash recovery. SQLite already provides those mechanisms.

We rejected a duplicate generic event log alongside the typed evidence tables,
a second authoritative configuration checkpoint, and replacing the planner's
existing full-history digest with a hash chain. Those add storage or change
semantics without solving the immediate problem. A hot window starting at the
original balance anchor is also not a bounded-memory design.

## Next accounting boundary: bounded residency

This is designed but **not implemented** by the storage replacement:

```python
# One serialized worker owns the transaction and historical reads.
result = await journal.advance(event, now_ms)
host.publish(result.current_state, result.live_view)
await host.dispatch(result.effects)  # only committed effects

# Inside the worker, domain calculations receive explicit evidence access.
state, effects = reduce_home(head, event, now_ms, evidence=transaction.reader)
transaction.append(effects.evidence)
transaction.checkpoint(state)
```

`AccountHead` holds current contract, opening/current physical state,
receipt/generation and current request. `EvidenceReader` exposes domain reads
for meter neighbours, interval bounds, objective identity/dispositions and
historical outcomes. A fake reader supports deterministic domain tests; no
cursor, connection or lazy SQL-backed tuple reaches the HA event loop.

Persist the latest meter point at each source instant and interval contributions
between adjacent points. A correction updates only neighbouring contributions.
Indexed aggregates answer complete intervals; the existing boundary uncertainty,
epoch/reset and exact-deadline SOC rules remain unchanged. Objective versions and
current responsibilities use indexed projections; corrections invalidate affected
outcomes. Do not claim that an unindexed SQL sum or an ever-growing open-objective
list has constant cost.

Initially preserve the exact planner digest by streaming its canonical historical
rows through SHA-256 in the worker. That bounds temporary memory but still costs
historical CPU. Any different digest requires an explicit protocol change.

Before adopting this boundary, differential replay must match the full-account
oracle for late insertions, same-time corrections, resets, capacity changes,
objective amendments and restart. Profile progressively longer histories. A
normal tick hydrating historical lists disproves the design. Unresolved
obligations cannot be discarded simply to meet an arbitrary memory cap.

## Companion app deployment

A companion Home Assistant app is a suitable eventual host for the execution
ledger, historical calculations and detailed profiling. Keep a thin integration
for HA entities, configuration, measurement capture and final device-command
checks. The existing cloud planner need not move. HA documents app communication
through the Core API and a container-based app lifecycle:
[app communication](https://developers.home-assistant.io/docs/apps/communication/),
[app development](https://developers.home-assistant.io/docs/apps/).

The split gives separate process metrics, heap and restart lifecycle. It does not
reduce unbounded retention by itself, and containers still share host CPU/RAM.
Moving the controller across this boundary additionally requires explicit command
identities, durable acknowledgement, writer fencing and disconnect/reconnect
behaviour. Do not move it by replacing local calls with unacknowledged network
messages. This change keeps the storage portable; it does not deploy a new app.

## Validation and operational limits

Tests cover exact round trips beyond transport array/string limits, late
corrections, one-row append, no history writes on status changes, rollback at
checkpoint publication, stale writers, cancellation after worker commit,
trace trimming, missing rows, corrupt schemas, interrupted import, scoped cleanup,
cleanup retry, and real runtime command fencing after SQLite failure.

A local benchmark of diagnostic export 50 imported 256,284 meters, 109,594 SOC
observations and 795 accepted plans into **one 252.6 MB database**. Import took
3.57 seconds, restart/hydration 8.44 seconds, and five single-meter appends each
wrote one row (median 7.63 ms wall / 7.34 ms process CPU). These are development
machine timings, not measurements on the HA N100. The benchmark excludes traces
and runtime checkpoint context; it is not an exact disk-size comparison with the
live archive census.

Use `scripts/benchmark-execution-storage.py <diagnostic-export.json.gz>` to repeat.
The remaining startup cost confirms why a database alone does not solve RAM
retention. Storage resource counters report row counts, appended/deleted rows,
commit failures/timing, database bytes and migration/cleanup stage (0 complete, 1 awaiting verification, 2 awaiting cleanup). Encoded bytes
count JSON only, excluding typed SQL columns and physical disk-write volume.
