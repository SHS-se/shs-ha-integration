# Household runtime and restart journal prototype

The next implementation stage after the offline battery compiler now has a pure,
executable command protocol and checkpoint format. It exercises the architecture
in [shared-entity reconciliation](../../smart-home-solutions-t-by/docs/energy-optimisation/control-reconciliation.md)
without attaching it to the shipping `ScheduledController`.

## Caller usage

```python
from home_runtime import create_home, reduce_home
from home_runtime_checkpoint import encode_checkpoint, restore_checkpoint

state = create_home(group_specs, limits)
state, effects = reduce_home(state, event, now_ms)
# The host installs the returned state before handling effects.
# Persist requests storage; only its durable acknowledgement can produce Send.
checkpoint = encode_checkpoint(state)
resumed, effects = restore_checkpoint(checkpoint, restart_time_ms)
# Restore emits Persist, ConfirmAuthority and Observe, never Send.
```

These imports assume the pure module directory is on the Python path, as in the
tests and replay script. The modules require Python 3.10 or newer; validation here
used Python 3.13.

Run the committed synthetic crash trace from the integration repository:

```bash
python3.13 scripts/replay-home-runtime.py tests/fixtures/home-runtime-crash-recovery.json
python3.13 -m unittest discover -s tests -p test_home_runtime.py -v
```

The trace persists a preparation, emits one write, crashes before the subsequent
state is persisted, and resumes from the preparation. Early matching readback
keeps uncertainty. Fresh readback after the declared latest-effect bound settles
it and adopts the request without another write. The fake `limit_w` control is
only a protocol fixture; it is not a Sigenergy command or a power-delivery model.

## Ownership and interfaces

`home_runtime.py` owns immutable household state and one synchronous reducer.
Observations, operating authority, desired requests, adapter proposals, journal
acknowledgements and transport outcomes enter as data. Effects request persistence,
dispatch, observation, current authority, transition calculation and timed wakeups.
The reducer performs no I/O and never waits for a device. Other groups can advance
while one group waits for confirmation or persistence.

Each physical actuator group declares its complete control surface and conservative
maximum import/export envelope. A `Request` names a complete target, revision,
absolute expiry and native guards that must hold for every write and final adoption.
An adapter `Proposed` transition names the current generation, request revision,
observation revision and adapter revision. Its absolute assignment steps each
declare the complete preceding control state, additional native guards, possible
electrical effects, confirmation timeout, latest-effect delay and explicit
identical-repeat evidence. Partial control surfaces and invalid transitions fail
validation. Native measurements and electrical observations remain separate.

`home_runtime_checkpoint.py` owns the closed version-2 JSON codec and conservative
restore. Unknown fields, incompatible versions, nonfinite values, invalid counters
and inconsistent attempt/sequence identities are rejected. There is no legacy
checkpoint migration. The replay script simulates ordered durable writes in memory;
it does not implement an on-disk journal worker.

## Protocol guarantees exercised

- A preparation is persisted before its exact journal revision can authorise a
  send. Dispatch rechecks current authority, generation, request expiry, native
  guards, observed controls, freshness and the shared resource envelope.
- There is at most one active sequence and one unsent preparation per group.
  Superseding software work retains commands that may still affect the device.
- Transport acceptance is not physical confirmation. `not_sent` releases only
  the named attempt. Ambiguous attempts retain their possible effects until fresh
  observation after the adapter's latest-effect bound; the deadline alone is
  insufficient. Incompatible writes wait. Identical repeats require explicit
  adapter evidence and retain each attempt separately.
- Retry deadlines persist across new plans, mode changes and restart. Attempts
  use bounded exponential pacing. Invalidations do not permit an immediate burst.
  The attempt limit blocks further preparation without discarding uncertainty;
  fresh post-bound observation permits automatic recovery.
- Controlling mode corrects external drift automatically. Monitoring, Planning
  and Control Verification do not originate optimisation writes. Mode exit fences
  optimisation immediately; an explicitly approved release request can finish
  handover. A missing release is visible as `release_required`, with no invented
  device settings. Old mode revisions cannot restore desired authority on re-entry.
- Restart retains requests, absolute deadlines, retry pacing, attempt identities
  and unresolved effects. Even a journaled preparation becomes ambiguous because
  dispatch may have happened after that snapshot. Fresh authority confirmation,
  a physical frame and readback are required before further work. An unchanged
  satisfied request is adopted without a default-mode cycle.
- Clock rollback suppresses dispatch, invalidates freshness and unsent plans, and
  retains incoming authority changes and transport evidence. Recovery requires the
  clock to reach its high-water mark and new observations. Stale requests still
  allow expiry/recovery processing for other groups.

Reservations take the maximum of mutually exclusive observed/pending possibilities
inside each group, then sum independent groups plus external demand once. Stale
groups reserve their declared maximum. This is conservative aggregate electrical
protection. It is not phase modelling, delivered-energy accounting, or credit for
an unconfirmed reduction elsewhere. Shared physical equipment must belong to one
group, or have a future explicit shared constraint; falsely declaring independence
would invalidate this calculation.

## Requirements for the future effect ports

The host must have a single state owner and marshal all events onto its loop.
State installation and handing a Send to the transport must have an ordered commit
boundary: do not leave an unfenced Send queued across a subsequent authority event.
The port must refuse dispatch at or after `send_by_ms` and return definite
`not_sent` evidence when it can prove no command escaped. Ambiguous transport
failures must remain ambiguous. Device control state must be read back; a service
receipt is insufficient.

The persistence worker must durably and atomically store whole checkpoints before
acknowledgement, preserve write order, and acknowledge each required preparation
revision. It must not replace a newer durable snapshot with an older one or silently
coalesce away a preparation acknowledgement. A failed or oversized checkpoint must
produce `JournalFailed`; retry the outstanding persistence while its preparation
is valid. Current observations and independent work continue during failure.

Wakeups, observation requests and transition calculations need bounded/coalesced
queues without dropping authority or transport evidence. Observation revision
counters must continue above their persisted watermark after restart. Current
authority must come from the authoritative configuration source, not an echoed
checkpoint. Frame demand must exclude all modelled groups to avoid double counting.

An adapter's repeatability and latest-effect evidence must be commissioned on the
actual equipment and transport. `evidence` is an explicit contract identifier,
not proof that such testing has happened. The latest-effect deadline includes the
whole permitted dispatch window. Every attempt's envelope must cover the physical
states its assignment can cause, including uncertainty. The pure runtime cannot
establish these device facts itself.

## Bounds and validation

Current bounds are 32 groups, 32 control/measurement values and native guards per
record, 8 steps per transition, 64 unresolved attempts per group and 1 MB per encoded
checkpoint or input trace. These limits apply together: a configuration below each
cardinality bound can still exceed the byte limit and must fail persistence rather
than evict unresolved attempts. The replay reader accepts at most 10,000 records.

Validation on 14 September 2026: 529 Python tests passed, including 22 new runtime
tests; 58 frontend tests passed; integration compilation passed. Tests cover crash
cuts at prepared/sent/accepted/ambiguous states, late effects, safe retries, drift,
mode exit, expiry, journal failure, independent progress, shared reservations,
rollback and corrupt checkpoints. The replay fixture is tested through its CLI.

A local Python 3.13 timing probe exercised 32 groups dispatching one prepared step
each: 100 warmups and 1,000 immutable reducer calls, with a 60,220-byte checkpoint.
Decision time was p95 1.86 ms, p99 1.93 ms, maximum 2.09 ms. This is a synthetic local
measurement, not a maximum-state/event-rate benchmark on the slowest supported HA
host. Queue, storage, observation and physical response latency remain unmeasured.
The architecture's production timing targets remain unverified.

The design used immutable whole-home snapshots rather than a mutable event-sourced
runtime: this keeps one ownership boundary and makes crash cuts directly replayable,
at the cost of copying and whole-checkpoint writes. Independent Codex review found
missing request guard enforcement and a rollback path that dropped authority;
both were corrected and regression-tested. Claude review was deferred with the
user's agreement because of allowance limits.

## Remaining implementation work

The [actual energy ledger](energy-ledger.md) is now implemented inside the same
household checkpoint, with gross directional counters, explicit epochs, bounded
watermark queries and replay/restart tests. It remains separate from reservations.

1. Bind accepted household policy and current economics to runtime requests, with
   the specified contract validation, expiry and shared allocation rules. The
   offline battery compiler is not yet an executable runtime wire contract.
2. Commission the battery adapter's transition, repeat, ordering, readback and
   latest-effect contracts, including approved Maximum Self Consumption handover
   with fresh rated limits. Then implement journal, event, timer and transport
   ports and connect them through tested verification and rollout stages.
3. Extend equipment support in the agreed order: battery, pool, then car. Generic
   protocol ownership can be reused; every supported group still needs its own
   validated device capabilities and transition evidence.

**Thermal models are deferred and remain a significant area of work.** Thermal
state, losses, heat-pump performance, competing circuits and forecast uncertainty
need general models before fitting unusual household installations. This runtime
does not infer a general heat-pump model from Phil's pool/hot-water configuration.
House-specific notes remain separate from the general architecture. Direct user
controls and the notification framework remain outside this stage.
