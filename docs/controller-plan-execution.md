# Plan execution and deviation accounting

**Normative replacement design, 17 September 2026. Battery implementation completed
18 September 2026; equipment validation pending.**

See the [implementation and Verification guide](controller-plan-execution-implementation.md)
for the software boundary, diagnostic replay and rollout status.

See the [current-code gap analysis](controller-plan-execution-gap-analysis.md)
for the integration and server assessment and replacement sequence.

## 1. Purpose and authority

The controller MUST execute the planner's intended energy strategy using live
measurements. It adjusts instantaneous operation to actual household demand,
solar production and equipment capability. Departures from the accepted plan
MUST remain accounted for and MUST have an explicit disposition: recovery within
planner-authorised flexibility, incorporation into a replacement plan, or a
reported unfulfilled outcome. The controller MUST NOT independently replace the
planner's economic strategy.

The planner owns economic optimisation, future scheduling, service valuation,
recovery opportunities and trade-offs between objectives. The controller owns
local execution, physical feasibility, measured delivery, deviation accounting
and requests for replanning. An energy debt is an accountable shortfall relative
to the plan, not newly granted control permission or a physical constraint.

MUST and MUST NOT are requirements. Examples illustrate them; their numbers are
not defaults, tolerances or validity thresholds. This document specifies the
replacement behaviour, not the behaviour of currently installed software.

### Supersession and retained requirements

This specification replaces the controller decision model in
[battery execution](battery-execution-design.md),
[opportunity-cost selection](battery-opportunity-cost.md),
[policy binding](policy-binding.md), and the backend's
[controller policy](https://github.com/SHS-se/smart-home-solutions/blob/main/docs/energy-optimisation/controller-policy.md)
and [reactive controls](https://github.com/SHS-se/smart-home-solutions/blob/main/docs/energy-optimisation/reactive-controls.md).
Where those documents disagree, this specification takes precedence. Their
implementation records and historical evidence remain available.

The replacement retires controller selection by current cost plus alternative
future continuations, conditional economic bids, local horizon rescheduling,
and the requirement to compile those representations for live execution.
Planner-side economic models and search are not replaced by this decision.

The implementation may be rewritten completely. Reuse is optional: preserve the
required behaviour and evidence, not the existing classes, policy wire format,
continuation compiler or runtime structure. This is not a requirement to bolt an
accounting layer onto the old economic controller.

The following requirements remain authoritative within their stated scope:

- [Constraint requirements](constraint-requirements.md): no invented forecast
  admissibility bounds or source-timestamp event ordering.
- [Participation and battery supply](device-participation-and-battery-supply.md):
  inclusion, planning and control authority are separate; eligible supply and
  proportional solar attribution remain explicit.
- [Configuration error UX](configuration-error-ux.md): all known setup errors
  identify their fields and link to the actual editors.
- Existing physical limits, native routing, command durability, single SHS writer,
  authority changes, transport reconciliation and approved release semantics.
  These mechanisms enforce a request; they do not choose its economic purpose.
- Empirical battery conversion-loss collection and fitting, including source
  mappings, directional curves, installation overhead and measured/configured
  provenance. Retain this code and its evidence through the controller rewrite.

This design covers the common controller responsibility. The first implementation
slice is the home battery. EV and other device executors retain their existing
implementation until explicitly migrated. Thermal modelling, direct user-control
APIs and a notification framework remain separate work.

## 2. Behavioural contract

| Situation | Required behaviour |
| --- | --- |
| Plan calls for cheap-period charging | Continue charging toward its energy objective, subject to actual capability and permissions. A different locally computed future score is not a reason to stop. |
| Plan calls for household supply | Follow actual eligible net demand within the authorised operation and real limits. Forecast watts are not a cap on household demand. |
| Demand exceeds the forecast | Account for the resulting energy difference. Reduce charging when real headroom requires it; do not defer merely because the forecast was wrong. |
| Solar is below forecast | Preserve the charging objective using permitted sources where possible. Solar-only permission remains solar-only; grid permission is not inferred. |
| A temporary interruption prevents planned delivery | Record the shortfall, its evidence and an authorised recovery route if available. |
| Recovery cannot meet the original purpose or deadline | Request a new plan and expose the shortfall. Do not promise automatic recovery next quarter. |
| Lower demand or additional solar leaves more stored energy | Record a credit. Do not discharge simply to reduce accounting variance. |
| Battery is full | Respect saturation. Do not treat a charging command as delivered energy or accumulate a recoverable obligation to charge above capacity. |

An ordinary instantaneous adjustment is not a strategic deviation requiring an
exceptional event. Its accumulated energy effect still belongs in the accounts.
Changing charging periods, reversing the planned economic role, sacrificing
service, or buying recovery energy outside permitted opportunities is strategic.
Such a change requires explicit authority from the accepted plan or its replacement.

The size of a forecast error alone MUST NOT authorise or prohibit an action.
"Unexpected" means an observed condition with a relevant consequence for execution,
such as lost import headroom, unavailable equipment or unrecoverable energy drift.
The controller MUST record that consequence rather than a guessed appliance
identity, guessed event duration or arbitrary percentage threshold.

## 3. The planner's execution contract

Publish one versioned execution contract for the actual controllable scope,
separate from hypothetical schedules for Verification devices. It MUST contain:

| Part | Required meaning |
| --- | --- |
| Identity | Plan and intent revisions, physical group and participation/supply identities, model and conversion basis, source actuals watermark, and execution validity. |
| Reference | The accepted nominal trajectory, initial state and accounting basis, slot intervals, intended operation and expected gross flows. Partial first slots name their actual duration. |
| Objective | Stable objective identity, quantity/state or service sought, why timing matters, relevant checkpoints/deadlines and whether the quantity is a target, permission or forecast. |
| Live response | Supported native operation, source and export permissions, supply scope, equipment bounds, and how it responds to current demand/PV. |
| Recovery authority | Explicit windows, permitted corrections, recoverable quantities or state targets, resource allocation and original deadline. The planner supplies their economic justification. |
| Handover | The actuals and outstanding outcomes incorporated by the plan, with explicit dispositions for previous objectives. |

The nominal reference MUST remain unchanged during a plan revision. Local
recovery requests are overlays recorded against it, not edits that make actuals
appear to have followed the original schedule. Future portions of an old plan
stop accruing obligations when a replacement takes effect.
The contract must define cumulative nominal energy between slot boundaries, for
example by integrating a stated constant nominal power over the elapsed part.
That convention defines the reference at an arbitrary observation or handover
instant; it never interpolates actual meter delivery into invented measurements.

A permission or ceiling MUST NOT be interpreted as a delivery target. A house
supply forecast is an estimate of demand-following delivery; a charging objective
can instead specify an energy increment by the end of a cheap window. The plan
MUST make this distinction machine-readable. It MUST NOT demand energy delivery
that contradicts its own state, capacity or source assumptions.

Recovery authority is a compact instruction, not a catalogue of counterfactual
horizons. Its economic window and maximum usable correction come from the
planner's objective and available resources. They MUST NOT be manufactured from
forecast confidence bands. A recipe MUST include its deterministic allocation
and timing rule; the controller does not choose the cheapest future quarter.
For example, a recipe may request charging toward a specified stored-energy
target at allocated available power in the current window, with an explicit
maximum correction and deadline. It must state how nominal delivery and recovery
share that power. Corrections may occur in the remainder of the current quarter
or an authorised later window; a quarter boundary does not itself require delay.
Without such authority, the nominal plan can execute, but discretionary shifting
requires replanning. This is explicit contract semantics, not a legacy fallback.

No device receives a hard per-slot grid-energy allowance. Actual authorised
consumption may exceed its forecast, and real grid supply remains available under
the plan's permissions and physical limits. Recovery limits bound delegated
changes to the strategy, not the validity of household measurements.

## 4. Accounting model

### 4.1 Three separate records

1. **Actuals:** directional measured energy and state evidence, independent of
   commands and plans. This history survives plan changes and restarts.
2. **Reference and variance:** what the accepted plan expected over the same
   interval and the difference from actual delivery. This is derived from
   actuals and immutable plan references, not a separately editable balance.
3. **Obligation and disposition:** what still needs to happen to fulfil the
   objective, by when, under which authority, and what was recovered, incorporated
   into a new plan, explicitly retired, missed or remains uncertain.

An observed variance is not automatically a recoverable obligation. Lower demand
can remove a predicted need to discharge. A missed comfort period cannot be
delivered retrospectively by heating later. The planner must distinguish an
energy correction from a lost service outcome.

Keep charge, discharge, import, export, solar and household consumption gross and
directional. Every stream names its electrical boundary and units. At a common
AC boundary, over the same interval:

```text
grid import + solar + battery discharge
  = household consumption + battery charge + grid export + balance residual
```

The residual remains visible; do not clamp it away or hide it in household load.
Conversion losses belong at their stated boundary, once. An AC/DC conversion
model produces an estimate, not a new physical meter. Energy uses integer mWh in
the ledger (1 kWh = 1,000,000 mWh); calculations retain conversion/rounding evidence.

### 4.2 Battery debt and credit

Use stored-energy change as the common basis for battery recovery. For a shared
anchor and interval, with AC-side charge/discharge counters:

```text
O = opening reference energy - opening accounted energy
P = planned stored-energy change
A = eta_charge * measured_charge - measured_discharge / eta_discharge
D_flow = O + P - A

E_flow = opening accounted energy + A
R_state = observed stored energy - E_flow
D_state = planned stored energy - observed stored energy
        = D_flow - R_state
```

Positive `D` means below the reference (debt); negative `D` means above it
(credit). `P - A` is the interval's incremental variance; `O` carries the existing
balance. The opening accounted energy has a recorded observation/flow basis and
uncertainty. Never assume `O = 0` merely because a new quarter began.
DC meters use their declared conversion basis rather than applying
the AC formula again. Where efficiencies vary, use the versioned interval model.

The constant-efficiency equation above is illustrative, not a requirement to use
95% or another fixed efficiency. Preserve the existing empirical conversion
model, including power-dependent gain, fixed overhead and idle-loss treatment.
Its reported installation boundary is not automatically cell-internal storage
efficiency. The contract MUST identify the compatible model and electrical/storage
basis used for its reference; actuals retain their own measurement provenance.
Where a path has not been identified empirically, its configured assumption stays
explicitly labelled. Do not count overhead twice or claim a site residual is an
isolated solar conversion measurement. A new fitted model does not silently
rewrite an accepted reference or settled history; basis changes require the
explicit reconciliation described in §6.

Publish `D_flow`, `D_state` and `R_state` separately with their evidence and
uncertainty. SOC resolution, capacity calibration, auxiliary consumption and
conversion-model error can explain differences; they MUST NOT be silently
labelled controller delivery or converted into a repayment command. Record any
state reconciliation as a separate adjustment, never invented charged energy.

Recovery quantity is derived from the outstanding objective and actual state,
including known subsequent delivery. Charging more than the nominal trajectory
can reduce a stored-energy debt. Reduced later charging can consume a useful
credit where the plan authorises it. Different deadlines and service objectives
MUST NOT cancel each other merely because their signed kWh sum to zero. Gross
throughput and cost remain visible even when net deviation is zero.

### 4.3 Evidence, time and uncertainty

Meter evidence settles delivery; service-call success and setting readback do not.
Keep requested, acknowledged, measured and projected quantities separately.
Provisional power integration, if implemented with a declared measurement basis,
MUST be labelled estimated and replaced/reconciled with counter evidence once;
it MUST NOT be added to the same counter delivery.

Compare reference and actuals over identical intervals. A counter interval crossing
a quarter or plan boundary does not prove how energy was distributed inside it.
Carry the known aggregate and unresolved allocation, or defensible bounds; do not
invent proportional measured delivery. A missing reading is not measured zero.
If uncertainty changes whether a correction is warranted, do not claim exact
repayment; obtain evidence or request replanning as appropriate.

Process events in receipt order with locally generated revisions. Source timestamps
are provenance and interval evidence, not authority to reject a later received
update as going backwards. A repeated cumulative value creates no new energy.
Resets, replacement sources and corrections retain explicit provenance and unresolved
gaps rather than silently creating a new zero baseline or negative consumption.
Corrections amend the accounts through recorded revisions, including any
affected plan handover; they never rewrite the recorded commands.

### 4.4 Mandatory accounting invariants

- Each physical interval contributes to measured totals once, independently of
  how often the controller evaluates, retries or replans.
- Opening deviation plus planned increment minus actual increment, plus explicit
  reference/state corrections, equals closing deviation on the stated basis.
- Planned delivery, requested delivery and recovery forecasts never settle debt.
- Every objective has an identifiable balance and disposition. A quarter rollover,
  restart, mode change or new plan ID cannot erase it or move its deadline.
- Physical observations, scope attribution and cash-cost estimates are distinct.
  Neither a modelled saving nor a scope allocation is measured energy.
- No unexplained adjustment is booked as recovery. Unknown evidence and residuals
  remain visible even if the controller is currently operating normally.

## 5. Execution and recovery

For each relevant event or deadline, the controller MUST:

1. Update observations, delivery and pending effects under current authority.
2. Derive the active nominal request and objective from the accepted contract.
3. Adapt its native response to measured demand/PV and real equipment limits.
4. Reconcile deviation and remaining service against the same reference.
5. Apply only a supported, planner-authorised recovery correction, checking current
   resources and its original deadline. Record the correction separately.
6. Reconcile the resulting request through the existing actuator-group owner and
   publish accounting evidence. Request replanning if authorised execution cannot
   fulfil the objective or the strategic assumptions need revision.

Protective changes required by real equipment limits take effect without waiting
for a replan. Their energy consequences remain accounted for. Loss of a recovery
opportunity is not permission to violate a physical limit or continue a strategy
beyond its authorisation. A replan request does not itself stop otherwise valid
nominal execution, grant new permissions or extend plan validity.

### Recovery feasibility

Before labelling a debt recoverable, identify the remaining authorised windows,
their permitted correction and available power, the original need/deadline,
capacity and source restrictions, and competing requests/pending effects.
Distinguish **authorised**, **projected feasible**, **currently executing** and
**measured complete**. Future headroom is a forecast, not a guaranteed reservation
of uncontrollable household demand.

Shared headroom MUST be allocated once across the household. A recovery rule
involving multiple controllable groups must include planner-owned allocation or
ordering and account for joint native constraints. Do not invent permanent device
priorities. Where the supplied instructions cannot resolve contention, use only
authorised physically feasible actions and request a new plan. A hypothetical
Verification stop cannot release headroom; an unconfirmed real stop cannot yet
fund another load.

As opportunity is consumed or conditions change, recompute projected recovery.
If the remaining authorised capacity is insufficient, report that promptly rather
than waiting for the deadline or indefinitely rolling debt into later quarters.
No guessed duration of an unexpected load may establish recovery feasibility.

The controller MUST NOT reverse an operation merely to remove a credit. Deliberate
discharge for future solar headroom belongs in the planner's strategy. Unneeded
predicted consumption is an explained variance; wasted energy is not repayment.

Unchanged effective native targets reuse their execution identity where the
adapter permits. Accounting updates alone MUST NOT cause redundant mode changes.
Native protections and response limits remain authoritative; this spec adds no
SHS minimum-runtime timers or numeric switching thresholds.

## 6. Replanning, handover and restart

A replan request includes actuals watermark, current observed state, unsettled
intervals, objective outcomes, pending physical effects and the reason for revision.
Coalesce requests without losing their evidence or deadline. The backend MUST
acknowledge which prefix and objectives it incorporated and explicitly identify
which outstanding outcomes are retained, changed or retired and why.
Locally assigned request generations identify superseded work. A response for a
superseded generation MUST NOT displace a newer accepted plan; source timestamps
are not used to decide that ordering. Repeated delivery of the same accepted
contract is idempotent, including its objective dispositions.
An omitted disposition cannot clear an outstanding outcome: retain it as unresolved
and request a corrected handover. Do not guess that a new target incorporated it.

At locally accepted activation time `c`, close the old reference's execution
interval and open the new one atomically with the handover record. A response
based on an earlier snapshot does not become current merely because it arrived.
Reconcile the intervening actuals and pending effects, then validate the new
contract against current permissions and state. It MUST NOT replay an elapsed
slot, count snapshot-to-arrival energy twice, or discard that interval's variance.
If the intervening change cannot be accommodated by its instructions, retain the
applicable accepted contract and request an updated plan; no new permission is
inferred. Expired or revoked authority follows the existing release protocol.

For comparable stored-energy references at the same activation instant:

```text
reference_adjustment = E_reference_new(c) - E_reference_old(c)
D_state_new(c) = D_state_old(c) + reference_adjustment
```

The actual energy in both accounts is identical. The adjustment is a planner's
change of reference, not charging, discharge or measured recovery. A replacement
planned from actual state may incorporate the old shortfall and start with zero
current variance. It MUST retain the old shortfall, its incorporation link and
any missed service/deadline in history; it MUST NOT add the incorporated debt
again to the new target. Unknown interval allocation remains unknown across the
handover. Changed capacity/conversion bases require an explicit reconciliation,
not this same-basis equation applied without qualification.

Only a recognised planner disposition or measured fulfilment closes an obligation.
Deadlines anchored to a service event do not move with the rolling horizon. A
cancelled or missed objective remains distinguishable from successful delivery.
Late evidence corrects the affected history and current outstanding balance once.

Persist the accepted reference, objective dispositions, actuals cursors, outstanding
deviations, recovery progress and pending command effects together with their
identities. Restart restores them without creating new energy, clearing debt,
extending validity or resetting deadlines. Reconcile actual equipment state before
new writes. Continue compatible authorised execution or follow the existing
release protocol; do not add a parallel legacy economic-controller fallback.

## 7. Diagnostics and user-facing evidence

For every accounting interval and decision, retain:

| Evidence | Question it answers |
| --- | --- |
| Plan/objective revision, nominal operation, expected energy and deadline | What were we trying to deliver, and why then? |
| Requested operation/limits, authority, changes and acknowledgements | What did the controller actually ask the equipment to do? |
| Directional metered flows, state, interval and measurement basis | What physically happened, and what remains uncertain? |
| Opening/closing debt or credit, increments, residuals and adjustments | Does the account reconcile? |
| Deviation reason with source observations or command evidence | Why did execution depart? |
| Recovery authority, allocated windows, remaining quantity and feasibility | How can the original objective still be delivered? |
| Replan request and old/new disposition links | Where did responsibility for the outstanding balance go? |

Reasons distinguish live demand following, physical headroom reduction, saturation,
source restriction, equipment unavailability, transport/response discrepancy,
measurement uncertainty and planner-authorised amendment. Use **unexplained**
when evidence does not establish a cause; do not retrofit a plausible economic
story. One aggregate deviation can have multiple evidenced contributions.

The primary explanation presents plan, measured delivery, difference and next
authorised action. For example: “0.50 kWh below planned stored energy; charging
was reduced by the import limit; recovery of 0.50 kWh is planned before 18:00.”
Show whether that recovery is conditional, in progress, measured complete or no
longer feasible. A credit is not itself an error, and a command is not completion.

Replace the selected-continuation outlook with the accepted plan plus explicit
recovery overlays and their provenance. Do not present an independent controller
forecast as the plan. Realised cost, projected cost and preference penalties stay
separate; no unsupported savings claim is inferred from a reconciled kWh account.

Setup failures follow the configuration UX contract, including cross-section
readiness and direct field correction. Measurement and equipment failures must
identify their actual source rather than link to an unrelated configuration field.
This document requires visible status and exportable evidence, not notifications.

## 8. Worked examples

### Missed cheap-period charging

The plan seeks 1.00 kWh of added stored energy in a cheap quarter. Actual delivery
adds 0.50 kWh because available import headroom limits charging. The debt is
0.50 kWh on the stored-energy basis. A nominally successful command does not alter it.

The planner has authorised the next quarter for this recovery before the energy
is needed. With charge efficiency 0.95, 0.50 kWh of stored-energy recovery needs
approximately 0.5263 kWh at the AC charge boundary. Across a full 15 minutes this
requires about 2.105 kW **additional** average charging above that quarter's nominal
request. Check the combined request, battery headroom and other loads, not just
the 2.105 kW increment. Use remaining time when the quarter is already underway.

If only 1 kW of additional AC power remains available for that quarter, it can
add at most 0.2375 kWh of stored energy under these assumptions. The remaining
0.2625 kWh is not silently deferred past the deadline: report insufficient recovery
and replan. If later measurement proves additional delivery, settle the account
from that evidence, not the earlier projection.

### Less demand and a full battery

The plan forecasts household supply requiring a 0.40 kWh stored-energy reduction.
Actual eligible demand requires only 0.10 kWh. The battery is 0.30 kWh ahead of
the reference, with correspondingly lower gross discharge. The controller does
not export or discharge unnecessarily. The credit can reduce a later authorised
charge or be incorporated into the next plan. If already full, a charging ceiling
cannot establish further delivered charge; saturation and the remaining objective
are explicitly reconciled.

### Expensive-period load spike

The accepted operation supplies eligible household demand. An unexpected appliance
increases net demand; the battery follows it within actual capability and scope.
The extra discharge creates a measured debt relative to the forecast. The controller
does not freeze at forecast watts or buy energy immediately just to cancel the
debt. It uses the planner's authorised recovery route or requests a new plan if
the remaining energy jeopardises later service.

## 9. Domain boundary and implementation sketch

The following is a domain sketch, not a released wire schema or a requirement to
reuse current modules. A small event-driven controller can own the entire
execution account and desired requests. Callers post events; they do not coordinate
separate account, recovery and command mutations. Existing reconciliation and
metering code may be reused only where it fits this boundary and these requirements.

```python
# Nominal admission, then live adaptation and settlement.
controller.post(ExecutionPlanOffered(contract))
controller.post(Observed(frame))
controller.post(MeterObserved(sample))

# Replacement carries its actuals prefix and dispositions.
controller.post(ExecutionPlanOffered(replacement))
controller.post(TransportResult(operation_id, result))
# Returns are evidence, never synthetic MeterObserved events.
```

```text
ExecutionContract = identity + validity + actuals_anchor + objectives
                  + reference_intervals + recovery_instructions + handover
Objective = id + physical_group + typed_target + timing + permissions
TypedTarget = StoredEnergyTarget | DemandFollowing | DeviceServiceTarget
RecoveryInstruction = objective_id + authorised_windows + correction_rule
                    + resource_allocation + deadline + economic_reason
ExecutionAccount = actuals_cursor + reference_revision + objective_dispositions
                 + recovery_progress + reconciliation_records
Disposition = Open | Fulfilled(evidence) | Incorporated(plan, objective)
            | Retired(planner_reason) | Missed(evidence)
Assessment = nominal_request + effective_request + variances
           + recovery_projection + reason_evidence + replan_need

advance_execution(state, event, now) -> (state, effects)
assess_execution(contract, account, observations, pending_effects, now) -> Assessment
reconcile_plan(old_contract, new_contract, account, actuals, now) -> HandoverResult
```

Uncertainty and evidence quality accompany quantities; they are not a success state
or a numeric zero. A `DeviceServiceTarget` requires its own service model and
measurement definition before migration; electrical kWh is not a generic substitute
for temperature, comfort, filter runtime or EV readiness.

| Owner | Knowledge and invariants |
| --- | --- |
| Backend planner/contract producer | Nominal strategy, objectives, feasible recovery instructions, shared allocations and replan dispositions. |
| HA execution domain | Admission, atomic reference handover, derived variance, authorised correction and replan reasons. Sole shared-state writer. |
| Actual-energy ledger | Physical source identity, directional totals, uncertain intervals, receipt revisions and correction provenance. Independent of control authority. |
| Actuator-group adapter/host | Native response, durable attempts, permissions, pending effects, confirmation and release. No economic ranking. |
| Diagnostics/read models | Derived account and command views. No separate mutable debt balance or decisions. |

Parse transport and storage representations at their boundaries. Index objectives
by stable identity, reference intervals by active time, meter streams by physical
boundary/direction and commands by physical group. Store dispositions and evidence;
derive balances from their anchored accounting prefix and retained increments.
Retention must preserve settled totals and unresolved intervals needed by open
objectives, without requiring a successful replan to continue metering.

The shared-state transition must not wait for network, disk or actuator responses.
Effects run asynchronously and return correlated results. Preserve one SHS command
owner per coupled physical group and durable evidence before sending where a crash
could otherwise lose an issued effect. These are behavioural requirements; they
do not require the existing generic runtime framework or its checkpoint schema.

## 10. Required acceptance evidence

| Trace | Required result |
| --- | --- |
| Nominal cheap charging with ordinary forecast noise | Charging purpose retained; measured variance explained; no independent future-score reversal. |
| Large load change with sufficient import headroom | No deferral solely because forecast and measurement differ. |
| Real import limit interrupts charging | Immediate feasible reduction, measured debt and explicit recovery route or replan. |
| Lower solar with and without grid-charge permission | Objective preserved where permitted; source restrictions never widened. |
| Demand-following load spike | Actual eligible demand can exceed forecast; real limits and supply scope still hold. |
| Recovery across quarters | Stored/AC conversion, remaining duration and nominal-plus-correction power reconcile. No debt reset at the boundary. |
| Nonzero opening balance | With 0.50 kWh opening debt, 0.50 kWh nominal increment and 1.00 kWh actual increment, closing debt is zero; test the corresponding credit case and explicit reference amendment. |
| Recovery becomes impossible or cheap window closes | Visible remaining shortfall and replan; no endless postponement or unapproved expensive purchase. |
| Credit, full battery and lower-than-forecast demand | No forced disposal, fake charge delivery or pointless cycling; clear objective disposition. |
| Equal charge/discharge net effect | Gross throughput, conversion losses and cost retained. |
| Command acknowledged but no delivery | No repayment; request/response discrepancy visible. |
| SOC and flow evidence disagree | State residual reported separately; no fabricated meter energy or automatic corrective cycling. |
| Unknown meter tail, reset or cross-boundary counter interval | Known aggregate retained, allocation uncertainty explicit; no invented zero or pro-rata actuals. |
| Duplicate reports, retries and later source timestamp regression | Receipt order honoured; no double counting or rejection merely for source-time order. |
| Delayed or repeated replan response | Post-snapshot delivery reconciled once, old/new reference adjustment explicit, incorporated debt not added again. |
| Response n arrives after n+1 was accepted | The locally superseded response cannot restore the old strategy or repeat dispositions; no source-timestamp ordering is used. |
| Late correction after replan | Correct affected history and outstanding quantity once; do not mutate historical commands. |
| Replan after a missed service deadline | Historical miss remains visible; no new rolling deadline masquerades as fulfilment. |
| Restart during recovery or ambiguous transport | Same actuals, outstanding balance, original deadline and possible effects recovered durably. |
| Shared headroom and Verification loads | Resources allocated once; hypothetical or unconfirmed stops do not fund charging. |
| Permissions, scope or participation change | New requests use current authority; physical history and previously issued effects survive. |
| Missing cross-section setup fields | Full field enumeration, readiness, visibility, highlight, navigation and correction-clearance path pass. |
| Export/replay of an unexplained decision | Account can be reconstructed from the exported reference, actuals and adjustments; missing cause stays unexplained. |

Exact numerical equality applies to the ledger's declared integer arithmetic.
Physical measurement tolerance comes from source resolution/calibration and
conversion evidence, not an invented forecast band or service-loss allowance.
Projection feasibility does not prove future delivery or hardware response.

## 11. Design rationale

Independent Codex and Claude reviews both supported plan execution with explicit
deviation accounting. The Codex domain design is the base: one active reference,
directional actuals and derived obligations behind one event boundary. It gives
callers fewer opportunities to update a request without updating its account.
Adopt Claude's explicit distinction between adherence, operational departure and
authorised recovery, and its separation of recovery permission from feasibility.

Do not adopt separate caller-managed admission/selection/settlement stages,
automatic expiry as debt repayment, one guessed cause per deviation, acceptance
of superseded replan responses, or retroactive attribution to a delayed plan's
original start. Do not classify every demand-following deficit as harmless: its
effect on the outstanding objective still needs reconciliation. These choices
keep one coherent accounting and authority model.

| Alternative | Decision |
| --- | --- |
| Add an adherence penalty to current economic ranking | Rejected: tuning another score still permits independent strategic reversal and does not define fulfilment or debt disposition. |
| Rigid quarter-hour energy budgets | Rejected: forecast demand is not a limit on real consumption, and available energy may legitimately carry across quarters. |
| Local recovery optimiser | Rejected: recreates a second planner with competing strategy ownership. |
| Plan reference plus compact authorised corrections | Selected: every change is attributable to a plan instruction, physical need or visible unresolved discrepancy. |

We accept less opportunistic local economic freedom in exchange for adherence
and auditability. We accept explicit measurement uncertainty and additional replan
requests where necessary in exchange for avoiding fictitious delivery or invented
recovery promises. Recovery instructions must stay small; if their producer needs
a catalogue of alternative horizons, simplify the delegation and replan instead.

## 12. Delivery boundary

Implement the accounting and admission core first against deterministic traces,
including the worked examples, a plan handover and uncertain meter intervals.
Then publish a new versioned producer/consumer contract, generated fixtures and
real consumer tests before replacing the live selector. Remove the old economic
selection path from the migrated scope; do not retain dual decision owners or
silently translate old policy meaning into the new contract.

Reuse or rewrite modules as appropriate. Preserve and verify the required native
command mapping, authority, release and crash-recovery behaviour.
Replay captured installations to separate plan error, execution error, physical
non-delivery and measurement uncertainty. Commission the actual adapter before
claiming delivered performance. Documentary agreement does not establish hardware
readiness or change an installed operating mode.

Wire schema identifiers, calibrated measurement error, supported native correction
steps and benchmark limits require implementation evidence. This specification
does not invent values for them. These are engineering tasks, not missing product
decisions blocking the agreed plan-execution objective.
