# Participation and scoped supply: implementation status

16 September 2026. **Partial implementation; the live battery replacement is not
connected or commissioned. Do not deploy this as the fix for the September 15
battery discharge incident.** The normative decisions remain in
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
  their common AC measurement boundary still needs verification.
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
  label. Checkpoint v6 carries the updated policy/authority shape and rejects older
  offline checkpoints; no production host was using that checkpoint format.
- The async `HomeHost` implements reducer effects, durable-before-send, final
  authority checks, ambiguous transport outcomes and conservative restart. A pure
  native adapter only composes supplied commissioned transitions. Neither module
  is attached to HA setup or granted physical authority yet.

## Production conditional projection

`generateOptimisationPlanWithBatteryProjection` returns the existing plan and a
separate resolved battery projection from the same generation. The existing plan
API and HA plan wire output are unchanged. Schema 9 uses the execution branch's
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

HA does **not yet produce a commissioned native context**. Its explicit null
context yields `native_context_required`; it never invents operations, evidence or
measurement boundaries. Even a valid received policy is `delivered_not_admitted`.
The exchange does not send a runtime policy event or grant a writer. The real
compiler/provider fixture consumed by Python is marked synthetic test data and
is not physical commissioning evidence.

Rollout dependency: apply the new nullable-column migration and deploy the protocol
3 planning worker and ingest together, followed by the exchange endpoint and HA.
No compatibility fallback is provided. This work has not been deployed.

## Work still required before the battery replacement is complete

1. Produce the native context from verified local configuration and commissioning
   evidence, with its approved supply scope and per-quarter native permissions.
   Establish physical native-mode response, units/quantization, transition ordering,
   write/response latency, stale-input behavior, a shared AC meter boundary and
   subgroup enforcement precision. The supplied house/PV entity names and a prior
   406 W cap observation are not evidence for every mode or transition.
2. Connect same-capture physical measurements, energy accounting, durable grant
   arbitration and commissioned adapter IO to `HomeHost` in HA setup. Fence/release
   the old battery writer before any new runtime grant; requested Controlling or
   successful policy delivery alone is not effective authority.
3. Extend represented constraints if needed. Nonzero shaping thresholds, hard
   per-boundary targets, positive reserves for enabled battery export, fixed-plan
   authority and curtailment remain explicitly unsupported by this projection.
4. Replay the captured incident through the connected path, including a contrary
   case where conserving energy is cheaper, then coordinate both-repo rollout.

The current `ScheduledController.execute_battery` remains the live writer in this
checkout. The rated/forecast command path has **not** been replaced by these offline
policy and delivery changes. Unit tests are not evidence that the house now runs scoped C+V
control. No HA settings, native mode, service command or deployment was changed.

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
