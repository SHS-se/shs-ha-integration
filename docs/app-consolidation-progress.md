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
- [x] Publish, upgrade and verify live controls, entity identities and app-only restart.

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

## Live rollout — 28 September 2026

Published app `0.1.0-beta.18` and companion `0.9.0-beta.66`. The first live
startup found that the gateway journal's durable-record allowlist omitted the new
entity catalogue. Added the record and a real SQLite journal restart regression;
all 1,033 integration tests pass. App CI passed all 70 app tests, 93 editor tests
and eight browser checks before multiarchitecture publication.

The live indexed/reference accounting check passed with 356,654 meter facts,
150,083 observations and 800 admissions. The original migration identity,
activation and fenced source journal remain unchanged. Configuration adoption
removed the cloud token and editable options from HA; the app's canonical record
has mode 0600 and HA acknowledged its native subset at revision 1. The final app
image update left Core's start timestamp unchanged and skipped the completed
accounting conversion. Live receipt recovery and final functional checks follow.

Live load testing also found that diagnostic export still requested the lifetime
accounting journal synchronously. App diagnostics now use current accounting and
at most three days of execution traces, leaving operational evidence in SQLite.
An app-only regression verifies that export never calls lifetime feedback. Empty
counter sub-events no longer repeat an identical durable state write; the complete
receipt still commits its cursor and source mirror, and actual state changes still
persist immediately. Crash-replay coverage proves the preceding durable cursor
survives an interrupted empty sub-event. The app suite now has 72 passing tests.

The live HA log review caught five native diagnostic sensors rejected because
JSON category strings were not converted to `EntityCategory`. The adapter now
converts category, device class and state class to HA enums and applies descriptor
updates through the same method. The repair was instantiated against the actual
installed HA classes in an isolated process, and 1,034 integration tests pass.
Companion `0.9.0-beta.67` loads all 29 real entities with no restored placeholders.
Final app `0.1.0-beta.21` passed 72 app tests, 93 editor tests and eight browser
checks and is deployed. It preserves Core's final start timestamp while catching
up approximately 16 receipts/second on this machine, without storage failures.

Live profiling identified repeated accounting interval reads as the remaining CPU
bottleneck. SQLite chose the primary-key suffix scan instead of the day-bucket
index. App `0.1.0-beta.22` explicitly selects that index, aggregates boundary edges
inside SQLite and shares one read transaction for live objective outcomes. The
production snapshot's live feedback remains byte-identical (SHA-256
`9afb0eba76f7c72b3e0ef0b649fbf3dd8128a3a453d7c597f17b3f7da8bb8925`),
with measured query time reduced from 2.34 seconds to 0.57 seconds. All 73 app
regressions pass, including query-plan and transaction-reuse coverage.

Recovery now has a dedicated dashboard state with the completed observation
number. An existing runtime no longer offers first-install instructions while
replaying its queued data. Desktop and mobile tests cover recovery progress and
the transition to the live dashboard; all ten browser checks pass. This UI repair
is published as app `0.1.0-beta.23` without changing the companion.

The active-loop profile exposed two further costs: the one-second display cache
expired before a slow calculation completed, and the boundary index required a
second table lookup for every edge. App `0.1.0-beta.24` starts display-cache reuse
when calculation finishes (retaining its true sample timestamp), shares identical
boundary-day sums within one SQLite read snapshot, and replaces the old index
with a covering index. New accounts invalidate the display cache immediately;
read transactions never share cached sums across later corrections. The index
upgrade retains the verified schema, head revision and all operational facts.
Production-snapshot feedback remains byte-identical and takes about 0.10–0.16
seconds instead of 2.34 seconds. All 76 app tests pass.

The live settings-save check found a configuration-installation race: HA correctly
revokes admission while recording the native revision, but an overlapping request
poll caused the app session to restart. Gateway calls now coordinate around that
installation: existing admitted calls finish first, new app operations wait for
its acknowledgement, and receipt delivery/Recorder reads remain multiplexed.
Cancellation releases the admission wait. All 78 app tests pass. The first live
save was durably recovered and acknowledged at revision 2 without losing settings.

The HA correction URL reached the app correctly but a redraw overwrote the
requested card expansion with the prior collapsed DOM state. Expansion intent now
applies after preserving existing card states. Browser coverage verifies that a
direct field URL expands the owning section and focuses the actual editor. All
12 desktop/mobile browser checks and 93 editor checks pass. App beta.25 reached
the registry before workflow cancellation, so changed UI bytes use beta.26.

A live diagnostic download completed in 4.07 seconds (6.5 MB gzip), with 167 recent
execution traces, a three-day retention declaration and no lifetime accounting
journal. The settings endpoint returned the 48 configured devices and applied
revision. Receipt processing continued during export.

The final active-loop profile found checkpoint validation repeatedly decoding the
same latest admission through the indexed sequence. Each immutable evidence view
now retains only its latest decoded row; appended views and prior ordinal prefixes
remain independent. Coverage proves repeated contract access decodes once and
appending cannot change an old view. All 79 app tests pass. The beta.26 build was
cancelled before publication (registry manifest 404) and rebuilt with this repair.

Live beta.26 checks passed both configuration paths: an unchanged app save applied
revision 3 in 7.2 seconds, and the HA hot-water mode selector applied revision 4
without changing its verification mode. No gateway reconnection occurred. The HA
correction URL expanded the right section and focused its actual power editor.
However, sustained receipt delay still caused intermittent freshness faults.

App beta.27 changes only the derived meter query layout from daily to hourly
blocks, atomically rebuilding aggregates without altering original facts or the
execution checkpoint. Boundary scans stop at the interval's next edge, and current
admission lookups reuse the immutable latest row. Production-snapshot feedback
remains byte-identical, now taking 18 ms (previously 125 ms). All 81 app tests pass,
including daily-layout upgrade, out-of-order times, late corrections and old views.

## Final live acceptance — 28 September 2026

Completed with app `0.1.0-beta.27`, companion `0.9.0-beta.67`, release commit
`6145e0e` and successful app workflow `36366486694`. CI passed 81 app tests,
1,034 integration tests, 93 editor tests and 12 desktop/mobile browser checks.

- The app owns canonical settings and credentials, with desired/applied revision 4.
  HA's native replica has no cloud token; the app configuration file is mode 0600.
- All 29 original HA entity IDs, unique IDs and enabled states match the baseline.
  All are genuinely loaded, with no restored placeholders or unavailable states.
  Battery remains Controlling; EV, pool and hot water remain in Verification.
- App settings save and the existing HA mode selector both passed live writes
  without changing configuration values or restarting Core. The correction URL
  expands the relevant section and focuses the actual measurement editor.
- The existing SHS logo, navigation, schedule chart and settings rendered correctly
  through HA ingress. The final browser shows Connected and a validated plan.
- A requested planner exchange completed in 30.1 seconds, accepting plan
  `cb6e8c3c-4e23-4f2f-a64d-2afa6937572a`, issued at 01:39:41 UTC. Receipt
  processing and control continued during the exchange.
- Detailed controller diagnostics downloaded successfully (6.5 MB gzip, 4.07 s),
  containing recent execution traces and a three-day retention declaration, with
  no lifetime accounting journal. Operational correction facts remain indexed.
- An app-only restart recovered control within the first 15-second observation
  interval after the initial sample. Subsequent samples showed Controlling,
  successful native service returns and source ages around 2–7 seconds. Receipt
  processing and retirement advanced monotonically; transient batches drained.
- Core's start time remained `2026-09-28T00:00:17.04634887Z`. The activation
  `bdf1a357ada4457cb848df654874d04b` is unchanged, and the original source
  authority remains fenced with its clean-stop marker. Migration was not rerun.
- The final resource spot-check showed 291.6 MiB memory and 0.33% CPU; this is a
  momentary sample, not a long-term load measurement. No new app errors appeared
  during the restart and acceptance checks.

Routine app releases now restart only the app. Companion updates are reserved for
HA adaptation, execution-boundary or wire-contract changes that need native code.
