# Battery execution policy and mixed-mode ownership

> **Superseded controller design — 17 September 2026.**
> [Plan execution and deviation accounting](controller-plan-execution.md) replaces
> the economic selector, continuation-policy requirement and decision architecture
> below. This document remains historical design/implementation evidence. Physical
> command, authority and reconciliation requirements survive where consistent with
> the replacement; retaining their current implementation is not required.

## Required scope extension — 15 September 2026

Extend the existing compiler, current-response evaluator and sole-writer host/adapter with revision-bound explicit battery supply scope and its solar-attribution convention. The current software policy below does not yet carry that extension. Feed aligned measured gross demand and PV separately; enforce eligible house supply and physical native routing while retaining remaining-time C + V ranking. Derive mixed-mode membership from HA inclusion, website planning and HA authority. The plan is the same in every mode; a Verification device's requests are logged, not sent, so execution treats its measured draw as uncontrolled demand ([authoritative plan contract](authoritative-plan-contract.md)). The v35 rating-wide shortcut is superseded as target policy.

See the [agreed participation and battery supply specification](device-participation-and-battery-supply.md).
Documentation only; replacement implementation and coordinated rollout remain pending.

Architecture decision, 15 September 2026. Implementation is authorised for the
software compiler, evaluator and command-reconciliation protocol. The existing
ScheduledController remains the live owner until a separately tested host and
adapter cutover. This document does not authorise hardware enablement.

## Problem and synthesis decision

The prior exact-anchor policy expires after one millisecond and binds every
observation revision. It cannot become usable by lengthening that lease. We need
current time/SOC/PV/load evaluation, future economic consequences, exclusive SHS
battery ownership, and real external demand when other devices are Planning or
Verification.

Independent Codex and Claude Opus Max candidates both rejected a four-dimensional
C/F interpolation table. We select Codex's analytic current response plus a finite
family of feasible bridge/suffix continuation functions. Unlike Claude's proposed
one-dimensional interpolated boundary values, this retains import ramp coupling
and explicit continuous feasibility domains without treating sampled error as a
certificate. The guarantee is exact scoring within the published family, not a
continuous optimum. Claude's operation-level safety envelopes, derived lifecycle
status, response parity tests, and distinction between ephemeral observations and
durable changes are compatible and adopted.

We reject coverage grace for forced operation outside its applicable policy,
implicit PV curtailment, floor-to-millisecond saturation, and an exclusivity
boolean without a fenced grant. No hypothetical service delivery or planned stop
may alter the executable external scenario. Thermal models, direct user controls,
and notifications remain deferred.

## Caller usage

```python
policy = read_execution_policy(provider_bytes)
home, effects = reduce_home(home, PolicyOffered(policy, real_watermark), now_ms)
home, effects = reduce_home(home, ConditionsObserved(real_conditions), now_ms)
# One policy owner selects a semantic operation; _drive alone prepares and sends.
# Every Send names the writer grant; the future port rechecks it at dispatch.
```

```typescript
const result = compileBatteryExecutionPolicy({
  problem: resolvedBatteryAndExternalLoads,
  identity, validity, domain, reference_id, operations, permissions, projection, search,
});
// Bounded result or explicit rejection. No native service calls or HA-side solve.
```

## Implementation decisions and acceptance

- Source intervals: one remaining current interval and complete future UTC quarters;
  reject unsupported subdivision. Each execution window ends at its next quarter.
  Absolute validity and early refresh are explicit; no energy allowance exists.
- Current physical saturation uses positive floating-hour durations, including a
  second segment only when it has positive duration. Current C is checked against
  a shared duration-based scorer kernel with generated cross-language vectors.
- Future bridge-to-anchor cells reuse the existing optimiser. The fixed suffix's
  original first ramp is removed and replaced by bridge-to-suffix ramp. The
  current-to-bridge ramp depends on current terminal import. Terminal utility is
  retained; an empty terminal account is explicit. A normally ranked all-idle
  future family provides continuous SOC coverage where idle is physically feasible.
  If the first future quarter needs charging/discharging to respect grid limits,
  the search seed supplies that minimum action and lands at the chosen anchor.
  Every published witness and
  entire bridge domain must satisfy native source/permission/reserve constraints.
- No normal forced charge uses Grid First. No execution witness in this first
  profile contains PV curtailment. Solar-only charging follows surplus and house
  supply follows net deficit; forced operations have explicit source permissions.
- A local validated catalog binds semantic operations to complete native targets,
  exact quantum, response evidence and model/config identity. Synthetic evidence
  is labelled and cannot be used as commissioning evidence.
- Other participants are external real demand. External evidence must cover their
  actual and unresolved possible effects once. Logged Verification requests are
  never sent and cannot count as delivered.
- One durable grant names the SHS owner, epoch and control/config identity.
  Requested mode, release-contract revision and grant confirmation are separate.
  Every local mode and mode revision is bound into the execution authority. A
  mode change rebinds that authority (release or take-over) but keeps the accepted
  plan and battery reference: they are mode-independent, and a reference captured
  under either mode is accepted under the other (23 September 2026).
  Expired observations of execution conditions withdraw the active operation.
  Entry, exit, release failure, rapid re-entry and restart derive effective status
  from these facts. A mode selector alone never establishes exclusivity.
- The single policy session owns only the battery's desired request. Direct
  requests cannot bypass it during expiry or missing coverage. Generic independent
  groups retain their existing request path. Identical native targets renew without
  changing generation or extending an old preparation's persisted send window.
- Restart retains a still-valid operation while awaiting fresh context and grant,
  without an artificial baseline cycle. Already issued effects always survive.
  Missing coverage fences new optimisation; expiry uses only approved release.
- A delayed policy replacement reconciles at its source watermark, not arrival
  time. Reject a cut older than already-settled prefixes. Meter actuals remain
  independent of predicted costs and are never reset or counted as an allowance.
- New checkpoint schema 5 rejects schema 4; no compatibility execution path.
  The old reader/compiler remains explicitly offline analytical tooling, with
  its useful scoring tests. Its active exact-anchor runtime path is replaced.
- Required tests include multi-minute live-condition changes, state saturation,
  negative prices, future ramp parity, export reserve, real 7kW EV load despite a
  hypothetical stop, same-target renewal, stale durability acknowledgements,
  ownership denial/revocation, failed release/re-entry/restart, delayed policy
  receipt/accounting, malformed payloads and declared maximum bounds.

We accept finite-family approximation and a quarter-bounded first execution
window in exchange for bounded work and explicit coverage. Representative compile
latency, coverage and economic regret remain deployment evidence to collect;
no sampled error is advertised as a certified regret bound. Live ports, final
legacy-write exclusion, native transitions and outage commissioning remain the
next stages before battery control can be enabled.

## Exact wire shape

All objects are closed; all numbers finite; timestamps integer UTC milliseconds; powers W, stored energy kWh, money SEK. No implicit defaults in wire parsing. Numbers below illustrate one structurally valid cell, not evidence from a real solve. The object has exactly these fields:

```json
{
  "schema": "battery-execution-policy-v1",
  "profile": "finite-continuation-v1",
  "view": "executable",
  "identity": {
    "policy_id": "policy-31", "revision": 31, "battery_id": "battery",
    "intent_revision": "intent-8", "plant_revision": "plant-2",
    "scope_revision": "scope-4", "external_scenario_revision": "external-7",
    "tariff_revision": "tariff-5", "response_model_revision": "pv-first-v1",
    "catalog_revision": "catalog-3"
  },
  "actuals_origin_ms": 1790013600000,
  "validity": {
    "from_ms": 1790013600000, "refresh_after_ms": 1790014440000,
    "until_ms": 1790014500000, "boundary_ms": 1790014500000
  },
  "domain": {
    "energy_kwh": [1.0, 14.0], "pv_w": [0.0, 12000.0],
    "residual_load_w": [0.0, 15000.0]
  },
  "plant": {
    "cutoff_kwh": 1.0, "capacity_kwh": 14.0,
    "charge_max_w": 8800.0, "discharge_max_w": 9600.0,
    "charge_efficiency": 0.95, "discharge_efficiency": 0.95,
    "import_limit_w": 17000.0, "export_limit_w": 10000.0,
    "wear_sek_per_kwh": 0.1
  },
  "permissions": {
    "available": true, "grid_charge_allowed": true,
    "battery_export_allowed": true,
    "export_reserve_kwh": 4.0,
    "export_price_eligible": true, "minimum_export_price_sek_per_kwh": 0.25,
    "price_revision": "tariff-5"
  },
  "economics": {
    "import_sek_per_kwh": 0.5, "export_sek_per_kwh": 0.3,
    "shaping_sek_per_kwh_per_kw": 0.0, "ramp_sek_per_kw": 0.0
  },
  "reference_id": "hold",
  "operations": [
    {"id": "hold", "operation": "hold", "charge_limit_w": 0.0, "discharge_limit_w": 0.0},
    {"id": "charge-3000", "operation": "grid_charge", "charge_limit_w": 3000.0, "discharge_limit_w": 0.0},
    {"id": "export-2000", "operation": "export", "charge_limit_w": 0.0, "discharge_limit_w": 2000.0}
  ],
  "continuation": {
    "representation": "piecewise-quadratic-absolute-v1",
    "coordinate_order": ["energy_kwh", "previous_import_w"],
    "cells": [
      {
        "id": "tail-0-charge-domain", "witness_id": "witness-17",
        "domain": {
          "energy_kwh": [1.0, 14.0], "previous_import_w": [0.0, 17000.0],
          "inequalities": []
        },
        "cost": {
          "import_sek": {"polynomial": [0.0, 0.0, 0.0], "absolute_terms": []},
          "export_sek": {"polynomial": [0.0, 0.0, 0.0], "absolute_terms": []},
          "wear_sek": {"polynomial": [0.0, 0.0, 0.0], "absolute_terms": []},
          "shaping_sek": {"polynomial": [0.0, 0.0, 0.0], "absolute_terms": []},
          "ramp_sek": {"polynomial": [0.0, 0.0, 0.0], "absolute_terms": []},
          "terminal_sek": {"polynomial": [0.0, 0.0, 0.0], "absolute_terms": []}
        }
      }
    ]
  },
  "quality": {
    "assurance": "exact-scoring-within-published-family",
    "scorer_revision": "household-scorer-v1", "compiler_revision": "execution-v1",
    "family_id": "family-17", "source_hash": "sha256:0000000000000000000000000000000000000000000000000000000000000000",
    "numeric_tolerance_sek": 0.0000001,
    "search_exhaustive_in_declared_graph": true,
    "search_pruned_prefixes": 0,
    "heldout_count": 40, "heldout_max_regret_sek": 0.03,
    "certified_regret_bound_sek": null
  }
}
```

`polynomial=[constant, E_coefficient, E_squared_coefficient]` evaluates `c+bE+aE²`. An absolute term is exactly `{ "weight": number, "energy": number, "previous_import": number, "constant": number }`, evaluated as `weight*abs(energy*E+previous_import*P+constant)`. Only ramp may contain absolute terms, at most two/cell; nonnegative weights. Examples include first-future-interval ramp from current final import and next-future-interval ramp from bridge import. Units are embodied in generated coefficients, checked by cross-language fixtures; input P is W, never silently kW.

Each inequality is exactly `{ "energy": number, "previous_import": number, "maximum": number }`, meaning `aE+bP<=maximum`; at most eight/cell. All domains are closed. Overlap is allowed and evaluator selects minimum total cost, stable cell-ID tie within 1e-7 SEK. No domain extrapolation. Current policy validity is half-open `[from,until)`. Plant cutoff is strictly below capacity; reserve is within that range. All identity strings <=128 characters.

Objective wire coefficients omit always-zero `starts/service` and derived `billable/total`. Python builds the existing complete Objective representation: billable=import-export, total=billable+wear+shaping+ramp-terminal. Source service obligations remain unsupported. Terminal utility is explicitly included: an anchored fixed suffix has a constant terminal term derived by the existing scorer. Publish zero only when the source has no terminal utility. If no fixed suffix remains, the compiler must represent the supported terminal utility as exact endpoint cells or reject that scope; do not replace nonzero terminal utility with zero. Import/export amounts may be negative because prices can be negative; wear/shaping/ramp must be nonnegative over each accepted domain, established by compiler and checked at extrema by reader.

At most 64 published cells after all direction/grid/terminal splits, 12 operations and 128,000 serialized bytes; the existing 1 MB checkpoint cap remains. Absolute ramp terms retain their kinks without extra cells. Aggregate compiler work is bounded to 40 million scored intervals. Parser rejects duplicate keys/IDs, unknown fields, future unsupported assurance tags and wrong coordinate ordering. Policies cannot self-authorize a native response model: the local catalog revision and independently supplied expected plant/scope/permissions must match.

## Local semantic operation and native mapping

The policy operation contains **ceilings**, not imposed physical watts. The local native catalog owns complete target controls, quantum/bounds and commissioned model identity:

| Semantic operation | Established native mode | Target charge limit | Target discharge limit |
|---|---|---|---|
| self_consumption | Maximum Self Consumption | rated | rated |
| solar_charge | Maximum Self Consumption | rated/configured ceiling | 0 |
| supply_house | Maximum Self Consumption | 0 | selected ceiling |
| grid_charge | Command Charging (PV First) | selected total ceiling | 0 |
| export | Command Discharging (PV First) | 0 | selected ceiling |
| hold | Standby | 0 | 0 |

Require a local binding for every operation. Exclude Command Charging (Grid First), implicit PV curtailment and forced export for `supply_house`. Targets must already lie on the local actuator quantum; reject mismatches so scoring and issued ceiling cannot diverge through silent rounding. Catalog matching uses exact supplied surface keys/options, not hardcoded generic HA entity names.

Installation evidence, 15 September: Phil confirms that Standby stops battery
charge/discharge while PV supplies the house and exports the remainder. In
`history (17).csv`, Standby was selected at 08:43:30.534 UTC and self-consumption
restored at 08:45:48.458 UTC. During the settled Standby period PV remained
2.438–2.564 kW, with 2.079–2.244 kW export. This resolves the concern that Standby
necessarily suppresses PV on this installation. The CSV has no battery-power
channel; zero battery flow is the user's direct observation. Charge/discharge
registers remained 8.8/9.6 kW, so this is evidence of mode behaviour, not a replay
of the new adapter's complete zero-ceiling transition sequence.

Physical automatic saturation is only at configured capacity/cutoff. `export_reserve_kwh` is an **economic/source permission guard**, distinct from cutoff. An export alternative is ineligible if export permission or price eligibility is false, observed export tariff is below the configured minimum, state is at/below reserve, or the predicted response crosses reserve before the boundary. Do not shorten an export segment at reserve and assume the inverter stops there; without a commissioned native reserve register, that would fabricate an actuator capability. House supply may consume below export reserve down to physical cutoff.

Because export reserve is also a live prohibition, local conditions independently include current permissions/reserve/eligible price identity. Reject policy acceptance if these disagree with the current configured authority. Runtime send guards must recheck current reserve and prices. A physical observation outside the predicted export envelope forces reselection/release; future host latency and commissioning remain required to establish real reserve protection. A short HA timer alone is not proof of a hard hardware reserve floor.

## Python public domain interface

Reuse `Controls`, `Envelope`, `Guard`, `ActualsWatermark`, `StreamActuals`, `SettledActuals`, and existing runtime codec. The following are frozen domain dataclasses; transport parsing creates them once.

```python
@dataclass(frozen=True)
class ContextIdentity:
    battery_id: str
    intent_revision: str
    plant_revision: str
    scope_revision: str
    external_scenario_revision: str
    tariff_revision: str
    response_model_revision: str
    catalog_revision: str

@dataclass(frozen=True)
class PolicyIdentity:
    policy_id: str
    revision: int
    context: ContextIdentity

@dataclass(frozen=True)
class BatteryOperation:
    id: str
    operation: Literal['self_consumption','solar_charge','supply_house',
                       'grid_charge','export','hold']
    charge_limit_w: float
    discharge_limit_w: float

@dataclass(frozen=True)
class Permissions:
    available: bool
    grid_charge_allowed: bool
    battery_export_allowed: bool
    export_reserve_kwh: float
    export_price_eligible: bool
    minimum_export_price_sek_per_kwh: float
    price_revision: str

@dataclass(frozen=True)
class OperationBinding:
    operation: BatteryOperation
    target: Controls
    response_model_revision: str
    response_evidence: str
    native_guards: tuple[Guard, ...]

@dataclass(frozen=True)
class NativeCatalog:
    revision: str
    adapter_revision: str
    control_surface_revision: str
    mode_key: str
    charge_key: str
    discharge_key: str
    mode_options: tuple[str, ...]
    quantum_w: float
    charge_max_w: float
    discharge_max_w: float
    bindings: tuple[OperationBinding, ...]

@dataclass(frozen=True)
class ExecutionConditions:
    revision: int
    at_ms: int
    valid_until_ms: int
    energy_kwh: float
    pv_w: float
    residual_load_w: float
    previous_import_w: float
    identity: ContextIdentity
    permissions: Permissions
    # Actual physical frame stays home state; no duplicated accounting envelope.

@dataclass(frozen=True)
class CurrentResponse:
    current: Objective
    energy_end_kwh: float
    terminal_import_w: float
    possible_import_w: float
    possible_export_w: float
    # internal bounded segments are not part of caller coordination

@dataclass(frozen=True)
class RankedOperation:
    operation: BatteryOperation
    current: Objective
    continuation: Objective
    future_delta: Objective
    full: Objective
    total_delta_sek: float
    witness_id: str
    possible_import_w: float
    possible_export_w: float
    energy_end_kwh: float
    terminal_import_w: float

@dataclass(frozen=True)
class Decision:
    evaluated_at_ms: int
    valid_until_ms: int
    reference_id: str
    ranked: tuple[RankedOperation, ...]
    selected_id: str
    refresh_due: bool
    excluded: tuple[tuple[str, str], ...]

@dataclass(frozen=True)
class OutsideCoverage:
    reason: str
    refresh_required: bool

@dataclass(frozen=True)
class ExecutionPolicy:
    source_json: str
    # Validated immutable source; cached .summary contains PolicySummary:
    # identity, actuals_origin_ms, from/refresh/until/boundary_ms, domain,
    # plant, permissions, economics, reference_id, operations, cells, quality.

def read_execution_policy(data: bytes | str) -> ExecutionPolicy: ...

def evaluate_current(policy: ExecutionPolicy, operation: BatteryOperation,
                     conditions: ExecutionConditions, now_ms: int
                     ) -> CurrentResponse | OutsideCoverage: ...

def evaluate_continuation(policy: ExecutionPolicy, energy_kwh: float,
                          previous_import_w: float
                          ) -> tuple[str, Objective] | None: ...

def evaluate_policy(policy: ExecutionPolicy, conditions: ExecutionConditions,
                    now_ms: int, incumbent_id: str | None = None,
                    deadband_sek: float = .02) -> Decision | OutsideCoverage: ...

```

The reader normalizes the flat wire identity into policy identity plus context identity. Runtime comparisons use exact context identity. No caller accesses continuation coefficient layout. `evaluate_policy` returns scores and semantic choice; it never mutates a HomeState, calls a device or settles a meter.

`CurrentResponse` enforces native model semantics with remaining-time saturation at physical bounds. Integrate segment durations in floating-point hours: a saturation instant can fall between integer milliseconds, and must not be rounded through Date/timestamp conversion. Source accounting follows the scorer: solar attribution is min(charge, PV); the `solar_charge` operation only responds to surplus after household load. Forced export represents total commanded discharge; `supply_house` responds only to net deficit. Use the current interval scorer kernel in backend and cross-language vectors in Python. If a current operation is infeasible, omit it with an explicit reason in diagnostics; do not reject an otherwise valid policy merely because one optional operation lacks current headroom. Reference has to be mathematically evaluable; physical dispatch eligibility remains separately checked.

Let `V_a` be the continuation Objective at operation a's endpoint. For the one common reference r: `F_a=V_a-V_r`, `J_a=C_a+V_a`, and `delta_a=C_a-C_r+F_a=J_a-J_r`. Thus scores preserve current C/F/J semantics rather than introducing an opportunity-price heuristic.

## Home runtime ownership/state

```python
@dataclass(frozen=True)
class PolicySession:
    compiled: ExecutionPolicy
    watermark: ActualsWatermark
    settled_actuals: tuple[SettledActuals, ...]
    reconciled_from: ActualsWatermark | None
    reconciled_actuals: tuple[StreamActuals, ...]
    decision: Decision | None
    status: Literal['active','diagnostic_only','outside_coverage','awaiting_context']
    request_id: str | None  # stable execution identity; not policy revision

@dataclass(frozen=True)
class WriterGrant:
    owner_id: str
    epoch: int
    config_revision: str
    control_surface_revision: str
    expires_at_ms: int

@dataclass(frozen=True)
class ScopeParticipant:
    group_id: str
    mode: Mode
    mode_revision: int
    owner: Literal['new_runtime','legacy','external','none']
    control_surface_ids: tuple[str, ...]

@dataclass(frozen=True)
class ExecutionScope:
    revision: str
    battery_group_id: str
    participants: tuple[ScopeParticipant, ...]
```

`HomeState` holds a single `policy`, optional fresh `conditions`, and a versioned `authority` containing the configured identity, plant, permissions, local catalog, scope and owner ID. Group adds explicit lifecycle/grant state; retain `mode_revision`, generation, attempts and existing approved release Request. Ledger remains exactly one owner in home runtime.

Reuse `PolicyOffered` event name with the new wire domain and watermark; remove its old synthetic bindings field because the catalog is local state. Replace `PolicyContextChanged` with `ConditionsObserved`; `AuthorityInstalled(authority, revision)` installs scope/catalog/config in one versioned event so callers cannot transiently combine unrelated authority identities. `AuthorityChanged` is the requested-mode update, with grant confirmation separate because a dropdown cannot prove exclusivity.

`_accept_policy` validates identity/watermark/reader, settles previous actuals once and installs the single session. `_refresh_policy_decision` reevaluates conditions, resolves chosen local target, and alone updates the battery desired request before `_drive`. Other groups use their existing request owner. Direct requests cannot bypass the battery policy when it is inactive/expired/outside coverage; ownership is scope-based.

Stable same-operation policy renewals preserve request ID/revision, generation and in-flight attempt identity; updated economic policy revision remains on session. A renewed expiry never changes an already prepared attempt's `send_by_ms` or persisted authorization proof. Old durable ack may only authorize within the old proof's original bounds. Changed target/model/grant/catalog increments execution revision and fences unsent preparation while preserving issued uncertainty. `_drive` retains current frame/guard checks both before preparation and after persistence.

Durable grant verification and final-send epoch validation are mandatory host-port contracts. This deliverable defines/replays them; no assertion that an unmodified legacy writer is already excluded. On real integration, the final write boundary for both owners must obey the same grant arbiter.

## Implementation split

The current future solver may add minimum required PV curtailment even when requested curtailment fractions are [0]. Explicitly filter/reject every continuation witness containing curtailment in this execution profile; requested fractions alone do not prove no curtailment.

1. Backend: extract reusable single-interval battery physics/scoring as needed; compile optimized fixed suffixes using current bounded search; derive bridge domain/cost functions; score breakpoints/random interiors against authoritative scorer; emit the closed 128,000-byte/64-cell contract and rejection results.
2. Python evaluator: parse coefficients/identities, evaluate native current response at elapsed time/live PV/load/SOC, evaluate bounded continuation functions and rank C/F/J. Generated fixtures are the cross-language seam.
3. Home runtime: install new sole session, conditions and catalog; stable intent renewal; lifecycle/grant state; reserve/permission and physical-envelope rechecks; accounting reuse; schema5 strict checkpoint and replay update.
4. Later host/adapter: gather trustworthy conditions, deliver/persist effects, enforce grant at all final writers, native transitions/readback and commissioning. No live control enabled by this software stage.

## Implementation outcome

The compiler, Python evaluator, schema-5 runtime session and ownership protocol
are implemented. Runtime catalog validation is deliberately local to the runtime,
so pure economic evaluation cannot authorise a native target. Conditions and
derived diagnostics do not journal by themselves. Send authorization rechecks the
grant, original preparation window, active scope, native guards and live physical
admission after persistence. An expired condition withdraws optimisation; a
restart with no new observations waits for fresh evidence before adopting a still
valid target. Rapid re-entry with a fresh scope/policy cancels an unissued release,
while an issued release remains in the unresolved inventory until reconciled.

The compatible refinement to the selected design is the explicit idle continuation
family and physically necessary seed action described above. Idle competes under
the same objective and feasibility checks as every other family; it is not an
error-recovery path. This closes ordinary low-SOC coverage holes without adding an
HA optimiser or inventing a continuous optimality guarantee. Other finite-family
holes remain explicit. The old offline compiler's generated examples remain
byte-identical after the shared scorer extraction.

Validation commands and the remaining live rollout gates are recorded in
[the controller review](controller-architecture-review.md). No deployment or
battery enablement forms part of this implementation.

## Charge timing from actual state

The [charge-now versus wait adjustment](battery-opportunity-cost.md) makes the
existing C/F/J comparison an explicit acceptance requirement. Compare useful
charge amounts and waiting from fresh energy over the remaining segment, each
with its own conditional future. A forecast SOC shortfall is evidence for
re-evaluation, not an obligation to catch up. The document distinguishes the
finite continuation family from fresh future search and specifies cost, coverage
and external-demand sensitivity evidence. This documentation adjustment does not
complete the production host/adapter cutover or change the installed writer.
