# Participation and scoped supply: implementation status

15 September 2026. **Partial implementation; the live battery replacement is not
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

## Work still required before the battery replacement is complete

1. Build the production projection from the *selected* resolved dispatch bundle
   into the conditional one-battery problem. Preserve exact tariffs, curve/state
   basis, limits and non-battery trajectories. Do not reconstruct them from rounded
   display slots or run a second potentially different dispatch solve.
2. Cross-score that projection against live dispatch. Wear representation is now
   available; initial ramp and terminal normalization still need explicit mapping.
   Nonzero shaping thresholds, hard per-boundary targets and curtailment are not
   currently represented by the execution compiler and must remain unsupported
   until modeled consistently.
3. Connect policy exchange/renewal, same-capture physical evidence, durable grant
   arbitration and commissioned adapter IO to HA setup. Fence/release the old
   battery writer before any new runtime grant; requested Controlling alone is not
   effective authority. Make uncommissioned/uncovered status visible on Schedule.
4. Establish physical native-mode response, power units/quantization, transition
   ordering, write/response latency, stale-input behavior, shared AC meter boundary
   and subgroup enforcement precision. A successful 406 W cap observation is not
   evidence for every mode or transition.
5. Replay the captured incident through that connected path, including a contrary
   case where conserving energy is cheaper, then coordinate both-repo rollout.

The current `ScheduledController.execute_battery` remains the live writer in this
checkout. The rated/forecast command path has **not** been replaced by these offline
policy changes. Unit tests are not evidence that the house now runs scoped C+V
control. No HA settings, native mode, service command or deployment was changed.

## Validation

Regression coverage includes proportional PV (base 1 kW / house 3 kW / PV 1 kW
allows 2/3 kW), stale/missing/duplicate/average measurements, changed admission and
scope, exact native-response rejection, wear parity across current/continuation
scoring, current restrictions with permitted future recovery, durable writes,
transport ambiguity, and mixed-mode chart partition/rendering. Repository test,
frontend test, build, lint and mocked-browser results are reported with the commit.
