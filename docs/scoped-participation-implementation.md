# Participation and scoped supply: implementation status

16 September 2026. **The live battery command path is now connected in code;
deployment and hardware testing remain.** See [live operation, conversion losses
and rollout](battery-live-commissioning.md). The normative decisions remain in
[device participation and battery supply](device-participation-and-battery-supply.md).

## Implemented

- Inclusion belongs to HA; website roles determine Monitoring/Planned; Schedule
  offers Verification/Controlling for Planned equipment. Local inventory remains
  visible after exclusion so a device can be re-included.
- Complete outbound inventory omits Excluded devices. Local exclusion fences
  optimization authority immediately. Device telemetry, associated system SOC/
  temperature, and storage source bindings respect inclusion. Whole-house balance
  can still use local storage flows before removing their individual payload fields.
- Admission is bound to the acknowledged website role revision and physical owner.
  Demotion, exclusion and changed admission remove the old controlling permission.
  Configuration migration 14 clears existing grants; re-admitted Planned equipment
  starts in Verification. Existing issued-effect/release records are retained.
- Missing setup for Planned equipment is a planning blocker, rather than silently
  treating that equipment as Monitoring.
- Website grey consumption is gross base, before PV. Small/excess Planned bands
  are grouped as Other planned devices. The executable chart restores Verification
  demand from the captured external-demand projection without changing commands.
  Historical membership is explicitly labelled as reclassification using the plan.
  Invalid measurements propagate through stacking and SVG rendering as gaps.
- `battery_supply.py` validates the five scope forms, proportional solar sharing,
  membership revisions, freshness/alignment, unique measurement sources and gross
  reconciliation. Interval averages and reviewed watts cannot authorize instant
  subgroup supply. The user-named house/PV entities are discovery candidates;
  saved field roles define their measurement meaning; the Sigen DC conversion model
  and its remaining daylight approximation are documented in the live guide.
- Closed execution-policy v2 adds mandatory scope, proportional attribution and
  explicit wear basis. Both compiler and evaluator reject native responses that
  exceed scope, including house supply during forced export. They never silently
  clamp a scored native target or widen it to the battery rating.
- Offline household schema/scorer v2 supports both explicit AC-throughput wear and
  discharged-storage wear. The latter matches the existing planner's degradation
  basis. Current permissions remain strict; future continuation uses its own
  explicit per-quarter permissions. Current ineligibility cannot by itself erase
  an otherwise permitted future charge/export opportunity.
- Runtime authority now binds the actual supply selector, not only its revision
  label. Checkpoint v7 carries the updated policy/authority shape and rejects older
  offline checkpoints; the production owner uses a version-2 outer journal carrying recovery bindings.
- The async `HomeHost` implements reducer effects, durable-before-send, final
  authority checks, ambiguous transport outcomes and conservative restart. A pure
  Sigen adapter constructs bounded PV First register transitions. HA setup now
  connects both modules through the durable battery writer fence.

## Production conditional projection

`generateOptimisationPlanWithBatteryProjection` returns the existing plan and a
separate resolved battery projection from the same generation. The existing plan
wire also carries explicit `battery_supply_scope` (`whole_house` for current generated plans). Schema 9 uses the execution branch's
projection for Controlling; provenance is trimmed with that branch's remaining
horizon. Verification uses the conditional branch described below.

Each continuity candidate carries its own materialization. The selected candidate
supplies unrounded final household demand after thermal/device scheduling, exact
remaining-quarter durations and battery flows. Its selected dispatch bundle
supplies tariffs, limits, usable-energy state, terminal curve and discharged-storage
wear. There is no second workbench solve and no reconstruction from display values.

The projected household problem freezes the final non-battery schedule. Its total
plus `dispatch_total_offset_sek` reproduces the **conditional one-battery** dispatch
score; the offset is initial stored-energy utility. Initial import is null because
batch dispatch has no initial-to-first-slot ramp charge. This does not assert joint
optimality of the earlier auction and subsequent thermal scheduling.

Ready projections are detached and immutable. Unsupported models/constraints or
infeasible selected trajectories return explicit reasons and cannot authorize a
substitute command. Grid charging is feasible in the dispatch scoring domain;
ordinary charge-bidding heuristics are not treated as physical permission. This
record grants no device authority. The exchange below compiles it only after
validating a separately supplied native context.

Parity tests compare alternative battery schedules under two different fixed
household schedules, negative/positive prices, partial first quarters, losses,
wear, shaping, ramp and terminal utility. A production thermal example also
cross-scores using its actual resolved store. Tests cover selected continuity,
quarter-boundary provenance, mixed-mode external demand and unchanged plan output.

## Policy exchange and Verification delivery

Planning protocol 3 carries the resolved projection through every serialized
worker stage. Ingest stores it in `energy_optimisation_current.battery_projection`
with the plan in the same upsert; old rows remain null until a fresh generation.
The transport rejects missing or mismatched projections rather than reconstructing
one from display slots.

When the battery is in Verification, the physical execution plan still contains
no battery command. A separate `battery_verification` projection evaluates only
the battery while freezing all other consumption to the exact physical execution
schedule. Its gross load and Planned/base partition come from that execution
schedule, not the joint hypothetical plan. Controlling uses the selected execution
projection without another battery solve.

`energy-battery-policy` authenticates the device and subscription, loads only its
home's current acknowledged generation, and checks fixed-plan revisions. The
caller supplies a local native context (configuration/catalog revisions, declared
response evidence, supported operations, scope, bounded domain and explicit
per-quarter permissions). It cannot submit economics or a search budget. The
server restores all Planned demand components, including Verification devices,
shares solar proportionally, and intersects each quarter's native permissions with
the source problem. Current restrictions do not erase permitted future charging.
The source cut is never rebased: a later quarter requires a fresh plan.

Compilation is followed by another source/expiry check. Every response explicitly
has `purpose: verification` and `control_authority: false`. The HA exchange validates
the closed delivery envelope and Python policy, coalesces requests, persists status,
and rejects replies overtaken by reconfiguration, expiry or shutdown. Schedule and
HA diagnostics expose delivery blockers separately from existing command status.

## Production connection and remaining rollout

HA now builds the native context from configured Sigen controls, ratings, live
membership and a versioned directional conversion model. The delivery envelope
still grants no authority by itself: `HomeHost` validates it, observes live load,
installs the scoped policy and acquires the sole writer before sending commands.
Current and future scoring use the same DC storage / converted AC flow semantics.
The pure `pv-first-v1` AC fixtures remain a separately declared offline model.

The production `ScheduledController` delegates battery work to this owner. It
continues to coordinate other adapters through the shared lock and pending-demand
reservations. Policy withdrawal, restart and unload use the journalled approved
release; they do not reactivate the legacy forecast/rating battery algorithm.

The [live guide](battery-live-commissioning.md) records sensor signs, freshness,
register refresh, native ordering, source-cut counters, measured-loss evidence,
remaining solar conversion approximation and coordinated deployment steps.
Nonzero shaping thresholds, hard per-boundary targets, positive export reserves
and PV-curtailment remain outside the existing production projection's coverage.
A real installation run is still required to validate physical response and timing.
Nothing has been deployed or commanded on the user's installation by these tests.

## Validation

Regression coverage includes proportional PV (base 1 kW / house 3 kW / PV 1 kW
allows 2/3 kW), stale/missing/duplicate/average measurements, changed admission and
scope, exact native-response rejection, wear parity across current/continuation
scoring, current restrictions with permitted future recovery, durable writes,
transport ambiguity, mixed-mode chart partition/rendering, serialized projection
parity, authenticated home isolation, source replacement during compilation, and
configuration/shutdown races during delivery. The provider fixture is generated
with `deno run --allow-write scripts/generate-battery-policy-delivery-fixture.ts`;
its copy in HA `tests/fixtures/battery-policy-delivery.json` must stay byte-identical. Repository test,
frontend test, build, lint and mocked-browser results are reported with the commit.
