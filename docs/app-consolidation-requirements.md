# App consolidation requirements

User-approved direction and follow-up notes, 27 September 2026. These requirements
supersede the initial HA-authoritative configuration choice in
[app-container-design.md](app-container-design.md). The runtime cutover is complete;
see [runtime-move-design.md](runtime-move-design.md) for deployed evidence. Implementation and live acceptance are recorded in
[app consolidation progress](app-consolidation-progress.md).

## Updates and ownership

Routine SHS configuration, UI, controller, planner-client and diagnostic changes
must not require a Home Assistant Core restart. The companion should change only
for HA adaptation, execution-boundary or protocol changes that actually require
new companion code. Normal config-entry reload is a lifecycle operation; it does
not replace already imported Python modules. Do not introduce Python hot patching.

Separate app releases and storage migrations from companion releases. Replace the
current whole-core digest/exact companion coupling with an explicit, tested wire
protocol and capability contract. Required upgrades must remain explicit and fail
clearly when incompatible; this is not authorization for implicit compatibility
fallbacks. The implementation design must identify the minimal code actually shared
with the HA execution gateway. Most future app updates should restart only the app.

The app owns SHS configuration, entity mappings, validation, discovery suggestions,
readiness, controller policy, cloud exchange, history, charts and diagnostic exports.
HA remains the authority for its actual devices, entity metadata and native services.
The companion receives a revisioned execution/subscription configuration from the
app, retaining only the state needed for its approved command boundary and existing
finite restoration obligations. It is not a second editable settings owner.

## Home Assistant remains a first-class control surface

Preserve the existing control/verification mode entities, price entities and useful
diagnostic/status entities, including their unique IDs and automation behavior.
Mode changes from HA must be validated and committed by the app and reflected in
both interfaces. An unavailable app cannot be represented as a successful setting
change. Concurrent app/HA edits must not overwrite one another silently.

Move detailed diagnostic downloads, profiling UI and configuration editors into
the app. HA exposes compact values/status and direct links to the appropriate app
view. Do not place full timelines or diagnostic documents in entity attributes.
Preserve the configuration correction contract: actual fields, field-specific
errors, required-field visibility and direct correction links.

## Historical data has distinct purposes

The inherited execution store contains append-only meter, observation, admission
and reconciliation evidence. It reconstructs an accounting account used for actual
delivery, open objectives, planner acknowledgements and late corrections. These
records are not merely diagnostic logs. Its current full-history residency and the
unpruned gateway/app receipt streams are implementation debt, not product retention
requirements. Verification already has count limits; limits by count do not provide
a clear user-facing retention duration.

Keep compact durable operational state: current configuration/plan, meter anchors,
accounting totals and acknowledgements, open obligations, unresolved command outcomes,
captured settings, required deduplication state and learned model parameters.
Keep only recent detailed diagnostic evidence, measured in days rather than weeks
or months. A proposed starting default is three days; the exact window and byte
budget remain part of the storage design, not a newly imposed control-validity rule.

Longer consumption history belongs in HA Recorder/statistics and the cloud planner.
Read necessary planning/backfill/calibration windows on demand and avoid building
another permanent raw-data archive in the app. For example, the current battery
loss-model input reads two days of five-minute statistics through HA; it need not
retain all raw historical controller events to retain the learned parameters.

Compaction must first create and validate a sufficient checkpoint. Retire processed
transport receipts only once their effects and the source mirror can be restored
without them. Preserve uncertain effects, open objectives, meter reset boundaries,
planner acknowledgement continuity and receipt-order handling of late corrections.
Storage age must not become a source-timestamp rejection rule. A short diagnostics
window does not authorize discarding unresolved operational obligations.

Startup must restore compact state plus the unprocessed suffix, independently of
the length of diagnostic history. Retention, restart and upgrade tests must prove
equivalent accounting, no duplicate device writes and unchanged entity identities.
Expose all active app databases in diagnostics, including sizes, row counts,
operation timings, retained range and receipt lag. Treat the one-off migration
archive separately from the active working set.

## Implementation sequence

1. Design the stable companion contract and explicit app/schema upgrade path so
   subsequent app work can ship without unnecessary Core restarts.
2. Move canonical configuration and its editors to the app while preserving HA
   entity controls; move detailed diagnostic downloads and remove obsolete HA UI.
3. Implement compact operational checkpoints and short diagnostic retention, then
   extend database and recovery-progress displays to the actual active stores.

Use the architecture review workflow before changing these ownership/storage
contracts, and the UX workflow for the app configuration and diagnostic surfaces.
