# Plan-execution specification: implementation gap analysis

17 September 2026. Target: [Plan execution and deviation accounting](controller-plan-execution.md).
Read-only source review of HA integration `14890a3` and backend
`smart-home-solutions-t-by` `be1f506`, including the uncommitted replacement
specification. These checkout revisions do not establish what is deployed.
This assessment authorises no implementation or operating-mode change.

## Assessment

**The planner already communicates a substantially complete nominal electrical
balance for each quarter. The missing piece is an accountable execution contract,
and the live battery controller currently uses a different decision model.**

The replacement is substantial work in both repositories. Adding debt telemetry
around the current economic selector would preserve the central conflict. The
server must publish objectives, recovery authority and handover dispositions;
HA must execute those instructions and reconcile physical delivery against an
immutable reference.

This does not require discarding the planner's existing flow calculations.
Final schedule materialisation, physical metering, authority checks and native
command reconciliation provide useful foundations. Their existing semantics must
be assessed separately from their names.

## Does the planner allocate every kWh?

### Nominal quantities: largely yes

[`PlannedSlot`][B-plan] (lines 535–586) and its producer (4054–4120) publish:

| Quantity | Existing representation |
| --- | --- |
| Interval | Canonical quarter start and `duration_hours` |
| Solar | Raw and available PV power, curtailed power |
| Household demand | Base demand, individual device loads, aggregate load and unserved demand |
| Battery | Separate charge, discharge and battery-export power; expected closing SOC |
| Grid | Separate import and export power |
| Execution | Battery operation/permissions/ceilings, device commands and service schedules |
| Economics | Prices, import cost, export revenue and decision/allocation diagnostics |

For each average power, `expected_kWh = watts × duration_hours / 1000`.
The server uses that arithmetic for cost and totals (3949–3955, 4123–4128), and
checks the final unrounded electrical balance (4135–4152):

```text
PV + grid import + battery discharge
  = load − unserved demand + battery charge + grid export + curtailed PV
```

`battery_export_w` is a subset of discharge and grid export, not an additional
energy source. Device demand is already included in `load_w`; adding it again
would double count consumption.

**Missing explicit kWh columns are therefore not the principal gap.** The main
questions are which quantities constitute obligations, their accounting basis,
and who is authorised to change their timing.

### Important qualifications

1. **The first quarter can be partial.** The public start remains the quarter
   boundary, while `duration_hours` covers the remaining modelled interval
   ([producer][B-plan], 1635, 4055–4056). HA already derives the effective start as
   quarter end minus duration ([runtime][H-runtime], 65–66). A new reference
   should expose explicit effective start/end and cumulative reference semantics.
2. **Public numbers are rounded.** The server captures exact final rows before
   rounding public watts to two decimals and SOC to six ([producer][B-plan],
   4045–4099). The exact battery projection is stored separately from the plan
   returned by ingestion ([endpoint][B-ingest], 2030–2032, 2166–2168). Agree a
   canonical accounting precision/rounding rule; do not treat display precision
   as exact integer-mWh ledger truth.
3. **Electrical balance is not a complete source-to-device assignment.** Auction
   diagnostics include solar/grid contributions to store allocations, discharge
   destinations and some energy transfers ([diagnostic types][B-dispatch],
   176–207). Those are economic attributions, not a universal physical routing
   matrix. Some thermal demand is added after the auction, explicitly diagnosed
   as `load_added_after_dispatch` ([producer][B-plan], 3984–3996). The exact battery
   projection separately attributes solar charging using PV surplus after house
   demand ([projection][B-projection], 225–238). Preserve distinctions among
   forecast flows, economic attribution and measured energy.
4. **Choose the correct plan branch.** Schema 9 has hypothetical top-level
   scenarios and a separate physical `execution_plan` ([producer][B-plan],
   4644–4670). Verification battery projection is conditional on the other
   devices' physical schedule. HA selects a branch by operating mode
   ([mode selection][H-modes], 66–76). Execution `base_w` can include external
   device demand folded into fixed demand ([scope projection][B-scope], 53–115);
   it is not necessarily the UI's canonical Base category. Resolved 23 September
   2026: both branches carry the same selected schedule, the per-mode projection
   was removed, and the branch choice no longer changes what is executed or
   verified.

The new design needs a conserved nominal reference and declared attribution where
supply scope requires it. It does not need to pretend that individual electrons
can be traced from a source to an appliance on a shared bus.

### Captured replay check

The supplied `plan-replay-…-2026-09-17T15-30-00+00-00.json` has 288 nominal priority
slots. Independent arithmetic over the public rows found a maximum electrical
balance residual of approximately **0.01 W**, device-demand partition residual
below `5e-13 W`, and SOC recurrence residual below `1e-6` of capacity. These
results are consistent with the published rounding.

Its first quarter starts at 15:30 UTC, but the snapshot is effective at
15:38:13.799: the modelled duration is **6.770 minutes**, not 15. The hypothetical
first row represents approximately 0.141901 kWh demand, 0.090748 kWh PV and
0.051153 kWh battery discharge. Integrating it over a full quarter would overcount.

This capture places the battery in **Verification**; the physical execution plan
has no battery model. Its first-row demand is approximately 0.119043 kWh, supplied
by 0.090748 kWh PV and 0.028295 kWh grid import. This is evidence about generated
plans, not a measurement of battery execution or a reproduction of the reported
bad live decisions.

## Gap map against the specification

“Partial” means useful machinery exists, not that the requirement is satisfied.

| Spec requirement | Current implementation | Assessment and required change |
| --- | --- | --- |
| §§1–2: planner owns strategy | Live battery ranks immediate plus future economic scores | **Conflicting.** Replace independent selection with nominal response and authorised correction. |
| §3: nominal flow reference | Balanced final flows, duration, SOC and native commands | **Partial.** Add canonical interval/energy basis and distinguish targets, forecasts and permissions. |
| §3: stable objectives and original deadlines | Some device services have IDs, required energy and deadlines; battery has schedule and valuation | **Missing execution semantics.** Add stable objective lineage, typed targets and outcomes, especially for battery. |
| §§3,5: recovery authority | Counterfactual continuation policy, no objective-linked recovery recipe | **Missing.** Planner supplies windows, quantity/state, deterministic timing, resource allocation and economic reason. |
| §4: gross physical actuals | Directional counter ledger, epochs and bounded uncertain intervals | **Partial.** Reconcile basis, receipt ordering and corrections; preserve physical totals independently of authority. |
| §4: deviation account | Policy actuals settlement and SOC observations | **Missing.** Opening deviation, reference increment, actual increment, state residual and explicit amendments. |
| §5: shared recovery feasibility | Equipment/scope protections and current native state | **Missing household recovery ownership.** Allocate shared resources once, including pending real effects and Verification exclusions. |
| §6: replan responsibility | Plan replacement, economic continuity, identity checks and historical upload acknowledgement | **Partial infrastructure; missing semantics.** Incorporated receipt prefix, dispositions, atomic reference amendment and local request-generation ordering. |
| §6: persistence/restart | Durable physical runtime/command checkpoint | **Partial.** Persist and restore reference, obligations, original deadlines, corrections and recovery progress as well. |
| §7: account diagnosis/export | Runtime status, faults, alternative scores, selected-continuation outlook | **Missing account view.** Export reconstructible reference, actuals, amendments, outcomes and recovery evidence. |
| §§9–10: one transition owner and acceptance evidence | Battery reducer/async host; other devices use scheduled controller | **Partial architecture.** Establish coherent ownership and new end-to-end traces; old policy tests do not establish conformance. |

## Principal findings and code evidence

### 1. The live controller deliberately chooses a new battery strategy

HA constructs `BatteryRuntime` in the production entrypoint
([setup][H-setup], 193–201); the scheduled controller skips its legacy battery
branch when that runtime exists ([scheduled controller][H-controller], 1203–1208).

The runtime reads the plan but enumerates hold, solar charge, house supply,
grid charge and optional export at plant ratings. It sends those alternatives
with `reference_id: 'hold'` ([runtime][H-runtime], 314–347). The server adopts the
operations and reference ([exchange][B-exchange], 301–302). HA then scores each
operation with its compiled economic continuation and an incumbent deadband
([selector][H-selector], 579–607).

There is no nominal trajectory obligation in that selection. This is the most
important behavioural replacement, not a missing explanatory label.

The current regression test `test_source_cut_and_future_permissions_ignore_old_dispatch_choice`
sets a slot's `allow_grid_charge=False` and still expects future grid charging to
be permitted ([test][H-runtime-test], 343–350). Under the old contract this is an
intentional distinction between selected dispatch and permissions; it is not
proof of an unauthorised command. It demonstrates the different decision model.

Do not replace the selector with naive playback of forecast watts. The existing
native command producer already distinguishes demand-following rated ceilings
from intentionally partial supply ([commands][B-commands], 29–53). The new
typed objective must say what follows actual load and what is an energy target.

### 2. The server's missing outputs are responsibilities, not basic flow data

The public plan/scenario shapes contain service slots and diagnostics, but no
battery objective lineage, opening balance, authorised recovery schedule or
previous-objective dispositions ([types][B-plan], 589–700). Device service inputs
do contain required kWh and earliest/deadline values (252–262), and EV targets
exist (223–241); a claim that the planner has no objectives anywhere would be wrong.

The separate exact battery projection contains the selected materialisation and
usable stored energy, but strips service economics to an empty list. Its
`actuals_watermark` is the first projected interval's timestamp, not a receipt
cursor identifying incorporated controller evidence
([projection][B-projection], 159–218). The exchange constructs a continuation
search request ([exchange][B-exchange], 274–319). This is not recovery authority.

Required server changes span the planner output types/materialiser, request and
response schemas, ingestion/storage, identity/version negotiation, replan inputs,
and generated consumer fixtures. Recovery allocation must be derived from the
planner's strategy and resource constraints. It cannot be manufactured by a
serializer from flow numbers alone.

### 3. The existing meter ledger is not the deviation account

`PolicySession` stores compiled policy, actuals settlement and selection state
([state][H-state], 548–592). Meter receipts update its physical ledger and
settlement (1460–1479). These records do not compute the specification's
`opening debt + planned increment − actual increment`, retain objective
dispositions or distinguish SOC residual from missing delivery.

The live runtime registers grid counters at `grid_ac` and battery counters at
`battery_dc` ([runtime][H-runtime], 433–436). Planner battery flows participate
directly in the AC balance and pass through efficiencies when updating storage
([producer][B-plan], 3935–3939, 4135–4137). The projection also uses energy above
minimum SOC, whereas public SOC represents the battery's total fraction.

The new reference must identify electrical boundary, storage origin and conversion
model. Subtracting these quantities directly, or applying losses twice, would
produce fictitious debt. This finding does not assert that the old economic
compiler's conversion arithmetic is wrong.

**Retain the existing empirical conversion code.** The user explicitly confirmed
this during the review. [`battery_conversion.py`][H-conversion] already fits
directional gain and fixed overhead from aligned observations, records fit
evidence and distinguishes measured models from configured assumptions
(111–173). The runtime loads two days of five-minute HA statistics
([coordinator][H-coordinator], 517–540), refreshes its model hourly or when sources
change ([runtime][H-runtime], 306–311), and sends the model to the policy exchange
(341–347). This capability is independent of continuation ranking and should
survive its removal.

The supplied `shs-controller-diagnostics (27).json.gz`, captured at approximately
15:37 UTC on 17 September, already includes a fitted model in
`current.devices[18].battery_runtime.loss_model` and its `loss_evidence`:

| Branch | Captured evidence | Result |
| --- | --- | --- |
| Grid charging | 54 windows; 26.322 kWh input | Measured gain 0.947965, fixed overhead 36.34 W; observed output/input energy ratio 94.18% |
| Discharge | 63 windows; 8.496 kWh input | Measured gain 0.974670, fixed overhead 143.45 W; observed output/input energy ratio 88.60% |
| Idle | 93 windows | Measured installation overhead 124.63 W |
| Solar charging | 1 window; source paths not isolated | Configured 0.95 gain retained and explicitly labelled; no empirical converter fit |

These ratios describe the sampled installation including overhead, not universal
cell efficiency or a measured round trip. The curve is
`output_W = max(0, gain × input_W − overhead_W)` for positive input; therefore
effective efficiency varies with power. Idle and directional overhead must not
be added twice. Solar source isolation is a measurement limitation, not simply
a shortage that accumulating more of the same observations necessarily fixes.

The same diagnostic export's planner battery inputs still contain configured
charge/discharge efficiencies of 0.95. Thus the evidence supports a more precise
gap: empirical calibration **exists and has yielded figures**, but the nominal
planner reference and the live model do not yet share the new explicit accounting
contract. Preserve capture/fitting/provenance, communicate the compatible model
to the new producer/consumer contract, and record model changes explicitly instead
of retroactively repricing energy or rewriting measured delivery.

The live ledger's meter registration covers battery and grid flows. Historical
household/PV quarter upload exists separately in
[`coordinator._actual_quarters`][H-coordinator] (2192–2238). Neither pipeline should
silently become the complete authoritative account: measurement provenance,
unknown intervals and any balance-derived values must remain explicit.

### 4. Replanning does not transfer the new responsibilities

The server's `ReplanReference` retains previous operation/powers for economic
continuity, with a price deadband ([continuity][B-continuity], 23–45).
`actuals_accepted_until` acknowledges historical quarter uploads
([ingestion][B-ingest], 2158–2168). HA stores that acknowledgement and replaces its
cached accepted plan ([coordinator][H-coordinator], 3052–3057, 3121–3131).

Existing policy admission reconciles physical actuals before installing a session
([state][H-state], 1090–1113). Identity/context checks also reject some obsolete
work ([exchange][B-exchange], 101–108; [runtime][H-runtime], 355–363). Preserve
those guarantees, but do not confuse them with objective handover.

Missing are the incorporated receipt prefix and explicit retain/incorporate/retire
dispositions, the common-instant reference adjustment, intervening actuals and
late-correction treatment, and idempotence of those postings. A plan starting from
current SOC is not evidence that an old miss was acknowledged; nor should already
incorporated debt be added to its new target a second time.

The current renewal hook asks for another battery policy
([runtime][H-runtime], 587–593). It does not communicate an execution shortfall,
lost recovery window or unresolved objective to the planner.

### 5. Source-time admission contradicts receipt-order requirements

`BatteryRuntime._meter` silently drops readings whose source timestamp is equal
to or earlier than the preceding sample ([runtime][H-runtime], 453–456).
The ledger rejects a later-revision sample when its source time regresses
([ledger][H-ledger], 98–109); tests explicitly preserve this behaviour
([ledger tests][H-ledger-test], 166–182).

Replace this with receipt revisions and explicit correction/reset/uncertainty
semantics. Timestamps may describe measurement intervals; they cannot decide the
order of received events. Do not mistake the already improved instantaneous
observation path for a fix to this separate cumulative-meter path.

### 6. Admission, persistence and diagnostics need accounting semantics

The HA plan validator checks many command, scope, device and SOC requirements but
does not validate `duration_hours` or the complete flow balance
([validator][H-validator], 1020 onward, especially 1283–1351). An in-memory probe
against the existing schema-8 fixture confirmed that it accepts, separately,
`duration_hours=-0.25`, `grid_import_w=-999`, or `load_w=99999999` in the first
priority slot. This does not claim the planner emits those values. It shows that
the consumer boundary cannot yet treat the schedule as a validated accounting
reference. The enormous load is relevant because it breaks the row's balance,
not because large demand should be prohibited.

Required checks should implement the new contract's arithmetic, units, identity
and interval semantics. Do not add forecast-confidence bounds or demand ceilings.

The runtime restores a physical checkpoint and then releases previous commands
([runtime][H-runtime], 114–159). Durable native reconciliation is useful, but it
cannot restore obligations absent from its state. Compatible continuation or
release must be tested with preserved accounts, deadlines and pending effects.

Diagnostics export runtime and delivery snapshots ([export][H-diagnostics],
18–22), while the outlook explains an independently selected continuation.
There is no reconstructible reference/deviation/disposition history. Battery also
bypasses the scheduled controller's per-device diagnostic evaluation
([controller][H-controller], 1203–1224); its own snapshots exist, so this is not
an absence of all battery diagnostics.

### 7. Other devices cannot inherit battery accounting by renaming it

EV, pool, boiler and generic devices still execute through the scheduled
controller ([controller][H-controller], 1212–1254). Their existing service
schedules are useful inputs, but they do not share the proposed objective and
disposition account. Some generated service IDs depend on horizon start or
planning deadline ([planning][H-planning], 218, 265, 453); stable service-event
lineage must be established rather than assumed.

Electrical kWh alone does not establish temperature, filter runtime or departure
readiness. Define each service's fulfilment evidence before migrating it. Joint
recovery needs planner-owned allocation across real controllable groups; a shared
execution lock is not a reservation of household power. Preserve the existing
configuration correction UX and test any new required fields through the complete
readiness/navigation/correction path.

## Recommended replacement boundary

The agreed specification remains authoritative. The following is an implementation
boundary for closing the gaps, not a competing product design.

### Caller usage first

```python
# Proposed domain seam; not implemented.
state, effects = advance_execution(state, ExecutionPlanOffered(contract), now)
state, effects = advance_execution(state, MeterObserved(receipt), now)
state, effects = advance_execution(state, TransportResult(operation_id, result), now)
```

Callers do not separately mutate debt, recovery and command state. Adapters parse
transport/storage types, the transition owns reference admission and account
changes, and asynchronous effects return correlated evidence.

| Owner | Required data and responsibility |
| --- | --- |
| Server planner/contract producer | Canonical reference intervals, typed objective IDs/targets/deadlines, native response semantics, recovery allocation and handover dispositions |
| HA execution transition | Accepted contract, actuals prefix, reconciliation records, objective outcomes, pending effects; derives nominal/recovery requests and replan needs |
| Physical ledger | Gross directional increments, receipt revisions, source/conversion provenance and uncertainty; no economic decisions |
| Native host/adapter | Sole writer per coupled group, durable attempts, scope/permissions, readback, release and crash reconciliation |
| Diagnostics | Derived plan/actual/difference/recovery views and replayable evidence; no second mutable debt total |

Use the specification's `ExecutionContract`, typed `Objective`,
`RecoveryInstruction` and `ExecutionAccount` shapes. Index by stable objective,
reference interval, physical meter stream and actuator group. Missing evidence
remains unknown; permissions do not become delivery targets.

### Delivery order and exit evidence

1. **Build the pure accounting/admission core against deterministic traces.**
   Opening debt/credit, losses, unknown counter intervals, common-time reference
   amendment, incorporated debt, missed deadline, duplicate receipt, late
   correction and restart must reconcile exactly on the declared basis.
   Specify the server/consumer contract alongside this work.
2. **Publish and validate the new server contract end to end.** Reuse final flow
   materialisation; add canonical intervals, objective/recovery generation and
   receipt/disposition handover. Produce generated fixtures consumed by HA,
   including mixed-mode and partial-quarter cases. Round-trip accepted account
   evidence through a real replan response before connecting live selection.
3. **Replace the live battery selector and its continuation-policy dependency.**
   Execute nominal live response plus authorised correction. Preserve and test
   command ownership, current permissions, supply attribution, physical limits,
   pending effects and crash reconciliation. Remove the old economic decision
   path for the migrated scope; do not run two decision owners or silently adapt
   the old policy into new semantics.
4. **Complete diagnosis and battery commissioning.** Export enough
   evidence to replay the account, replace the continuation outlook, establish
   accounting evidence for shared recovery, and exercise it with other real
   loads. Pass all applicable battery
   specification §10 traces, configuration correction tests and real adapter
   commissioning before claiming delivered performance. Other device executors
   remain as specified until explicitly migrated; each later migration needs its
   own service-fulfilment model and acceptance evidence.

A full rewrite of the execution domain is permitted. Reuse should be justified by
smaller coherent ownership and verified behaviour, not sunk implementation cost.

## Verification performed

The following bounded tests passed against the current implementation:

| Suite | Result |
| --- | --- |
| HA `python3.13 -m unittest discover -s tests -p 'test_battery_runtime.py' -q` | 31 passed |
| HA `python3.13 -m unittest discover -s tests -p 'test_energy_ledger.py' -q` | 20 passed |
| HA `python3.13 -m unittest discover -s tests -p 'test_battery_conversion.py' -q` | 7 passed |
| Backend `deno test --no-check --sloppy-imports --allow-read` on `battery-dispatch-projection.test.ts` and `battery-command.test.ts` | 12 passed |

Also performed: the replay arithmetic audit and the in-memory validator probes
described above, and the captured empirical-loss evidence. **70 passing tests establish existing behaviour, including some
behaviour being replaced; they do not certify the new specification.** No product
code, manifest version, device setting or deployment changed for this analysis.

## Review synthesis and remaining risks

Independent Codex and Claude Opus (High effort) reviews inspected both repositories
through the Architect workflow. Both found the nominal flow data, independent
live economic selection, absent deviation/recovery semantics and source-time
meter admission conflict.

**Base candidate: Codex's account-first replacement boundary.** It distinguishes
existing infrastructure from the responsibilities still missing and puts a
complete producer/consumer contract before live replacement. From Claude, retain
the explicit trace showing nominal battery flow fields are not consumed as an
execution obligation, and the existing atomic policy-admission seam as a useful
place to examine handover behaviour. Reuse of that seam is optional.

The synthesis rejects four shortcuts:

- **“The server already computes everything needed.”** Existing energy flows
  support the reference, but recovery recipes and objective dispositions are new
  planner outputs. Marginal value or terminal valuation does not by itself define
  a stable service deadline. Descriptive solar/grid allocations are not proof of
  complete physical routing.
- **Nominal-only live cutover followed by recovery/handover later.** An offline
  nominal executor is a useful intermediate test seam. Production replacement
  must preserve accountability across the lifecycle, even when a particular
  valid contract delegates no discretionary recovery.
- **Deleting timestamp checks as the complete receipt-order fix.** Counter
  interval arithmetic currently assumes ordered source times. Correction and
  uncertainty semantics must be designed and tested with the changed admission
  rule; accepting a sample alone does not make its energy allocation correct.
- **An accounting wrapper around continuation scoring.** This has lower initial
  migration cost but retains two sources of economic purpose and cannot satisfy
  planner authority. Raw command playback has the opposite problem: a shallow
  interface that forces callers to reconstruct target/forecast distinctions and
  recovery policy.

We accept a coordinated server/HA contract change in exchange for one economic
authority and an auditable execution account. We accept new domain and lifecycle
tests in exchange for removing the controller's independent horizon search. No
compatibility adapter or permanent second decision owner is recommended.

The first concrete implementation unit is the pure account/admission transition
with deterministic handover and uncertain-interval traces, alongside its typed
contract. Wire identifiers and module reuse should follow that evidence.

Known engineering risks are the AC/DC and storage-origin boundary, exact reference
rounding, service identity stability, native correction capability, uncertain
meter intervals and late corrections across handover. These require implementation
evidence; they are not missing product decisions that block this assessment.
The supplied replay is not a live-delivery trace. Hardware performance and the
cause of a particular deployed decision remain outside what this source review
and captured planner output can prove.

[H-setup]: ../custom_components/shs_energy/__init__.py
[H-runtime]: ../custom_components/shs_energy/battery_runtime.py
[H-selector]: ../custom_components/shs_energy/battery_execution_policy.py
[H-state]: ../custom_components/shs_energy/home_runtime.py
[H-ledger]: ../custom_components/shs_energy/energy_ledger.py
[H-conversion]: ../custom_components/shs_energy/battery_conversion.py
[H-coordinator]: ../custom_components/shs_energy/coordinator.py
[H-controller]: ../custom_components/shs_energy/controller.py
[H-validator]: ../custom_components/shs_energy/optimisation.py
[H-diagnostics]: ../custom_components/shs_energy/diagnostics.py
[H-modes]: ../custom_components/shs_energy/operating_modes.py
[H-planning]: ../custom_components/shs_energy/planning.py
[H-runtime-test]: ../tests/test_battery_runtime.py
[H-ledger-test]: ../tests/test_energy_ledger.py
[B-plan]: ../../smart-home-solutions-t-by/supabase/functions/_shared/energy-optimisation.ts
[B-dispatch]: ../../smart-home-solutions-t-by/supabase/functions/_shared/dispatch-plan.ts
[B-projection]: ../../smart-home-solutions-t-by/supabase/functions/_shared/battery-dispatch-projection.ts
[B-exchange]: ../../smart-home-solutions-t-by/supabase/functions/_shared/battery-policy-exchange.ts
[B-commands]: ../../smart-home-solutions-t-by/supabase/functions/_shared/battery-command.ts
[B-continuity]: ../../smart-home-solutions-t-by/supabase/functions/_shared/replan-continuity.ts
[B-scope]: ../../smart-home-solutions-t-by/supabase/functions/_shared/operating-scope.ts
[B-ingest]: ../../smart-home-solutions-t-by/supabase/functions/energy-optimisation-ingest/index.ts
