# Plan-based control implementation

Status: implementation plan, 10 September 2026. Implements the accepted
[equipment/control design](equipment-catalogue-design.md) and
[Sigenergy operating policy](battery-control.md).

## Outcome and scope

The household milestone is a home battery and pool heater following the agreed
plan, with observed execution and reliable edit/override/handover behavior.
Battery is commissioned first, then pool. This is Phil's delivery priority, not
an architectural requirement, prerequisite chain between device types, or default
ordering for other homes.

Deliver only the shared infrastructure needed for those controls and reuse it
for existing generic controls. Do not make a full manufacturer database, a new
catalogue administration product, a complete device-page redesign, or EV/room
commissioning prerequisites to this milestone. Preserve other homes' monitoring,
history and explicit planning choices through the schema migration. Standard HA
switches and numbers do not require downstream appliance make/model information.

Boundary agreed 10 September: the customer automation owns all pool hardware
behavior, including Nibe register writes, circulation, sequencing and triggers.
SHS sends versioned heat/defer/release requests through a customer-provided HA
interface and consumes feedback. Do not build a dependency engine or migrate
customer automation internals into SHS.

This plan does not itself enable live control. Software delivery and preparation
come first; commissioning has explicit bounded hardware actions to review.

## 1. Establish the two control definitions and migration rules

Delivered: [control contract v1](../contracts/control/v1/README.md), matching
schemas/fixtures in both repositories, [migration specification](../contracts/control/v1/migration.md)
and [pool evidence/setup gaps](../contracts/control/v1/pool-installation-evidence.md).
This step changes no runtime execution or live settings. Applied migration,
capability discovery and authority synchronization remain later steps.

Work across HA and website contracts before changing execution:

- Specify a stable home-scoped control ID, meter/source relationships, control
  contract/version, explicit objective, field ownership and source provenance.
- Separate HA actuator capabilities and local limits from website inclusion,
  objectives and energy-model preferences. Keep live state out of configuration.
- Define website desired revision, HA binding/capability revision, accepted
  revision combination, planning-model revision, permission and runtime status.
- Define change classes: presentation only, planning input, binding/semantics,
  restrictive limit, permission and external override. State which invalidate a
  plan, require handover or only update display.
- Specify opaque server IDs, local registry resolution and home-scoped API access.
  Entity renames preserve identity; uncertain replacements require mapping.
- Define battery dispatch intent + charge/discharge ceilings, keeping forecast
  watts separate. Define pool heat/defer instructions with an agreed objective
  and operating band. A defer instruction must allow a reported lower-bound
  limitation; it cannot promise zero heat when the local thermostat still calls.
- Explicitly represent pool, battery, room and EV planning associations; category
  labels cannot change execution semantics. Migrate unambiguous existing routes
  to these associations, and report unresolved cases precisely.
- Inspect the actual pool control path: distinguish Nibe pool-band control from
  existing electric-heater relay/meter groups and circulation. Establish which
  observations belong to this pool service and which customer request/feedback
  interface SHS will bind. Nibe and pump hardware remain customer-owned. Do not
  assume a meter named pool heater measures Nibe compressor consumption.

Deliverable: versioned schemas, representative battery/pool examples, invalid
examples, ownership/change matrix and migration specification in both repos.
Done when: both sides validate identical fixtures; no historical meter is
rekeyed or counted twice, and all required pool roles have an evidenced source
or a visible missing setup item. Bindings are installation data, not hardcoded
entity names.

## 2. Build shared HA capability discovery and local binding validation

Delivered: [local capability setup](control-capability-setup.md), shared primitive
checks, registry discovery, Sigenergy proposals, customer pool interface validation
and durable local binding reservations. New controls remain off and await the
revision/authority agreement in step 3.

- Extract/reuse existing switch, number, select and climate command validation.
  Represent supported operations, units, bounds, steps and explicit semantic roles.
- Resolve entities through HA registry identity and discover supported setup
  candidates. Normalize make/model/platform metadata for catalogue summaries.
- Add a declarative Sigenergy mapping preset backed by local code. Validate the
  customer pool request/feedback interface without binding its internal Nibe or
  pump entities. Brand/model metadata helps proposals; generic HA controls work
  without it.
- Persist binding edits before publishing an acknowledgement. Validate complete
  composed states and reserve actuator ownership, including group members.
- Add local operating limits, normal/handover settings and precise setup errors.
  Explicit user values survive subsequent discovery; renamed entities resolve
  without changing control identity.

Deliverable: reusable local control definition and discovery/validation layer.
Done when: a standard switch from an unfamiliar integration validates using the
same primitive, wrong numeric units are rejected, ambiguous ownership is refused,
and both target devices produce useful setup reports with control still off.

## 3. Make settings synchronization and plan authority revision-aware

- Add server persistence/API fields for desired, acknowledged and active state.
  Apply related field edits atomically; use expected revision checks to prevent
  stale browser tabs overwriting newer changes.
- Implement idempotent local acknowledgement and ordered revision handling.
  Test interruption before/after local save and server acknowledgement.
- Implement lightweight settings synchronization independent of statistics
  gathering. For the initial implementation use a dedicated authenticated
  revision/authority request every 15 seconds; no push transport is required for
  this release. Repeated network failures must not create extra revisions.
- Use a 120-second renewable execution authority lease, also bounded by plan
  expiry. The server must not renew authority for superseded binding revisions
  or removed controls. A stale plan cannot extend the lease.
- Show remote changes as pending until acknowledged; never claim immediate
  remote stop during disconnection. Local permission-off remains immediate.
- Persist desired/accepted revisions, but do not resurrect an expired lease on
  restart. Verify UTC/time validity; local runtime guards enforce expiry.
- Plan generation captures a consistent accepted snapshot and verifies it again
  before publication. Every command identifies its accepted control revision.
- Classify changes by dependencies. Replace coupled household plans coherently;
  do not mix revisions in a way that breaks shared grid headroom.

The 15-second/120-second values are proposed software defaults, to be covered by
fake-clock tests and verified during commissioning. The 120 seconds bounds loss
of software authorization while HA is running; it is not an inverter watchdog
claim or a promise of completed physical handover within that time.

Deliverable: end-to-end settings agreement and bounded execution authorization.
Done when: delayed acknowledgements, offline edits, stale plans and simultaneous
edits cannot apply a command to a different definition; status accurately shows
pending, active and expired states.

## 4. Connect the website and planner to the agreed definitions

- Keep the current device table and setup pages. Constrain available methods to
  supported contracts; allow an explicit pending method request before binding.
- Show separate setup, synchronization, plan and operation status. "Ready" must
  name the revision being acknowledged and not merely match a method string.
- Keep category informational. Display explicit objectives in device details.
  Keep load characteristic independent from actuation method.
- Distinguish running-power estimates, their source/override, and live watts.
  Do not treat low instantaneous power as a permanent equipment rating.
- Advertise supported battery intents and pool instructions from the local
  accepted contract. The planner may only authorize supported operations.
- Generate battery intent/ceilings explicitly from the optimization decision and
  user source/export policy. Do not reconstruct policy from signed watts later.
- Reconcile forecast and execution semantics: ceilings do not guarantee battery
  delivery; pool heat permission does not guarantee compressor operation.
  Account for limited delivery using observations and replanning.
- Verify pool energy accounting and electrical conversion against the mapped
  service, including shared compressor/auxiliary consumption where applicable.

Deliverable: both target controls are configurable and generate matching plans
with their operation permissions off. The minimal catalogue shows generic
contracts, observed make/model/interface and outstanding composed-control work.
Done when: target/deadline/load edits replan without remapping; method/binding
edits cannot leave a misleading ready badge or execute an older contract.

## 5. Implement the Sigenergy battery adapter

Replace the existing signed-target executor and configuration with the accepted
intent-and-limits design. No compatibility path through the inverter adjustment.

- Share one intent/mode policy and one desired-state executor.
- Validate both non-negative ceilings, ratings, SOC boundaries, reserve/export
  rules and available household/grid headroom before writing.
- Claim and verify remote authority; apply restrictive ceilings before mode
  transitions and relax only after acknowledgement, per the accepted design.
- Track setting acknowledgement separately from measured battery/PV/load/grid
  behavior. Report constrained output without demanding delivery at the ceiling.
- Restore Maximum Self Consumption and valid reviewed normal ESS limits.
  Reject the unset sentinel rather than saving it as a restorable request.
- Extend persisted ownership, restart recovery and manual-change detection to
  this composed control. Preserve plant import/export/PV limits.
- Remove obsolete signed-target fields through the one-way migration. Handle
  any existing ownership journal through a defined release/update procedure;
  do not silently reinterpret old journal values or discard pending restoration.

Deliverable: executable battery contract with simulated HA boundary coverage.
Done when: all allowed transitions, partial failures, reserve boundaries,
expiry/disable/restart restoration pass; no path writes negative ESS limits,
ESS First or the inverter adjustment. Export remains subject to the household's
explicit permission; implementing an intent does not enable it.

## 6. Connect the customer-operated pool service

- Bind the explicit pool control ID and accepted revision to a customer-provided
  HA request/feedback interface. Send complete heat/defer/release requests with
  the agreed objective and authorization expiry; do not derive them from watts.
- Validate advertised operating bounds, steps, temperature freshness and the
  complete request before dispatch. A website target edit must reach the
  customer's interface as a new accepted objective, not a captured old band.
- The customer's automation owns all Nibe writes, circulation/pump sequencing,
  triggers, thermostat behavior and restoration. Do not reuse the direct Nibe
  band executor for this contract or implement a general dependency mechanism.
- Retire SHS's old direct pool/group/companion mappings through reviewed handover.
  Keep the customer's hardware automation as the owner; SHS does not reserve or
  discover its internal actuators. Bind only the request and feedback interface.
- Correlate request acknowledgement independently from measured heating. Report
  scheduled, observed heating, limited deferral, unavailable heat and unknown
  observations using only the feedback actually supplied.
- Apply shared revision, permission, expiry and override rules to SHS requests.
  Release relinquishes SHS scheduling to the customer's normal policy; it is
  not an instruction to stop the pump. Verify the customer interface's expiry
  and release behavior before granting execution authority.

Deliverable: explicit pool planning requests, bounded customer interface and
honest correlated feedback; no home-specific sequencing inside SHS.
Done when: heat/defer/target-change, stale/duplicate requests, expiry/release,
missing/delayed feedback and lower-bound limitation tests pass; meter accounting
is verified once and old direct SHS actuator ownership has been resolved.

## 7. Deploy together and verify without hardware writes

- Run relevant Python, frontend, API/database and planner tests, including shared
  contract fixtures and migration tests. Run the release-required checks.
- Deploy server/database validation and website changes in the negotiated order,
  then the HA beta and restart. Reject incompatible executable schemas clearly;
  do not provide a legacy execution fallback. Keep both controls off throughout.
- Follow RELEASING.md. Each integration-code change includes
  `bash scripts/bump.sh beta` in its code commit; no manual release tags.
- Explicitly choose the backend/environment for commissioning. This installation
  was connected to the test backend at the readiness survey; do not silently
  switch it or assume website and HA point at the same environment.
- Preview actual plan instructions against live entity capabilities using the
  same command-building/validation logic with writes disabled. Ensure this
  preview does not acquire ownership or create successful-execution claims.
- Exercise ordinary settings edits, pending/active status, conflicts and renamed
  test entities. Confirm prices, actuals and unrelated monitoring still work.

Deliverable: deployed, coherent definitions and plans for the two devices.
Done when: the preview shows intended commands and all enablement prerequisites
pass or are presented as concrete commissioning actions. A preview alone does
not count as physical acceptance.

## 8. Commission and enable battery, then pool

Prepare the exact entities, intended writes, limits, expected observations and
handover for each session. Review this concrete live-test sequence with Phil
before the first hardware test; preparatory reads, code and simulations proceed
without waiting for that final commissioning decision.

Battery session:

- Confirm normal ESS limits, resolve the sentinel and record independent plant
  constraints without changing them. Identify and suspend competing command
  owners as part of the reviewed cutover.
- Test handover and bounded supported intents, measured flow, SOC protections,
  failure behavior and disable/expiry. Test remote-authority loss and establish
  independent hardware behavior on HA/network failure where safely practicable.
- Record which intents/conditions passed; keep unverified intents unavailable
  to executable plans. Do not infer unobserved surplus-PV/Standby behavior.
- Enable ordinary scheduled battery operation for the verified scope and inspect
  actual-versus-planned behavior through a meaningful transition and handover.

Pool session:

- Confirm the bounds/objective and customer request/feedback interface. Keep
  Node-RED/HA automation as the Nibe/pump owner; release old direct SHS mappings
  and cut over only the scheduling interface.
- Test a bounded heat request, deferral and its possible lower-bound limitation,
  correlated feedback, manual override, expiry and release to customer control.
- Enable scheduled pool operation and observe a meaningful heat/defer cycle.

Combined acceptance:

- Observe both following one valid household plan without conflicting ownership
  or duplicated power accounting. Check actual grid/battery/pool outcomes.
- Exercise a normal website preference edit and a local stop. The former reaches
  an acknowledged replacement plan; the latter relinquishes control correctly.
- Record installed versions, control revisions, tested scope and unresolved
  hardware observations. Only validated scope is enabled.

The milestone is complete when both controls actually follow plans within their
accepted scope, feedback is honest, and edits/overrides/handover behave as tested.
Passing unit tests or seeing a green configuration badge alone is not completion.
