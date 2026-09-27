# App consolidation implementation

Scope: [approved requirements](app-consolidation-requirements.md). The existing
runtime cutover remains active; this work upgrades it in place.

## Verification baseline

27 September 2026, commit `6d064c4`:

- Integration suite: 1,034 tests passed.
- App suite: 50 tests passed, including separate-process socket tests.
- Deployed companion: `0.9.0-beta.60`; app: `0.1.0-beta.14`.
- Existing activation and permanently fenced source journal verified unchanged.
- A live socket disconnection recovered through the existing reconciliation path.
  The disconnected period and delayed battery measurements require verification
  again after the recovery/storage changes.

## Delivery checklist

- [x] Synthesize independent Codex and Opus 5.5 High architecture reviews.
- [x] Separate protocol capabilities, installation identity and storage schemas.
- [x] Extract the companion's native execution and wire dependency boundary.
- [x] Move canonical configuration and credentials into app storage.
- [x] Route app edits and existing HA mode entities through acknowledged revisioned changes.
- [x] Move configuration editors and diagnostic downloads into the app.
- [x] Atomically checkpoint the source mirror and retire processed transport receipts.
- [x] Replace lifetime accounting hydration with indexed operational evidence.
- [x] Apply three-day detailed diagnostic retention and expose all active database metrics.
- [ ] Publish, upgrade and verify live controls, entity identities and app-only restart.

## Verification requirements

Configuration: concurrent revision conflicts, disconnect/retry, persisted versus
applied status, native permission revocation, and the complete field correction
path in both browser and HA controls.

Storage: compare exact accounting and planner feedback with the current model for
late corrections, resets, equal-source-time replacements, old-deadline objectives,
capacity changes and duplicate events. Inject interruptions at checkpoint, prune
and schema-switch boundaries. Preserve unresolved native effects and restoration.

Live: same installation/activation, unchanged HA entity unique IDs, prices and
control modes, diagnostics download, successful native command results, app restart
without Core restart, bounded receipt lag and measured startup/resource behavior.

Operational meter/objective facts required for unrestricted late corrections have
a different lifetime from detailed diagnostic documents. A three-day diagnostic
window is not authority to forget unresolved accounting or reject old evidence.

## Stage 1 — protocol and native dependency extraction

Implemented protocol 3 with strict capability admission, an explicit app runtime
schema record, and preservation of immutable import/activation identities. Release
versions are informational after the active upgrade; cold imports still verify
their original pair. Native command records and power-reading validation no longer
import the runtime reducer. Removed the unused HA household coordinator and bound
sensor annotations to the actual gateway projection.

Validation: 1,034 integration tests and 53 app tests passed. A subprocess imports
the native gateway without household, battery runtime, accounting or execution
storage. App restart tests change both release version and core hash and retain the
activation. Schema tests reject unsupported/foreign state without rewriting it.
This stage is not yet deployed; packaging/UI/configuration changes follow before
the coordinated companion update.

## Stage 2 — app settings and downloads

Canonical settings and credentials now live in the private app record. Desired
and applied revisions separate persistence from HA acknowledgement; browser edits,
cloud admission and HA mode entities use that writer. HA retains a durable native
replica and removes its old options and cloud token after adoption. Discovery and
configuration views run in the app. The existing editor is part of the hashed app
bundle; the integration cogwheel redirects to it. Controller downloads are served
and compressed by the app, without WebSocket chunk transport.

Validation: 1,031 integration tests, 58 app tests, 93 editor tests and eight desktop/
mobile browser checks passed. Removed tests of deleted HA editor endpoints were
replaced by app configuration acknowledgement/conflict/recovery coverage and browser
save checks. Browser testing caught and fixed a blur redraw dropping the Save click.
Branding and mobile layout inspected. This stage remains undeployed while storage
and the remaining native projection boundary are completed.

## Stage 3 — atomic source checkpoints and transport retirement

The execution transaction now stores the completed source mirror using changed
source rows and a context checkpoint. Partial receipt commits retain their
predecessor mirror. Startup restores that checkpoint, while the first upgrade
reconstructs it once from the old inbox. Reconciliation hashes the compact
checkpoint rather than the complete execution database.

Inbox and gateway schema upgrades add explicit floor/high watermarks. Only completed
execution progress authorizes retirement; delivery acknowledgements alone cannot
prune. Ordinals remain monotonic even when every transport receipt has been retired.
A reader behind the floor fails explicitly. The sealed migration source remains
untouched.

Validation: 1,033 integration tests and 59 app tests passed, including interrupted
partial/complete checkpoint writes, retirement followed by restart, unchanged app
activation and rejection of unprocessed acknowledgements. Not deployed yet.

## Stage 4 — indexed operational evidence and recent diagnostics

App execution now restores immutable SQLite evidence views instead of hydrating
lifetime histories. Materialized meter edges and daily sums preserve exact interval
bounds, including late replacements and resets; ordinal prefixes preserve old
views. Objective versions, acknowledgements and deduplication remain exact. Upgrade
creates a consistent offline backup and checks accounting parity before activation.
Interrupted verification resumes explicitly. Source checkpoint reconciliation hashes
exclude changing query metrics.

Detailed verification, resource samples and execution traces now have a maximum
three-day age, with existing count limits also applying. Operational evidence and
physical obligations are not aged out. The System page inventories active app and
companion databases, table sizes/counts, retained ranges, receipt progress and
separate offline archives.

Validation: integration suite 1,033 tests (one fixture updated to use timestamped
traces, its 21-test suite rerun); app suite 65 tests plus two additional interruption
checks passed; web build passed. Six indexed-store differential/runtime tests cover
unordered corrections, reset edges, historical views, actual battery dispatch,
restart without history hydration, interrupted verification and transaction rollback.
Production-snapshot parity and live deployment verification remain outstanding.

## Stage 5 — thin native entities and release preparation

HA now renders app-calculated sensor descriptors/values and mode explanations.
The native entity catalogue persists independently of live values: app downtime
marks entities unavailable and preserves identities through a Core restart.
Detailed runtime/plan documents no longer cross the entity projection. Existing
HA diagnostics provide a compact connection report and link to app downloads.
Native configuration contains permissions, mappings, device ratings/overrides and
explicit source subscriptions; credentials, tariff settings and discovery metadata
remain in the app. Configuration acknowledgement precedes policy work. Retry
fingerprints exclude generated review timestamps.

The complete live accounting snapshot (355,923 meters, 149,884 observations and 800
admissions) passed indexed/reference parity. Warm restore took 0.36 seconds without
meter hydration. Reusing a consistent read transaction reduced full historical
planner feedback to 2.73 seconds on that snapshot; routine live feedback uses the
active objective catalogue. The offline upgrade backup remains separate.

Validation: 1,032 integration tests, 69 app tests, 93 editor tests and eight browser
checks passed. The removed per-controller HA subscription test is superseded by
persisted generic entity catalogue/offline/reconnect coverage. Installed beta.60
was verified against all 86 source hashes and all 29 HA entity registrations were
recorded before deployment. App beta.15 and companion beta.65 are prepared for the
coordinated upgrade; publication and live verification still follow.
