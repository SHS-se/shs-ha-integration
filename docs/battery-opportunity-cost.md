# Battery charge timing from actual state

Architecture adjustment, 15 September 2026. This is a documented requirement and
validation plan, not a newly implemented controller feature. It extends the
[battery execution design](battery-execution-design.md) and the deployed
[mixed-mode correction](mixed-mode-execution.md).

## Usage first

Keep the existing boundaries. These are proposed production call sites for
existing software interfaces; the host/adapter cutover is still outstanding.

```typescript
// Backend: compile future consequences for the executable household projection.
const result = compileBatteryExecutionPolicy({
  ...validatedExecutionRequest,
  operations: supportedOperations, // include useful partial charge ceilings
  search: boundedSearch,
});
// Publish a compiled policy and its declared coverage, or an explicit rejection.
```

```python
# HA host: supply fresh observations to the existing single policy owner.
home, effects = reduce_home(home, ConditionsObserved(conditions), now_ms)
# The reducer invokes evaluate_policy; native writes remain grant/catalog fenced.
```

```typescript
// Offline acceptance: compare each first action with its own searched future.
const comparison = compileBatteryPolicy({
  problem: problemFromTheSameActualState,
  reference_id: "wait",
  alternatives: [waitRemainingSegment, ...chargeAmountAlternatives],
  search: equalEffortSearch,
});
// Independently rescore complete candidates and compare the published ranking.
```

The illustrative input builders above are pseudocode, not additional public APIs.
There is no `catchUpToPlannedSoc()` operation.

## Problem and current behavior

The question is whether an extra increment of battery energy is more valuable
when bought now or later, given the actual charge, remaining demand, PV, prices
and physical opportunities. The previous forecast SOC is not a required state.
Being below that line can justify re-evaluation; it does not create an energy debt.

The deployed schema-9 planner already considers future tariffs, losses and battery
value. Its live `ScheduledController` follows each slot's selected command. The
newer finite continuation compiler, Python evaluator and home runtime are
implemented software, but are not yet the production battery writer. Updating
these docs does not change that deployment boundary.

The September 15 export demonstrates why these issues must remain separate:

- The mixed-mode execution plan includes the running pool and pump; its current
  load is about 2.72 kW versus 0.67 kW in the hypothetical preview.
- It was captured at 14:53 Stockholm with 37.6% SOC; the later reading was 38.2%.
  That instant does not demonstrate a charge shortfall. The completed 14:30
  history quarter reported about 30.2%, which is a different observation basis.
- Under the supplied future forecast, it holds now and first charges at 21:30.
  Keeping pool/pump demand at their measured running power through 21:30 changes
  the same planner's immediate request to about 3.29 kW charging. This is a
  sensitivity experiment, not an asserted future run duration or probability.

## Required economic comparison

At actual time `t`, let `b` be the next canonical quarter boundary. Simulate each
feasible current operation `a` over only `[t,b)` to obtain its endpoint state `s_a`:

```text
J_a = C_a[t,b) + V_b(s_a)
advantage_of_charging_a = J_wait - J_a
F_a = V_b(s_a) - V_b(s_wait)
J_a - J_wait = C_a - C_wait + F_a
```

`C` is the remaining current cost. `V` is the conditional future cost, including
terminal value. `J` uses the common household objective. Compare supported charge
amounts, waiting and other legal operations, not just zero versus maximum power.
The chosen operation must pass existing physical checks and economic stability
rules. A positive mathematical advantage alone does not bypass a declared
numerical/uncertainty deadband.

Both alternatives can change their future actions. Buying more now may mean
buying less later, moving a later purchase, supplying a dearer quarter, or losing
room for cheap future PV. Waiting may remain best when existing energy covers
expensive demand or a cheaper, physically reachable refill remains available.
Do not freeze the old schedule and score the new first action against an
unrepaired tail. Do not force either branch back onto the old SOC trajectory.

Count import, permitted export revenue, conversion losses, gross throughput/wear,
applicable grid/shaping/ramp costs and terminal value once. The broader household
model also retains service consequences; the current battery-only compiler does
not support service objectives or coupled thermal control. Charging can be limited
by remaining time, available grid power, capacity and native routing. Foregone
solar export/capture has value when physically possible; solar is not universally
free. Do not add a second opportunity-cost or headroom bonus to costs already in J.

For illustration, at 1.46 SEK/kWh and 95% efficiency in each direction, buying
energy to deliver one additional AC kWh later costs about 1.62 SEK before wear
and other costs. Avoiding a 2.83 SEK purchase could therefore be worthwhile.
That comparison does not establish how many additional kWh are needed. Neither
16:15, a price quantile nor an end-of-solar SOC becomes a new trigger or target.

## State, time and coverage invariants

1. Start every comparison from the same fresh measured energy, actuals watermark,
   execution scope, forecast/tariff/intent revisions and unresolved physical
   effects. Compare usable and total energy only with an explicit cutoff conversion.
2. Align tracking evidence to the same instant. Slot SOC is an endpoint forecast;
   a historical quarter aggregate is not a live endpoint. If an intermediate
   reference state cannot be derived from a defined response model, report that
   alignment as unavailable rather than inventing a shortfall.
3. Completed purchases/delivery are sunk history. Charge only over the remaining
   interval; never assign a full quarter's opportunity to the last minute.
4. HA evaluates the bounded policy inside its validity and domain. Stale,
   mismatched or uncovered state requests renewal and follows the existing
   ownership/release protocol. It cannot authorise a heuristic top-up or extrapolate
   an unsupported future value. Mode changes still invalidate execution scope.
5. Future economics stays server-owned. There is one HA request writer, no parallel
   local optimiser and no overlay that forces recovery toward a planned SOC.

## Ownership, data shape and approximation

| Module | Knowledge it owns |
|---|---|
| HA snapshot builder / `operating-scope.ts` | Participation and external demand counted exactly once |
| `energy-optimisation.ts` | Currently deployed forecast-based replanning and battery valuation |
| `battery-policy.ts` / `household-score.ts` | Bounded counterfactual future search and authoritative cost/physics scoring |
| `battery-execution-policy.ts` | Published operation family, continuation witnesses, domains and compile limits |
| `battery_execution_policy.py` | Remaining-time native response and bounded C/F/J ranking from live state |
| `home_runtime.py` and future host/adapter | Single request owner, grants, freshness, pending effects and native writes |
| Offline replay/report tooling | Derived comparison and sensitivity evidence; no control authority |

The current continuation is `V(energy_kwh, previous_import_w)`, retaining ramp
coupling. It selects among analytic bridge cells and searched fixed-suffix
witnesses, including a feasible idle family. Different operations can select
different witnesses. This is exact scoring **within the published family**, not
full future reoptimisation for every possible endpoint or a global optimum.
The offline acceptance comparison allows each first action its own newly searched
future; that search also declares its limits and is not a global-optimum oracle.

Reuse existing `ExecutionConditions`, `Decision`, `RankedOperation`, `Objective`
and `OutsideCoverage` types. Add a derived offline evidence record, not another
control contract:

```typescript
// Proposed report shape; not implemented. Existing Objective/SearchEvidence reused.
type ChargeTimingEvidence = {
  basis: { at_ms: number; actuals_watermark: string; scope_revision: string;
    forecast_revision: string; tariff_revision: string; intent_revision: string };
  kind: "same_forecast" | "sensitivity";
  candidates: Array<
    { operation_id: string; status: "scored"; charge_input_kwh: number;
      endpoint_energy_kwh: number; current: Objective; future: Objective;
      full: Objective; suffix_witness: string; search: SearchEvidence }
    | { operation_id: string; status: "not_compared";
        reason: "unsupported" | "uncovered" | "search_limited" | "current_infeasible" }
  >;
  observed_ranking_regret_sek: number | null;
};
```

A report must explain the incremental current cost and future saving for the
selected operation versus waiting, with next refill assumptions, exclusions and
search/coverage limits. Include the projected minimum stored energy above cutoff
before the next refill, its timestamp and witness. The supplied expected plan had
about 0.33 kWh above cutoff just before 21:30; this is a diagnostic, not a reserve
requirement. Where both choices can be scored under a stated sensitivity, report
their cost difference under that same sensitivity. Do not invent a numeric regret
when the candidate or its future is uncovered. Missing search results do not prove
physical infeasibility.
Report regret measured on a test case separately from any certified regional bound.

## Forecast uncertainty remains explicit

Price-aware comparison cannot supply missing information about external demand.
Retain a justified central forecast and expose sensitivity to plausible alternative
load/PV paths. An unweighted stress case is not an expected forecast and does not
automatically become the command. Historical device run-duration learning is not
a prerequisite for this architecture adjustment.

Under expected-cost optimisation, sufficient inventory is where the next unit's
conditional future benefit no longer exceeds its complete acquisition cost,
subject to real protections and service requirements. Insurance against forecast
error needs an explicit risk objective and evidence. Do not invent probabilities,
add an arbitrary reserve, or infer a mandatory worst-case policy from a price graph.
Weighted scenarios must document their basis, correlations and what becomes known
when; actions must agree until observations distinguish scenarios.

## Synthesis decision and alternatives

Independent Codex and Claude Opus (High effort) candidates were compared through
the architect workflow. Codex's existing-policy plus offline counterfactual audit
is the base: it preserves current ownership and states finite-family limits
accurately. Adopt Claude's compatible projected-margin, next-refill and
scenario cost-exposure diagnostics.

| Alternative | Decision and tradeoff |
|---|---|
| Live bounded continuation policy plus offline ranking audit | Selected: uses existing interfaces and supports within-quarter actual-state decisions; compiler coverage and host cutover remain substantial work |
| Complete server re-solve for each observation/replan | Useful comparison/current baseline; cloud latency and repeated solve cost limit response, and unchanged optimistic inputs can still produce the same answer |
| New scenario-evidence facade around multiple compiles | Defer a new public API until the audit proves the needed evidence; reuse scorer/compiler boundaries first and measure aggregate work |
| Full multi-stage uncertain-future optimisation | Possible later extension; needs supported correlated scenarios, information timing, calibrated weights or an explicit risk objective |
| SOC catch-up or cheap-price override | Reject: creates a competing policy, treats forecasts as obligations and can waste PV headroom or buy unnecessary energy |

We accept a finite family and explicit coverage gaps in exchange for bounded local
work. We accept diagnostic sensitivity before any risk-policy change in exchange
for avoiding unsupported weights. We accept documentation-only delivery now in
exchange for proving the economic mechanism before altering live operation.

Do not adopt arbitrary counts of diagnostic scenarios without measured work
budgets, or treat perfect knowledge of each future scenario as an executable
uncertainty model. Independent scenario re-solves are sensitivities; a deployable
stochastic policy must enforce the information constraints described above. Nor
does changing current time leave the numeric future value fixed: it changes the
simulated endpoint, which must be evaluated again. Only scoring within the same
supported family is established, not an unrestricted optimal continuation.

## Next implementation step and acceptance

This turn changes documentation only. First build a captured charge-timing audit
through the existing counterfactual compiler/scorer, then compare the compiled
policy's ranking. Extend/refine the family only where measured ranking or coverage
failures demonstrate a need. Complete the existing host/adapter and sole-writer
cutover before claiming this decision process controls the installed battery.

Acceptance cases must include:

- Actual energy below/equal/above the aligned reference; already elapsed charging.
- Cheap-before-peak with uncovered demand, and the same prices with demand already
  covered. Include profitable partial charging, not only full/empty extremes.
- Cheaper later replenishment with sufficient time/power, versus later recovery
  blocked by charge-rate or shared-grid constraints.
- Valuable upcoming PV headroom, negative prices, losses/wear and terminal terms.
- Independent future actions for both branches, cost-component reconciliation and
  a case where fixing the old future chooses the wrong current action.
- External-demand/PV sensitivity that flips the ranking, clearly labelled without
  invented probabilities or a claim of a measured saving.
- Late-quarter state, boundary rollover, stale readings, permission/scope changes,
  uncovered endpoints and bounded search failures.

Measure compiler work, wire size, HA latency, candidate omissions and held-out
ranking regret under comparable search effort. Repeated wrong rankings caused by
the fixed-suffix family require architecture reconsideration, not an SOC patch.
Native response/readback commissioning and final-writer exclusion remain separate
release gates; pure scoring tests cannot establish physical delivery.

## Implemented discharge-following correction

Planner v35 and its coordinated HA validator update correct the 17:30 discharge
case independently of the full policy cutover. If discharge covers the complete
final forecast residual, `supply_house` permits rated native discharge; a
partial allocation keeps its selected ceiling. The recorded 405.60 W prediction
therefore no longer imposes a 405.60 W limit on actual house demand. Forecast
flows, SOC and costs retain their original values. See the
[basic intent contract](battery-intent-v2.md) for rollout and physical guards.

This is a bounded control correction, not implementation of the C/F/J comparison
above. Unexpected consumption may use energy valuable later. Physical cutoff is
not an economic reserve, and partial slots remain forecast-capped. A proposed
fixed-future minimum-SOC guard was rejected: this capture's plan eventually
reaches the floor on September 18, so that guard would prohibit any extra
September 15 discharge even when a cheaper overnight refill repairs the future.
The full actual-state economic policy remains outstanding.
