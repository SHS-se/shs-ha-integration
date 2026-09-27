# App consolidation design

## Usage

```python
await configuration.change(request_id, expected_revision, edit)
# Returns only after the gateway durably enforces the new configuration.
# Conflict/invalid/unavailable are explicit; an unapplied edit is never success.

await gateway.connect(hello(app_version))
# Protocol capabilities admit a release, never the app's source-code hash.
await runtime_schema.open(identity, activation)
await engine.restore()  # committed account + mirror + unprocessed receipt suffix

await operational.commit(session, evidence_delta, mirror_delta, processing)
await inbox.retire_through(operational.completed_receipt)
```

## Problem and shape

The live runtime has already moved to the app. Its immutable migration proof,
activation and permanently fenced source remain intact. The current full-core
hash check prevents independently changing app code; mutable HA options and HA
editors still own settings. Accounting restores all historical tuples, and the
source mirror replays every delivered receipt.

Use a strict protocol major with named capabilities and informational release
versions. Keep installation identity distinct from compatibility. Explicit,
idempotent storage upgrades run under the existing exclusive writer lease and
reject unknown schemas. No hot patching, compatibility fallback or archive import.

The app configuration service is the only settings writer, including cloud
admission and device inclusion. It stores canonical options/credentials, desired
and applied revisions and idempotent request results. Under one lock it validates,
commits desired state, installs the execution/subscription projection at HA,
then marks applied and acknowledges. Recovery reconciles those two known states;
unexplained divergence fails closed. All UI and HA mode changes use the same CAS
path. Native dispatch rechecks session/configuration/mode and retains original
bindings for finite restoration obligations.

Port the existing editor behind an explicit configuration client; retain its
field IDs, shared validators and complete correction path. Do not emulate hass.
The app serves downloads directly. HA keeps stable entity identities, price
attributes and compact operation/status projections. Detailed policy and editor
modules leave the companion import closure.

Operational storage atomically commits account changes, a completed source mirror
and receipt progress. A partial receipt retains its predecessor checkpoint and
the incomplete receipt until deterministic sub-event recovery completes. Delivery
ACK never proves processing. Persist floor/high watermarks before pruning.

Exact historical correction needs indexed meter knots, edge/block contributions,
event deduplication and acknowledgement provenance, measured/observed values by
instant, reference changes and objective versions. Keep these compact facts on
disk. Expire detailed diagnostics after three days by local receipt time. The
domain evidence interface provides meter lookup/neighbours/measure, observation
lookup, objective history and receipt summaries; transitions append typed deltas,
not lifetime tuple copies. Worker transactions and explicit revision views keep
mutable database state out of immutable domain snapshots. Stream the existing
objective hash byte-for-byte. Schema migration builds and verifies a new active
store before the atomic switch, retaining one offline migration backup.

## Synthesis decision

Base: independent Codex candidate, for desired/applied configuration recovery,
complete deduplication provenance and exact correction semantics. Adopt from
Opus 5.5 High: isolate native wire records from the runtime reducer, persist the
entity catalogue, hash a compact committed checkpoint rather than the whole DB,
and reuse the editor through a backend client. Both reject raw service RPC,
fake-hass adaptation and age-based meter deletion.

Reject Claude's proposed credential ownership in HA: the approved scope puts SHS
configuration in the app. Reject irreversible settlement and discarding all
acknowledged meter events: acknowledgement is not finality. Preserve indexed
observations for newly introduced historical deadlines, not only known objectives.
Historical reference queries must remain exact; dropping old contract intervals
without preserving their required reference data is not justified by present-day
callers alone. No additional user decision is needed to preserve existing semantics.

## Tradeoffs and verification

We accept compact long-lived operational facts for unrestricted corrections.
We accept one coordinated companion upgrade for independent routine app releases.
We accept explicit unapplied configuration status for truthful disconnected edits.

Tests must falsify incorrect accounting with differential late/reset/capacity/
objective cases, interrupt configuration and storage transactions, verify no
duplicate native effects, and prove stable HA unique IDs. Startup/memory should
depend on current state and unprocessed suffix. Validate browser field navigation,
HA edit conflicts, direct downloads, all database diagnostics and live app-only
restart. Implementation status is tracked in [progress](app-consolidation-progress.md).

First unit: strict protocol negotiation and an explicit active-runtime schema
upgrade. A changed app release/core must connect to unchanged companion protocol;
missing capabilities and unsupported schemas must fail before command authority.
