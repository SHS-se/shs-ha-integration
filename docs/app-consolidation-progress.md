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
- [ ] Extract the companion's native execution and wire dependency boundary.
- [ ] Move canonical configuration and credentials into app storage.
- [ ] Route app edits and existing HA mode entities through acknowledged revisioned changes.
- [ ] Move configuration editors and diagnostic downloads into the app.
- [ ] Atomically checkpoint the source mirror and retire processed transport receipts.
- [ ] Replace lifetime accounting hydration with indexed operational evidence.
- [ ] Apply three-day detailed diagnostic retention and expose all active database metrics.
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
