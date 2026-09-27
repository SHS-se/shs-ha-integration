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

Remaining cutover work is substantial and explicit: household/coordinator and cloud
ports, durable receipts and gateway session/activation, remote ownership/restoration
operations, compact HA projections, and the real dormant engine reconciliation.
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
