# Device participation and explicit battery supply intent

**Agreed design, 15 September 2026; production command wiring implemented 16 September.** This is the
normative decision for device terminology, configuration ownership, mixed-mode
accounting, chart membership and battery house-supply scope. It supersedes
conflicting four-mode, website inclusion, reviewed/unreviewed and forecast-sized
or rating-wide battery-supply proposals in earlier documents. Those documents
retain dated implementation evidence. See [current rollout status](battery-live-commissioning.md);
implementation in this checkout is not a claim of deployment.

## User-facing ownership

| Decision | Owner and surface | Meaning |
|---|---|---|
| **Included / Excluded** | Integration, **Devices** | Whether a device shares individual data and is eligible for SHS planning and control |
| **Monitoring / Planned** | Website | Whether an Included device remains unshifted background consumption or is modelled and scheduled individually |
| **Verification / Controlling** | Integration, **Schedule** | Whether a Planned device's proposed commands are logged or attempted on real equipment |

Only Planned equipment appears on Schedule. A newly Planned device starts in
Verification. Inclusion permits participation; it does not itself select planning
or grant control. Monitoring devices may still have historical data and forecasts,
but the planner must not shift their consumption.

Remove the local Monitoring/Planning selector options, “Include in the plan”
rows, “reviewed/not reviewed” participation labels and their Website hyperlinks
from Devices and Schedule cards. Website planning choices use Monitoring/Planned,
not Included/Excluded. Keep genuine setup, stale-input, synchronization and
execution-failure information. Internal provenance is not deleted merely because
the confusing participation label disappears.

Verification means proposed optimization commands are logged, not sent. Leaving
Controlling first fences new optimization writes and follows the existing approved
release protocol. Pending release and effects of already-issued commands remain
explicit; a selector change cannot retroactively turn those effects into simulation.

Default inclusion for newly discovered equipment is not decided here. New Planned
admission always defaults to Verification; a dormant controlling grant must never
revive automatically after exclusion, demotion or re-admission.

## Inclusion and data boundary

Excluded devices send no future device-specific readings, profiles, descriptive
metadata, entity bindings or inventory entries, and cannot be planned or controlled
by SHS. This is stronger than the existing readings-only exclusion, which retains
inventory metadata. Removing that metadata is required implementation work.

HA locally revokes excluded participation immediately and fences queued commands.
A versioned complete **Included inventory** retires formerly included membership
through omission; stale website state cannot reintroduce it. Inventory exchange,
planning and effect acknowledgement remain separate responsibilities.

Whole-house totals still include excluded electricity. Shared observations can
still be supplied for other independently Included equipment; this is not an
excluded device's individual series. Inclusion does not promise that aggregate
measurements hide physical consumption. Previously stored history is not silently
purged; retention/deletion remains separate from stopping future sharing.

Monitoring devices send data but contribute to base consumption. Planned devices
are separately accounted for. Missing metering or unsupported planning/control
must be visible as readiness/coverage limitations, not silently interpreted as
zero consumption or additional authority.

## Consumption and net demand

Use one AC boundary and aligned measurement times. Home-battery charge/discharge
is a separate storage flow, not an appliance consumption band.

| Quantity | Definition |
|---|---|
| House consumption `H` | Gross non-battery household consumption before solar |
| Planned consumption `P` | Sum of all individually Planned appliances, in either Verification or Controlling |
| Base consumption / chart **Base load** `B` | `H − P`: Monitoring, Excluded and unattributed consumption |
| Net house demand `N` | `H − PV` |
| Net base demand | `B − PV` |
| External demand for live planning `U` | `B` plus Planned consumption not under effective physical control |

The user's solar-subtracted “total house load” and “base load” correspond to the
net quantities above. Always say **net** when solar has been subtracted; do not
reuse a gross-load field for signed net demand. Net demand may be negative.
Subtract PV once in each whole-house balance, not independently from every band.

`H` and its disjoint device components must reconcile. Duplicate meters, shared
parents/children and stale intervals cannot be subtracted twice. A negative gross
remainder is a measurement/accounting inconsistency, not negative appliance use;
report the inconsistency rather than silently clamping it into apparent accuracy.

## Consumption graph

Grey means gross Base load `B`, before solar. Every Planned device belongs in the
Planned portion independently of its energy magnitude or control mode. Small
Planned devices and devices omitted by the colour/band limit may be grouped as
**Other planned devices**, never silently folded into grey. A zero-power planned
device need not draw a visible area, but its membership remains Planned.

The existing graph folds small or excess Planned series into grey through display
thresholds and an eight-band cap. Excluded-device metadata is a separate inventory
problem; removing it does not remove these display rules. Both must be corrected.
Monitoring and Excluded consumption remains within the whole-house/base total.
PV and battery flows remain separate from the positive consumption stack.

Historical plots use the recorded participation basis for their intervals, or are
explicitly labelled a reclassification experiment. Changing today's roles must not
silently rewrite the provenance of an old plan or measured comparison.

## Mixed-mode physical scope

A Verification device is Planned for an individual hypothetical schedule and chart,
but is **external demand** for live execution. Its proposed start/stop or setpoint
cannot count as delivered energy, removed load or released headroom. The same
applies to requested control whose physical authority has not become effective.

Example: base consumption 1 kW, a Verification pool pump drawing 2 kW, a Controlling
EV drawing 0.8 kW and solar 0.4 kW gives external consumption 3 kW and actual net
house demand 3.4 kW. Logging “stop pump” does not subtract its actual 2 kW.

The command interface contains only Planned equipment. The measurement/accounting
interface still sees the whole household. The hypothetical plan may optimize all
Planned devices; the executable projection may vary only effectively controlled
physical groups. External current demand uses measurements where valid; future
external demand needs forecasts. No mode name predicts an appliance's run duration.

Base consumption and external demand are distinct concepts. A legacy numerical
base-load field may carry their sum inside an adapter, but public terminology,
provenance and new contracts must preserve the distinction and count it once.

## Explicit battery house-supply scope

**Product decision:** the planner must communicate which demand the battery is
permitted to offset, not only estimated watts. This accounting policy is now in
scope, superseding the earlier review's suggestion to defer it as unnecessary.

Supported semantic forms are:

| Scope | Eligible gross consumption |
|---|---|
| **None** | No house demand |
| **Whole house** | All current non-battery household consumption |
| **Base load only** | Current base consumption |
| **Selected Planned devices** | Current consumption of an explicit set of Planned devices |
| **Base load + selected Planned devices** | Base plus that explicit set |

These forms describe one canonical selector, not overlapping permission flags.
Whole house follows the revision-bound household membership, not a frozen list of
visible chart bands. A selected Planned device may be in Verification: its actual
consumption can be eligible even though its hypothetical schedule is not executable.
Monitoring or Excluded devices cannot be individually selected; they remain in base.
Physical-owner mappings prevent double counting shared equipment.

On a shared electrical bus, scope limits the **amount** of battery power credited
to eligible consumption. It does not physically route electrons to selected devices
or prove their measured source. Explain scope as “battery may offset …”. No new
circuit-routing hardware or device priority order is assumed.

A scope is permission, not an instruction to cover its entire demand. The economic
policy still decides whether and how much to discharge now, considering stored
energy, future prices, future demand, replenishment, wear, losses and physical
constraints. Zero forecast discharge does not automatically mean scope None; an
explicit scope and the evaluated current action are different facts.

### Solar allocation within a selected scope

The contract must declare how solar is attributed between eligible and ineligible
consumption. This is an accounting rule, distinct from measured electrical routing.
Historical proportional cost attribution does not silently become a control rule.
Do not independently grant the full PV amount to every device or scope.

**Settled: solar is shared proportionally across gross household consumption.**
This applies to control-scope accounting as well as its explanation. Each disjoint
component receives the same fraction of self-consumed solar as its fraction of
current gross consumption, whether Planned, Monitoring, Verification or Controlling.
Excluded/unattributed consumption participates through the aggregate base component;
no excluded-device identity or individual measurement is required.

For eligible gross consumption `L_s`, gross house consumption `H`, and AC PV:

```text
self_consumed_pv = min(PV, H)
if H > 0:
    attributed_scope_pv = self_consumed_pv * L_s / H
    eligible_deficit = L_s - attributed_scope_pv
else:
    attributed_scope_pv = 0
    eligible_deficit = 0
house_supply_bound = min(eligible_deficit, max(0, H - PV))
```

Inputs must satisfy the reconciled physical partition `0 <= L_s <= H`, with
nonnegative gross consumption and PV. The zero-consumption case is a defined
physical case, not a missing-data fallback. Unreconciled or unavailable values
remain invalid/uncovered. Numerical tolerances must not conceal real mismatch.

For base 1 kW, other demand 2 kW and solar 1 kW, base receives 1/3 kW of solar;
**base-only scope permits up to 2/3 kW (about 0.67 kW) of house supply**. The
remaining solar is attributed to other demand. The economic policy may select
less battery power. Do not subtract all solar from base first or allocate it
again independently to each selected device.

PV attribution over all disjoint consumption components sums to self-consumed PV.
PV beyond gross demand is a separate surplus flow; it is not attributed as
negative consumption. Whole-house scope reduces to `max(0, H - PV)`, None gives
zero, and a sum of selected components uses their combined proportional share.
These bounds are not an economic optimum, a forecast allocation or a rated-power
request. The convention governs allowed accounting amounts, not measured electron
routing; observed household import/export and storage remain the physical truth.

Source permissions for grid charging and destination permission for battery export
remain separate. Scope None does not implicitly grant export. A forced operation
that also supplies house demand must respect the house-supply scope; reject an
incompatible native route rather than bypassing scope by labelling it export.

### Current measurements and future economics

Calculate current eligible demand from fresh, aligned physical measurements and
current participation, not the planner's predicted watts. Gross house/PV readings,
or signed grid exchange plus AC battery discharge minus AC battery charge, can
establish aggregate net demand. Individual scope decomposition additionally needs
suitable non-overlapping device meters.

Energy-counter differences yield interval averages, not exact instantaneous power.
Record sources, timestamps, interval/basis and uncertainty. Some houses support
whole-house control before a reliable base/selected-device split is available.
Unknown or stale required inputs must produce explicit unavailable coverage; do not
substitute zero, the old forecast, all-house scope or rated discharge as a fallback.
Use the existing explicit coverage/ownership/release protocol.

The existing current-response model must account for physical native behaviour.
A scope below whole-house demand is an accounting cap that the ordinary inverter
cannot identify itself. The host/adapter must update and enforce it with declared
freshness, latency, pending-effect and physical-response evidence. Do not assert
continuous hard enforcement from a periodic HA sample; unsupported precision or
native routing must be rejected or visibly uncommissioned.

Feed the measured state and scoped feasible response into the existing remaining-
interval `C + V` evaluation. Future economics remains server-owned and current
operation selection remains with one HA policy owner. No second local optimizer,
price threshold, fixed slot energy allowance or recovery-to-forecast-SOC overlay is
introduced. Each compared action must retain its valid future consequences.

## Contract and ownership sketch

This is a semantic sketch, **not a new deployed wire version**:

```typescript
type Participant =
  | { kind: "excluded"; key: DeviceKey }
  | { kind: "monitoring"; key: DeviceKey }
  | { kind: "planned"; key: DeviceKey; physicalOwner: OwnerKey;
      requestedExecution: "verification" | "controlling" };

type BatterySupplyScope =
  | { kind: "none" }
  | { kind: "whole_house" }
  | { kind: "selected"; includeBase: boolean; plannedDeviceKeys: DeviceKey[] };
// Selected keys are unique and canonical. Empty/no-base normalizes to None.
// All membership, physical-owner, planning and authority revisions are bound.
// Solar attribution: mandatory versioned proportional share of self-consumed PV.
```

HA owns inclusion, local mappings, requested/effective authority and live evidence.
The website owns planning roles and household intent. The server emits supply scope
and a bounded economic policy for that acknowledged participation. One derived
participation contract is consumed by snapshots, displays, policy acceptance and
final dispatch; callers do not independently recombine flags.

Any scope, role, inclusion, mapping or authority change invalidates dependent unsent
commands and requires a correctly identified policy. Stale website state cannot
restore local authority. Shared EV/pool/thermal controls need coherent physical-owner
groups; conflicts are explicit, not silent mode changes. Already-issued effects and
approved release remain accounted for during exclusions and transitions.

The scope identifier/revision participates in policy, plan, observation and cost
identity. Diagnostics show chosen scope, member roles, solar attribution, measured
eligible/whole-house deficits, measurement basis, selected action, economic reason,
actual power and any coverage/response limit. Scope is not inferred from numbers.

## Supersession and implementation sequence

The v35 “full forecast residual implies rated discharge” change and corresponding
beta.94 validator support are dated implementation facts, **not the target policy**.
Do not automatically widen supply to the rating in the replacement. Rated power
remains a physical maximum. Merely reverting to forecast-sized ceilings would
reintroduce the original tracking defect; replacement needs explicit scope, live
measurements, economic evaluation and a commissioned enforcement path together.

1. Encode the settled proportional solar rule and participation contract; record
   the still-open default inclusion of newly discovered equipment without inventing
   a new default in this documentation change.
2. Implement the participation contract, synchronization, inclusion boundary and
   one-way migration without accidentally promoting equipment to Controlling.
3. Update Devices/website/Schedule language and chart membership from that contract.
4. Extend policy compilation, native response, acceptance and observations with
   explicit scoped house supply; do not create a parallel battery writer.
5. Validate provider/consumer schema, fixtures and adverse event/measurement traces;
   coordinate rollout before enabling the replacement. No runtime change, version
   bump, deployment, hardware enablement or automatic revert occurs in this doc pass.

## Required acceptance evidence

- Included/Excluded wins locally over stale website roles; excluded payloads contain
  no device-specific inventory or telemetry. Whole-house energy still reconciles.
- Newly Planned and re-admitted devices default to Verification; pending release,
  queued writes, restarts and shared-owner conflicts cannot create hidden authority.
- Grey is base consumption; small Planned devices and >8 Planned devices remain in
  the Planned portion. Solar surplus and changing display range do not change roles.
- Verification schedules do not remove actual demand or invent battery headroom;
  physical non-delivery under requested control is handled honestly.
- None, whole house, base only, selected only and base+selected scopes are tested
  against the same measured state, including zero eligible demand and surplus PV.
- Scope membership/attribution changes invalidate stale commands; only suitable
  observations can authorize subgroup calculations. Test missing/stale/overlapping
  meters, averages versus instant power, pending native writes and sudden new loads.
- House supply cannot exceed eligible or total physical deficit, enable unintended
  export, or bypass scope through forced operation. Enforcement claims match actual
  adapter timing/capabilities, with explicit unsupported cases.
- Replay the September 15 406 W case and a contrary case where retaining energy for
  later is better. Compare full cost and ending energy; merely increasing discharge
  or explaining scope more clearly does not prove the economic decision is correct.

Related specifications: [actual-state opportunity cost](battery-opportunity-cost.md),
[battery runtime and native response](battery-execution-design.md), and
[backend contract companion](https://github.com/SHS-se/smart-home-solutions/blob/dev/docs/energy-optimisation/device-participation-and-battery-supply.md).

## Sigen conversion and command connection — 16 September 2026

The household controller now binds this intent to real Sigen PV First commands.
ESS limits and battery power are DC; grid and house measurements are AC. Field
roles establish ownership/signs and individual HA timestamps establish freshness.
No extra shared-HA-device or manual meter-certification step is required.

Directional conversion uses stable history to separate fixed overhead from gain.
Current and future scoring share the same versioned curves. Insufficient charge
observations retain labelled configured assumptions. Solar residuals do not prove
pure DC efficiency; separate PV-to-house conversion remains unmodelled in this
version. The [live guide](battery-live-commissioning.md) states these limits and the
actual deployment/test sequence, including cloud-independent command recovery.
