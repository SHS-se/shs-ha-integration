# Runtime move and cutover

Status: implementation in progress, 27 September 2026. The preparation release
remains the sole live owner until the complete replacement passes its gates.

## Usage

The app imports the same domain package as the integration tests. It opens
verified stores without starting effects, connects to the exact paired gateway,
reconciles current configuration and physical state, and only then starts jobs:

```python
imported = import_export(export_path, runtime_root, pair)
engine = await Household.load(imported.stores)  # dormant, no dispatch
session = await gateway.connect(imported.identity, pair)
await engine.reconcile(session.snapshot)
activation = await gateway.activate(imported.proof, engine.ready_proof())
await engine.start(session, activation)
```

Controller decisions call `capture`, `apply`, `release`, and battery transition
operations with a session/configuration/policy revision. They cannot name an
arbitrary service. The gateway revalidates current permissions, bindings, native
bounds and minimum-run obligations after durable preparation and immediately
before dispatch. A repeated command identity returns its recorded outcome.

## Shape

The app owns cloud exchange, forecasts, plans, controller policy, HomeHost,
execution accounting, verification and diagnostic history. HA owns canonical
configuration, physical observations, recorder access, captured originals,
minimum-run obligations, battery grants and native command dispatch. The existing
sensor/select unique IDs remain stable; their data comes from compact projections.

Use explicit observation, history, configuration, gateway and projection ports.
Do not imitate `hass` in the app. Native command validation has one implementation,
shared by physical execution and verification. The gateway never loads full
accounting history. Battery steps must belong to a persisted, admitted native
transition, rather than trusting a raw register write from the app.

Receipts use local sequence numbers in callback order. Persist command evidence
and observations before advertising them; app consumption checkpoints accompany
the domain transaction that consumes the receipt. Device timestamps remain
provenance, never an ordering gate. Separate delivery acknowledgement from domain
processing. Reconnection creates a new session, invalidates old command authority
and records a coverage gap; it cannot silently resume a previous session.

Cold export remains immutable. Import validates the complete file catalog, paths,
hashes, Store envelopes, database schemas, logical contents and canonical accounting
at the export evidence time. New app stores and a new gateway journal are bound to
one migration/export digest and release pair. The old fenced journal is never
unsealed or repurposed. Historical uncertain commands are evidence, never a queue
for replay. Configuration is re-read from HA at activation.

Activation is durable and idempotent on both sides. A lost reply is resolved with
the same activation identity. Dormant load and physical reconciliation precede
activation; activation precedes controller and cloud jobs. Retain every source and
failed staging attempt; no cleanup or automatic legacy fallback is part of cutover.

## Synthesis decision

Use the independent Codex candidate as the base: full domain split, durable receipt
processing, a new gateway journal and dormant import validation. Adopt Claude
Opus 5.5 High's shared native-command validator, explicit caller ports, compact
entity projections, and in-process extraction before wire composition. Both
candidates reject a fake HA object, a generic service RPC, duplicate controllers
and an observer-only endpoint as the final result.

Reject Claude's volatile-only observation ring because restart/replay needs a
consumer checkpoint tied to accounting persistence. Reject rewriting the sealed
source authority to `app`: a distinct gateway journal keeps source fencing
irreversible. Clean shutdown is not proof that all historical uncertain commands
are resolved. Do not claim at-least-once processing without a durable consumer
watermark. No arbitrary new forecast or source-time validity bounds are introduced.

## Tradeoffs and gates

- We accept a substantial local device gateway in exchange for fresh local
  enforcement and durable restoration obligations.
- We accept extraction before cutover in exchange for exercising the same policy
  through in-process and encoded ports with existing regression scenarios.
- We accept an explicit outage and retained source copies in exchange for a
  coherent one-off migration with a single writer.
- We accept full-history hydration for parity checks in a worker; redesigning
  accounting memory growth is a separate task.

First isolate native execution and ownership, retaining behavioral assertions for
EV, pool, generic devices and battery. Then extract the household/runtime ports,
add encoded transport and projections, and implement import/activation against the
actual composition. Test interruption and reply loss, revocation after preparation,
old sessions, receipt replay, pending restoration/minimum runs and stable entity
IDs. Only then publish the final pair and perform export, import, companion install,
Core restart, reconciliation and activation in that order. Installing the final
companion before exporting would invalidate the preparation release's export check.

## Implemented extraction and dormant importer

The first in-process extraction moves native action validation and dispatch,
DeviceOwnership persistence/decoding, permissions, device bindings and minimum-run
rules into the packaged core. There is now one actuator `async_call` adapter at HA
composition; controller policy and BatteryRuntime receive `NativeExecutor`.
Metadata and permission changes during journal preparation are checked before the
call. Service completion still does not claim physical delivery. Controller policy,
restoration orchestration and scheduling remain in HA at this intermediate stage.

`python -m shs_app.migration_import --export EXPORT_DIRECTORY --source-release VERSION`
performs a dormant import in the app container. It verifies the complete cold-export
catalog, sealed source authority, private copies, canonical database contents,
accounting digest and ownership decoder. It keeps app stores in `stores/` and
retains command/ownership evidence in `gateway_seed/`. An exclusive import lease
prevents competing workers; interrupted staging directories are retained. A retry
must use the same export and target pair and must find unchanged imported bytes.
The `import.json` proof is published last and explicitly lists runtime validation,
gateway seeding, physical reconciliation and activation as unfinished. This worker
cannot activate control, and it must not be used to justify fencing the live owner
before the remaining runtime replacement exists.

The second extraction packages the actual controller, battery runtime, verification,
configuration validators, plan validation and presentation code in `shs_core`.
Controller policy now receives `ControllerInputs` (state reads, temperature unit,
entity platform) and `NativeExecutor`; it no longer receives an HA object or entity
registry. Native dispatch remains injected at HA composition. A subprocess test
executes pool and battery scenarios from a copied distribution with no HA module,
then closes both owners. The existing controller assertions and accounting digest
checks still run against the canonical package.

Distribution metadata: the app copies the integration manifest beside `shs_core`,
just as it sits beside that package in HA. `api_contract` reads this explicit paired
manifest path, independent of working directory. This keeps versioned diagnostic
and cloud payloads tied to the exact bundled companion while the cloud client is
still being extracted; no alternate version or import fallback is introduced.

Remaining cutover work is substantial and explicit: durable receipts and gateway
session/activation, remote ownership/restoration operations, compact HA projections,
and the real dormant engine reconciliation.
The shared runtime existing in the image does not by itself mean it is the owner.

## Live evidence from the first extraction

Commit `7d38720` was published and deployed as integration beta.53 and app beta.5.
Both release workflows passed; the app reports Connected and the integration
reports Ready. The existing SHS icon assets are unchanged. An authenticated
Supervisor `/core/websocket` probe completed `auth_ok` and `get_config`, confirming
that the planned transport requires no new app permissions. No export, migration
fence or live ownership transfer was performed.

Commit `5593b40` was subsequently deployed as integration beta.54 and app beta.6.
CI passed 981 integration tests, 30 app tests, 93 configuration frontend checks
and six browser scenarios, and published both app architectures. Core returned
in 100 seconds; an SHS-only reload also returned HTTP 200 with
`require_restart: false`. The installed app imports the packaged controller and
battery runtime without importing HA, and its ownership decoder reads the live
minimum-run record. The live command journal still names `integration` as owner,
release beta.54, migration ID null, with 21 `service_returned` command outcomes.
There is no `/data/migrations` directory. These are extraction/deployment checks,
not evidence of completed household migration.

## Household and cloud extraction

`Household(ports, client, store=..., battery_inputs_store=...)` now owns the existing
cloud exchange, plan cache, snapshot construction, tariff calculations, forecast
resampling, actual/thermal aggregation and battery input scheduling logic. Construction
and `async_restore_plan()` are inert; neither schedules work nor sends commands.
The app distribution contains this exact implementation and the existing `ShsApiClient`.
The HTTP client remains the one explicit `aiohttp` dependency in the shared package;
all other imports remain standard-library or package-local. Both runtime hosts already
use aiohttp. Release CI now installs the pinned app dependencies so it executes the
real HTTP client instead of replacing its imports in tests.

`HouseholdPorts` supplies home facts/timezone, canonical options/admission, observations,
inventory, history/forecast reads, clock, repairs, publication and task lifetime.
HA's `ShsStatusCoordinator` is a composition adapter using the shared household and
HA's existing DataUpdateCoordinator listener/debounce machinery. `RecorderSource`
reads recorder rows and the read-only weather forecast service; calculations stay
in the household. Neither a synthetic HA object nor a config-entry facade is used
by the domain package. Native actuator dispatch remains at its existing HA boundary.

Configuration admission is awaited and compare-and-set: acknowledgement must follow
successful canonical update, and a conflicting edit cannot be overwritten.
Attention data remains in the household, while its single repair projection is
published by HA. Raw observations and half-open statistic windows retain the existing
semantics; local calendar calculations use the supplied home timezone, including
23/25-hour days. The canonical configuration resolver remains shared with the editors.

Tests now construct the real household for plan retention, rejected replacements,
replan recovery and source-port scenarios. An isolated copied distribution executes
those scenarios without HA. Adapter tests cover listener delivery, repair projection,
canonical admission, recorder start-state/attribute flags and read-only weather calls.
This is an in-process extraction milestone, not remote gateway activation or completed
live data migration. Durable receipts, remote ownership, final dormant reconciliation
and activation remain the next cutover gates.

### Live verification of the household extraction

The first deployment (`8bb8a54`, beta.55/app beta.7) exposed a battery recovery
caller still using the removed coordinator observation helper. `f9f6d75` moves
all battery reads onto `coordinator.ports.battery_report` and adds a composed test
that reopens a persisted BatteryRuntime using the real Household. This is covered
by the isolated distribution test as well as the regular runtime suite.

The corrected pair, integration **0.9.0-beta.56** and app **0.1.0-beta.8**, is
published and installed. CI passed **993 integration tests**, **30 app tests**,
**93 configuration frontend checks**, and **six browser scenarios**. Metadata and
existing SHS branding checks passed; both container architectures were published.
Core returned in 101 seconds and SHS loaded successfully. The app reports
Connected; the household reports Ready/actionable. Battery state is controlling,
command state adopted, execution active, plan accepted, and current runtime reason
null. EV, pool and hot-water controllers retain their configured verification mode.

The live app imports `shs_core.household` and `shs_core.api` without HA; its bundled
manifest names beta.56. The source command journal still records integration
ownership, release beta.56 and a null migration ID (297 completed service calls at
the read). No `/data/migrations` exists. A single startup observation-scope fault
remains in diagnostic history, but current battery execution recovered and its
runtime reason cleared. A supplier-cost price lookup warning was also logged;
these checks do not claim historical diagnostics are empty. No export, source
fence or live ownership transfer occurred during this extraction.

## Durable gateway transport foundation

Integration beta.57/app beta.9 package the first protocol-2 transport components.
They are **not registered or activated in the live composition** yet. The current
observer endpoint remains protocol 1. This release cannot be used to justify
fencing the live source or transferring control.

`seed_gateway(imported, ha_storage, require_core_stopped=...)` verifies the dormant
import, checks that Core is stopped, takes the source process lease, and compares
sealed command history and captured ownership against the export. It creates a
new gateway SQLite database bound to the exact entry, migration, export digest and
release pair. Repeating the same seed is idempotent. Partial or conflicting seeds
are retained and rejected. Imported prepared commands become uncertain historical
evidence; no imported command becomes a dispatch queue and the source stays sealed.

`GatewayJournal` persists connection generations, revocation, configuration
revisions, arrival-ordered observations, delivery acknowledgements and activation
evidence. Each reconnect revokes the previous session and records a coverage gap;
a delayed close from the old socket cannot revoke its replacement. Snapshots are
consistent database reads and cannot acknowledge delivery. The local activation
commit is idempotent for a lost response, but is deliberately absent from the wire
protocol until the real physical-reconciliation validator exists. A recorded
activation is historical evidence, not sufficient authority for a new session.

`GatewayStream` captures copied observations synchronously in callback order and
persists them with one worker. Its bounded queue fails closed on exhaustion;
failed storage prevents queued responses from being published. Source timestamps
remain provenance, including equal timestamps and decreasing timestamps. Cancelled
connect requests settle before their new session is revoked.

`GatewayClient` uses the HA WebSocket authentication/result envelope and the closed
`GatewayConnection` protocol. The separate-process test fixture exercises this
protocol with real sockets and SQLite; it is not an HA installation test. The final
companion still needs to register the authenticated HA endpoint and attach source
callbacks. Receipt storage in `ReceiptInbox` commits before delivery acknowledgement.
Lost replies require an explicit new connection; there is no silent session reuse.

Delivery is separate from processing. `processing_checkpoint` produces metadata
for the existing `ExecutionStorage` transaction, which atomically commits domain
state, accounting and partial-receipt progress. Tests prove rollback and reopen
consistency. Wiring deterministic receipt sub-events into the actual runtime
consumer remains unfinished; this foundation does not yet claim end-to-end
at-least-once processing.

The next implementation boundary is the semantic device gateway: captured originals,
minimum-run obligations, admitted battery transitions, and permission/session checks
immediately before physical dispatch. After that, compose dormant runtime loading,
receipt consumption, projections, physical reconciliation and activation. Only the
complete replacement passing those tests can precede the one-off live cutover.

Local validation for this foundation passed 1,009 integration tests, 39 app tests,
93 configuration frontend checks and six desktop/mobile browser scenarios. The
app metadata check confirms that the existing SHS icon bytes remain identical in
the integration, app-store assets and web interface. No live export, source fence,
gateway seed, app activation or HA restart was performed for this foundation.
