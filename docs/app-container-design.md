# SHS Home Assistant OS app: architecture and migration

Status: accepted architecture, 27 September 2026. The first installable observation
release is implemented; see [the app documentation](../apps/shs_energy/DOCS.md).
The first shared-core extraction and read-only snapshot rehearsal are implemented;
see [runtime-extraction.md](runtime-extraction.md). Full runtime extraction, durable
command handover and actual data migration below remain planned.
The user selected Home Assistant OS first. The cloud planner remains in its present service. UI details are
in [app-ui-design.md](app-ui-design.md).

## Usage: the user's and caller's view

The user installs the SHS app, opens its web interface through Home Assistant,
and completes a guided companion installation/migration. Their existing HA entry,
entity IDs, modes and device bindings remain recognizable. SHS has Overview,
Schedule, Controller, System and Settings screens. The runtime works with the
browser closed. Editing a binding from the app uses the same validation and
permission authority as editing it through an HA entity.

The following is a caller-first sketch, not executable implementation:

```python
# Container composition: one runtime owner, independently queryable views.
bridge = HomeAssistantBridge(core_connection, installation_id)
journal = await RuntimeJournal.open(Path('/data/runtime'))
home = Household(journal, bridge, cloud_client, clock)
web = AppWeb(home, journal.reader, bridge.configuration, diagnostics)
await serve(home, web)  # Python web server plus ordered runtime; one owner

# Settings: HA owns local bindings/permissions; app supplies its editor.
result = await bridge.configuration.apply(
    EditConfiguration(expected_revision=revision, changes=changes))
# Applied(new_revision) | FieldIssues(all_relevant_fields) | RevisionConflict
# Saving valid configuration does not enable control.

# UI reads never hydrate the full runtime account or take its dispatch lock.
timeline = await journal.reader.timeline(
    TimelineQuery(start_utc, end_utc, resolution='quarter_hour', devices=devices))
# Typed actuals, plan admissions, decisions, evidence, units and coverage.
```

Inside the runtime, input receipt, changed domain evidence, checkpoint and outgoing
intent commit together. An accepted input is acknowledged only after persistence:

```python
async def accept(batch: InputBatch) -> CommittedRevision:
    transition = runtime.receive(batch)  # ordered, typed domain input
    committed = await journal.commit(transition)
    runtime.install(committed.state)
    await bridge.acknowledge(committed.input_cursor)
    outbox.wake()  # only durable intents; delivery may later be uncertain
    return committed.revision

# Outbox worker, not a request handler and not the browser:
outcome = await bridge.execute(committed_intent)
await home.receive(outcome)
```

The gateway exposes a complete execution operation, not separate public
prepare/check/send/confirm calls that every caller must coordinate. Its internal
journal and native adapter own the necessary dispatch sequence.

## One-page rationale

### Problem

SHS currently shares Home Assistant's process, lifecycle and frontend extension
surface. Moving it requires more than packaging Python: local configuration,
recorder access, device authority, pending physical effects, cloud exchange cursors
and several stores cross the new boundary. The app should own the expensive work
and flexible UI while HA remains the place that can check and issue a device action.

### Shape

Use a Python application with its own web server, a compiled React/TypeScript UI,
SQLite persistence and one serialized household runtime. A small `shs_energy`
companion retains HA entity/recorder adaptation, local configuration and permissions,
final dispatch checks, stable entities, and durable command/authority evidence.
Keep economic planning in the cloud. Use HA Ingress for the browser and an
authenticated app-to-HA bridge through Core's supported API proxy.

### Synthesis decision

The independent candidates were produced by **Claude Opus 5.5, high effort** and
a native **Codex architecture subagent**, using the same grounded brief and read-only
repositories. Temporary candidate files remain outside the repository.

**Base: Codex's durable execution-gateway design**, selecting its HA-authoritative
configuration alternative for this HAOS-first release. It preserves a local final
permission decision and avoids two writers for settings and minimum-run obligations.
Compared with Claude's app-owned configuration and transient gateway, it accepts
a somewhat larger companion in exchange for clearer crash and correction behavior.

Adapt Claude's compact stream-oriented interface, small revision notifications for
the UI, stable `shs_energy` identity, and extraction of the website's pure chart
semantics. Choose ECharts for the chart prototype rather than requiring all new
interaction features to be written in custom SVG.

Reject these candidate details: automatic latest-claimant takeover; in-memory-only
command deduplication; arbitrary service payloads supplied by the app; treating
normal integration stop as a migration fence; dual HACS/app installer ownership;
simultaneously consolidating all databases and moving the runtime; and assuming
counter gaps are always exactly recoverable. Late evidence remains admissible in
receipt order. Source timestamps are useful data, not event-order rejection rules.
Claude's suggestion that historical backfill inherently violates receipt ordering
is not adopted.

### Tradeoffs accepted

- We accept a durable HA gateway in exchange for enforceable final dispatch and
  permission ownership close to the devices.
- We accept a controlled Core restart when companion code changes in exchange for
  verifying the code actually loaded, rather than only its files on disk.
- We accept a visible migration interruption in exchange for one control owner.
- We accept the app installer's writable HA configuration mount in exchange for
  installing the bundled companion from the app; this is broad filesystem access.
- We accept the current runtime's full-history residency during extraction in
  exchange for isolating migration from an accounting redesign. This limitation
  must remain visible and measured.

### Alternatives considered

| Architecture | Caller / ownership consequences | Why not the initial target |
| --- | --- | --- |
| HA runtime with an analytics-only app | Smallest migration, but heavy runtime stays in Core | Does not achieve the intended process isolation or meaningful container attribution |
| App runtime and app settings; HA transient transport | Fewer HA responsibilities; requires replicated permission projection and recovery policy | More authority transfer and migration risk than HAOS-first needs |
| App using ordinary HA services without a companion | Simple connection, but no SHS final epoch/attempt gate | Cannot enforce this handover and command-recovery contract at dispatch |
| Selected: app runtime, HA configuration and durable execution gateway | App caller sees domain operations; HA owns the few invariants it must enforce locally | More bridge work, but ownership and failure behavior are explicit |

### Open evidence and next step

First build a **non-controlling HAOS vertical slice**: bundled companion installation,
loaded-version handshake, ordered state stream, authenticated Ingress, existing icons,
resource metrics and one schedule query. Prove proxy authorization, prefix/deep-link
behavior and restart recovery on a test instance. In parallel implementation phases,
develop the all-device fence and replay seams before any live authority transfer.
No current household is migrated just because the app can start.

## Grounding in the current repository

| Existing boundary | Verified behavior | Design implication |
| --- | --- | --- |
| `__init__.py:async_setup_entry` | Constructs coordinator, controller, SQLite stores, battery runtime/fence; starts listeners and scheduled jobs | Extraction must untangle lifecycle and every writer, not copy the integration into Docker |
| `coordinator.py` | Reads HA recorder/states/registry and constructs cloud uploads, models, plans and cursors | Keep HA reads adapted behind the bridge; move models, aggregation and cloud exchange to app |
| `controller.py` | Calls HA services; persists overrides and minimum-run records | Dispatch/minimum-run protection remains HA-owned, economic request selection moves |
| `controller.py:async_stop` | Stops scheduling and retains last settings and journalled ownership | Normal stop is insufficient for migration fencing |
| `battery_writer.py` | Durable battery-only epoch/owner fence | Extend the invariant to every SHS actuator surface, not only the battery |
| `execution_storage.py` | Transactional SQLite, durable revision, append checks, cancellation settlement; still hydrates full histories | Preserve storage semantics first; containerization alone does not bound memory |
| `verification_storage.py`, `execution_migration.py` | Current SQLite plus existing one-way legacy import | Enumerate supported sources; do not guess precedence or copy all of `.storage` |
| `config_panel.py`, `control_configuration.py` | Shared configuration save/mode logic, structured field issues and HA WS API | App editors must use the same canonical validation/permission operation |
| `sensor.py`, `select.py` | Stable entry-based unique IDs and HA-facing execution modes | Preserve entry/domain/unique IDs; publish app projections through existing entities |
| `resource_profiling.py` | Bounded samples; process gauges currently cover all HA | New app metrics can attribute its own process/container, not the whole host |
| Website `PlanPanels.tsx` | Custom React SVG with pure series/geometry helpers | Share semantic transforms; do not assume the reference chart is Recharts |

This follows the existing companion-app direction in
[execution-storage-design.md](execution-storage-design.md), the
[constraint requirements](constraint-requirements.md), and the
[configuration error UX contract](configuration-error-ux.md). Earlier prototype
documents are rationale, not authority to restore superseded constraints.

## Module and ownership map

Keep app and companion in this repository so CI can test and package the pair.
Paths below are proposed, not created:

| Module | Owns |
| --- | --- |
| `packages/shs_core/` | Pure controller/accounting/planning transforms, existing domain types; no HA, HTTP or SQLite imports |
| `packages/shs_contract/` | Closed bridge messages, shared configuration fields/validation primitives and generated UI schemas; no duplicated hand-maintained protocols |
| `app/shs_app/runtime.py` | Single household event owner, deadlines, active plan and domain decisions |
| `app/shs_app/journal.py` | App checkpoints/evidence/outbox, durable input cursor, indexed view reads and migration staging |
| `app/shs_app/bridge.py` | Core transport, typed HA snapshots, history ranges, reconnect and receipt acknowledgement |
| `app/shs_app/cloud.py` | Existing planner API and upload cursors; economic contracts remain unchanged |
| `app/shs_app/web/` | Python HTTP server, Ingress trust/prefix, API, static React assets and view notifications |
| `app/shs_app/diagnostics.py` | Bounded operational metrics and scheduled database census |
| `app/shs_app/migration.py` | One restartable migration operation; inventory, export proofs, staging, activation and scoped cleanup |
| `app/shs_app/installer.py` | Exact bundled companion installation and resumable on-disk installation journal |
| `custom_components/shs_energy/` | HA configuration/permissions, discovery, recorder/state adaptation, authority/command journal, native dispatch and entities |
| `web/` | React screens using the same field catalog and SHS chart package as appropriate |

Start with one Python server process and one serialized persistence worker; no
multiple web workers that each start a controller. Use aiohttp initially, matching
the existing async HTTP code. Offload blocking persistence and expensive history
work; measure event-loop responsiveness under long histories. If CPU work still
starves the UI, split the runtime into a dedicated process inside the same container
with a single explicit owner, rather than increasing ASGI worker count blindly.
This is a measured redesign trigger, not a second runtime running in parallel.

Canonical writers:

- HA owns local entity bindings, operating permissions, control surfaces, minimum-run
  preparation, captured restoration baselines and the all-device dispatch fence.
  All app editors, native selects and HA services use one serialized operation.
- App owns controller decisions, active account/plan history, cloud exchanges,
  persisted analysis views and upload cursors. Its configuration snapshots are
  versioned read models, not a second settings store.
- Cloud retains economic planning preferences and plan production as today.
- HA recorder remains HA-owned. Browser state never owns runtime state.
- Only the gateway issues SHS actuator calls. User/other automation writes remain
  external physical actors; they are observed, not falsely claimed to be fenced.

Configuration updates invalidate affected execution grants before they are exposed
as applied. Every intent names the configuration/surface revision it was based on.
Stale intents are rejected at HA. Local permission revocation must work with the app
offline. Existing permitted handover/restoration semantics stay in the gateway;
there is no independent economic controller there.

## Type sketch and invariants

Pseudocode types deliberately omit serialization details:

```python
@dataclass(frozen=True)
class InputCursor:
    installation: UUID
    epoch: UUID
    sequence: int  # assigned locally in callback receipt order

@dataclass(frozen=True)
class ExecutionIntent:
    command_id: UUID
    decision_id: UUID
    authority_epoch: int
    configuration_revision: int
    surface_revision: int
    timing: DomainDispatchTiming  # existing device/request timing, no new TTL
    target: DeviceTarget  # closed device target union; not domain/service/data

DeviceTarget = BatteryTarget | EvTarget | SwitchTarget | TemperatureTarget
CommandOutcome = NotSent | Rejected | ServiceReturned | OutcomeUncertain
InputEvent = Observation | ConfigurationChanged | CommandEvidence | CoverageGap
ConfigurationResult = Applied | FieldIssues | RevisionConflict

class ExecutionGateway:
    async def execute(self, intent: ExecutionIntent) -> CommandOutcome:
        raise NotImplementedError

    async def synchronize(self, cursor: InputCursor | None) -> InputSession:
        raise NotImplementedError  # owns snapshot + ordered changes + resumption

class Migration:
    async def advance(self, migration_id: UUID) -> MigrationStatus:
        raise NotImplementedError  # resumes the state machine, never implicit takeover
```

Parse external wire/storage data once into domain types. Transport envelope checks,
identity checks and existing equipment/permission requirements are not statistical
forecast gates. Preserve late meter corrections and source timestamps as evidence.
No new confidence bounds or source-time ordering requirements are introduced.

Before dispatch the gateway checks the exact loaded release contract, installation,
authority epoch, configured target surface, current permission, existing native
guards, minimum-run obligation and any existing applicable dispatch deadline. The
timing field carries the current domain's requirements (including its explicit
absence of a deadline where appropriate); transport does not invent a freshness TTL.
It durably records dispatch preparation,
then rechecks revocable facts at the serialized service-call boundary. Adapter policy
has one source, even where both processes need a representation of it.

A command ID identifies immutable content. Reusing it with different content fails;
repeating it returns its stored evidence rather than repeating a possibly issued
service call. Multi-actuator transitions have per-step records and partial outcomes.
`ServiceReturned` never means measured delivery. Physical readback is separate.

## Disconnection, ordering and storage

Use one authenticated app-to-companion session through Core's WebSocket proxy,
plus bounded request/reply reads. Register SHS-specific commands scoped to the
installation/entry; don't give the UI a generic HA service execution endpoint.
The Supervisor token stays inside the container. Validate actual authorization
behavior against the HAOS target rather than assuming an internal address is trust.
[HA documents the Core proxy and token mechanism](https://developers.home-assistant.io/docs/apps/communication/).

Gateway receipts are sequenced in HA callback order and persisted before publishing.
App acknowledges only the cursor it committed with its checkpoint. Replay deduplicates
by installation/epoch/sequence. The UI has an independent lossy view-update feed;
closing a browser cannot acknowledge or lose control evidence.

The gateway keeps authority and unresolved command evidence durably, and a bounded
receipt spool for resumption. If observations can no longer be retained, record
coverage loss, stop accepting new intents dependent on that unestablished input
state, and synchronize explicitly. Never silently discard command uncertainty or
authority changes to satisfy a history cap. Storage failure cannot authorize a write.

A snapshot and post-snapshot changes need a HA-loop barrier: buffer callbacks while
capturing/persisting the snapshot, then deliver them in order. Buffer exhaustion
invalidates that snapshot attempt visibly. A crash before a callback became durable
is an explicit coverage boundary. Current state does not recreate missing history.
Recorder backfill is newly received evidence with original source provenance;
existing accounting determines what it can establish. Some counter gaps are
recoverable; resets, outages and ambiguous boundaries remain explicitly represented.

On disconnect/restart, revoke the volatile execution session; old epochs cannot
resume automatically. Recover the durable authority record and reconcile outstanding
effects before granting a new session. An epoch is scoped to the authorized app
installation, not awarded to whichever client connects last. Old pending effects
survive a new epoch. A connection change does not invent a universal device-off or
restore action: retain existing device-specific handover semantics.

Keep the existing execution and verification schemas for the first extraction,
with app-owned metadata/outbox as necessary. Do not assume a transaction across
multiple databases is atomic. Evidence needed to authorize an outgoing intent must
commit with its checkpoint/outbox in the owning execution database; unrelated
verification projections can lag with explicit revision markers. No read-only
dashboard query may hydrate the entire account on each refresh.

Bounded-history residency remains separate work under the existing storage design:
compact current state, indexed evidence reads, unchanged accounting digests and
differential replay. A container isolates heap and CPU attribution, not hardware
capacity or an algorithm whose cost grows with retained history.

## Bundling, installation and compatibility

The app can ship the matching companion and install it using a writable
`homeassistant_config` mount. This is our installer, not a built-in Supervisor
custom-integration package manager. Discovery only helps configure already-installed
code. The app uses persistent `/data`; its browser endpoint uses Ingress without
an exposed unauthenticated host port. HA's
[app configuration](https://developers.home-assistant.io/docs/apps/configuration/)
documents these platform facilities.

Build one immutable release manifest containing app build ID, companion build ID
and file hashes, protocol revision, and expected database schemas. CI packages the
same companion bytes into the image and release artifact. Validate the exact loaded
pair before normal runtime exchange/commands; no protocol fallback. A minimal stable
version-discovery operation remains readable so the repair screen can explain a
mismatch. A browser tab also refreshes when its UI/API build no longer matches.
An app-only release can name the unchanged companion build in its tested manifest;
that update does not itself require a Core restart. Companion code changes do.

Installer flow:

1. Inspect the installed source version and whether HACS or the app owns its files.
   Transfer update management explicitly; do not leave both managers active.
2. Verify the bundled artifact and stage only SHS's directory. Refuse unknown local
   modifications with a concrete repair action rather than overwriting silently.
3. Persist the installation phase before replacing the directory. Use a resumable
   directory-swap protocol; two renames are not an atomic exchange. A crash at an
   intermediate rename rolls forward from the verified staged payload.
4. Present the required Core restart as part of the operation. On first migration,
   follow the preparation/export sequence below before replacing the source runtime.
5. Compare the companion's **loaded code build ID** and protocol to the image's
   release manifest. Looking at the on-disk manifest alone is insufficient.

The practical guarantee is **a mismatched pair cannot control devices**. App and
Core updates are not one atomic deployment; there can be a visible restart/repair
interval. Bundling cannot guarantee compatibility with every future HA version.
CI must test the supported HA releases and both intended CPU architectures.

Own the broad mount's risk honestly: runtime module boundaries do not restrict OS
filesystem privileges by themselves. Scope installer paths and use an AppArmor
policy where verified, keep browser operations authenticated/admin-only, and never
serve configuration files. Ingress requires trusted-source filtering and prefix-aware
assets, routes and streaming connections, as specified by
[HA's Ingress documentation](https://developers.home-assistant.io/docs/apps/presentation/).

Do not automatically remove existing SHS HA services during extraction. Inventory
their use in automations; retain intentional supported actions through the gateway,
or explicitly design a breaking replacement. This is preservation of the existing
product surface, not a general legacy protocol compatibility mechanism.

Extend `RELEASING.md` and CI before publishing app artifacts. While integration code
changes, continue using `bash scripts/bump.sh beta` and include the bump in the same
code commit. Release workflows own tags. No release versions or tags are created
by this design document.

## One-off data migration

### Inventory and preparation

Retain the domain, original config entry ID, device identity and all entity unique
IDs. Entity registry records and HA recorder stay in HA. Cloud-owned data stays
in the cloud. Import only enumerated SHS data for the chosen entry.

| Source | Destination / treatment |
| --- | --- |
| Config entry data and options | Remain HA canonical for pairing/bindings/permissions; pass required cloud credentials privately to the app, never via browser diagnostics |
| `shs_energy.<entry>` | App plan/tariff/cache/exchange state and upload cursors |
| `shs_energy.battery_live_inputs.<entry>` | App input state with provenance and receipt continuity |
| `shs_energy.controller.<entry>` | Split deliberately: dispatch/restoration/minimum-run obligations to HA gateway; decision/history data to app; preserve overrides and retired restoration settings |
| `shs_energy.battery_writer.<entry>` | Generalized HA authority journal; old epoch/ownership evidence retained, all surfaces enumerated |
| `shs_energy.execution.<entry>.sqlite` | App execution DB, including head and every required evidence sequence |
| `shs_energy.verification.<entry>.sqlite` | App verification DB with record/metadata identity intact |
| Legacy verification/sample JSON and battery runtime/archive files | Normalize through the supported one-way source importer before export, or fail explicitly; no arbitrary merge of stale sources |

Unknown source versions/files are reported and preserved. A migration manifest
records source release/schema, entry identity, logical revisions, row counts,
content digests, included files, credentials presence (not value), pending effects
and the cutover barrier. A logical comparison, not raw database-file equality,
proves the destination represents the source.

First ship a **migration-capable preparation integration** that still runs the old
runtime but has a durable all-device fence, coordinated export and idempotent
migration status. The app may bundle this exact preparation payload as well as
the final companion, or direct the user to its exact release. The selected initial
packaging is to bundle both for a single guided app experience. This transient
source adapter is part of the one-off migration, not a legacy runtime fallback.

Install/restart into the preparation release before migration. Then preflight
available space, supported stores and configured devices while the old owner still
operates. Do not replace the old runtime with the thin companion before it can
export/fence correctly. A new installation with no old SHS data skips this phase.

### Durable state machine

| State | Durable fact and action |
| --- | --- |
| `Prepared` | Supported source loaded; migration ID and complete inventory fixed; app has no authority |
| `Fenced` | All old SHS writer paths denied under the shared authority boundary; pause scheduling, cloud/config mutations; settle in-flight work or classify uncertainty |
| `Exported` | Consistent immutable export at recorded receipt/config barrier; later observations are spooled by the preparation bridge |
| `Imported` | Data transformed into app staging; no dispatch capability |
| `Verified` | Reopened destination passes schema/integrity and logical parity checks; gateway state import and app checkpoints agree |
| `Activated` | Final companion loaded and exact pair verified; gateway durably binds migration ID, export digest and new owner epoch to the app checkpoint |
| `Complete` | App records activation proof, reconnect/readback reconciles obligations, stable HA entities confirmed; source cleanup is separately recorded |

The fence is persisted before any source store is declared frozen. Acquire/drain
every SHS command path, not only the battery lock. Permission revocations received
during migration still take effect; other configuration edits are explicitly held
until activation so they cannot invalidate the export unnoticed. Continue collecting
observations and uncertainty records through the preparation bridge.

Use SQLite's [backup API](https://www.sqlite.org/backup.html) for database snapshots;
coordinate JSON saves and every database writer under the export barrier. A backup
of one database alone does not make a multi-store snapshot coherent. Never copy a
live SQLite main file while ignoring its journal/WAL.

Transfer large exports as bounded authenticated chunks, verified against the
manifest, into a staging directory. Keep the export snapshot and retired app-owned
source stores unchanged after export. Canonical HA configuration/permission journals
and gateway observation/uncertainty evidence remain writable: replay their subsequent
deltas and reconcile their latest revisions before activation. An imported snapshot
must never restore a permission that the user revoked during migration.
Reopen staging and compare accounting balances, exact receipt order, active contracts
and objectives, upload cursors, controller/restoration state, minimum-run deadlines
and unresolved effects. Preserve planner digest semantics. Historical plan IDs can
repeat; use admission identity/receipt plus digest.

Activation is a two-sided idempotent protocol, not an atomic distributed transaction:
the gateway persists an activation token tied to the verified migration/checkpoint;
the app reads/persists that token before issuing an intent. A lost reply is resolved
by reading durable migration status, never by electing a second owner. Final
companion replacement/restart occurs with the migration fence already durable.

Before new dispatch, obtain current observations, replay post-export receipts and
reconcile existing uncertain commands. Retain the configured modes and minimum-run
obligations; imported settings do not newly grant control. Command ambiguity is
preserved even if the source application thought its last service call succeeded.

### Crash matrix and recovery

| Interruption | Recovery |
| --- | --- |
| Before durable fence | Preparation runtime remains the sole owner |
| Fence persistence uncertain | No app activation; reread source fence and keep writes denied until resolved |
| After fence, before export complete | Resume the same migration/export; never restart normal legacy control |
| Copy/import interrupted | Resume verified chunks or discard only this migration's incomplete staging and reimport the immutable export |
| Reopen/parity fails | Keep source and staging evidence; show exact failure; both normal controllers remain fenced |
| Companion directory swap interrupted | Installer journal completes verified replacement; no grant until loaded handshake |
| Activation committed, response lost | Query the stored token for the same migration; no new takeover |
| App/HA restarts after activation | Recover owner token, invalidate old session grants, reconcile outstanding effects before new commands |
| Cleanup interrupted | Resume only exact manifest-listed retired files; destination no longer depends on source |

Keep an immutable source backup until migration verification and a backup/restore
drill succeed. Cleanup is explicit, scoped and separate from activation. Never delete
current HA gateway journals or config-entry data. Recovery after fencing is roll-forward;
do not automatically revert to the old controller. A full system backup is an
operator recovery tool, not a runtime fallback.

## Implementation sequence and acceptance gates

| Step | Deliverable | Evidence required before advancing |
| --- | --- | --- |
| 1 | Non-controlling HAOS app/companion spike, existing icon reuse, Ingress, handshake and metrics | Target HA authorization, startup/restart, sidebar toggle, deep links and image rendering verified |
| 2 | Pure runtime extraction and narrow bridge interfaces | Existing tests/replay remain equivalent; no HA objects leak into core; no duplicate decision policy |
| 3 | Generalized HA authority/command journal and preparation exporter | All writer paths fenced; duplicate/lost replies, revocation during await, multi-step ambiguity and minimum-run crashes covered |
| 4 | Read-only app runtime and chart prototype using recorded/current snapshots | Schedule matches reference data; measured latency/memory; no actuator authority in this mode |
| 5 | Migration importer and final gateway installation | Full state/crash matrix, logical parity, source-version rejection and stable entity IDs on a HAOS test instance |
| 6 | UI configuration and controlled execution | Entire field correction path, offline revocation, reboot/reconnect, native guard and dispatch uncertainty tests |
| 7 | First explicitly scheduled household migration | Backup/restore rehearsal and accepted device-specific disconnect behavior; compare outputs and metrics after cutover |
| 8 | Bounded runtime residency, if profiling requires it | Long-history scaling and differential late-correction/accounting/digest parity before claiming bounded memory |

Specific architecture risks still needing evidence: exact HA proxy/admin permissions;
all-surface minimum-run/restoration ownership; performance of the current full-history
runtime inside the app; reliable snapshot/stream barriers; safe installer/HACS management
transfer; authorization of Appearance's sidebar toggle; actual sidebar rendering of
the existing brand assets; and deployment of the shared website chart package.

If the thin companion needs a second copy of the household controller to make these
checks work, this boundary is wrong and must be redesigned. If normal UI reads need
full-account hydration, the read model is wrong. If migration needs an unfenced old
writer after export, the migration protocol is wrong. Do not add ad hoc branches to
conceal any of those failures.
