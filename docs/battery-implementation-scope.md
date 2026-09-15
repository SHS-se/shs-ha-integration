# House-battery implementation scope

## Participation and supply added to implementation scope — 15 September 2026

Add the three-owner participation contract, removal of excluded-device metadata, role-based chart accounting, explicit battery supply selector, solar attribution, and live eligible-demand enforcement to the required battery work packages. Reuse existing scorer/compiler/evaluator/reconciliation boundaries. Four local modes and rating-wide full-residual supply are current implementation inputs to migrate, not acceptance criteria. Verify mixed-mode physical demand, observation quality, subgroup capability and future-cost ranking before rollout.

See the [agreed participation and battery supply specification](device-participation-and-battery-supply.md).
Documentation only; replacement implementation and coordinated rollout remain pending.

**Progress update, 14 September:** the scorer, captured accounting audit, bounded
battery suffix compiler, diagnostic time/state coverage, pure runtime/checkpoint,
gross actuals ledger and exact-anchor HA binding now exist. They remain offline
prototypes except for the separately shipped battery command-v2 intent change.
See the [14 September architecture review and battery release gates](controller-architecture-review.md) for verified
completed recovery fixes, the proposed mixed-mode contract and the current **no-go** decision
on enabling the replacement battery controller. Thermal modelling, direct user
controls and notifications may remain deferred; battery deployment and mixed modes
are required scope. The estimates below are the original planning baseline, not
a fresh estimate of remaining work.

Assessment: 14 September 2026. This maps the current working-tree target documents
and the battery boundary review to implementation work. It is a scope estimate,
not a new executable contract, a delivery promise, or authority to enable hardware.

## Product outcome

The planner predicts energy flows and supplies economically justified household
operating alternatives. HA chooses a supported alternative using real conditions;
the battery adapter establishes its modes and limits; the inverter handles rapid
solar/load changes. Forecast power must not become a cap on otherwise authorised
solar capture or house supply.

For the captured 11:15 example, 523.94 W is expected surplus, not the desired
maximum solar uptake. A selected capture alternative can permit the physical
charge rate while separately allowing or withholding house supply. Grid-enabled
replenishment and battery export have their own explicit meanings. A discharge
power ceiling alone does not preserve an energy reserve.

UI preference: keep the schedule compact. Retain the short plan identity and
issue time, use one short battery intent line, and put technical evidence in the
existing Status/diagnostic surfaces. Do not recreate an explanatory Plan details
section or put forecast, limits, applied registers and measurements into every
schedule row. Example future text: “Capture solar surplus · preserve battery” or
“Capture solar surplus · supply the house.” These labels must describe accepted
intent; they are not claims about current implementation or physical delivery.

## Existing work to reuse

- Schema-8 explicit battery operations, two non-negative power ceilings, source
  and export flags, and strict consumer validation.
- Battery quantity resolution from sensors/literals, units, capacity, rated
  powers, native cutoff, actuator steps and mode-option configuration.
- Scheduled command application, readback and fresh physical observations;
  approved rated-limit battery handover and a restoration journal.
- Scoped event scheduling/deadlines, telemetry and price/PV inputs, cached plans,
  plan acknowledgement, matching plan identities, and replan exchange.
- Four operating modes, simulated command verification, diagnostic samples,
  execution/attempt logs and controller performance metrics.
- Battery state/efficiency simulation, value curves, dispatch/settlement,
  transaction/refinement passes, first-quarter continuity candidates, fixed
  intervals, replay capsules and generated provider/consumer fixtures.

These are foundations, not the target policy/runtime. In particular, current code
already uses partial-quarter duration in its physical planner calculations
(`energy-optimisation.ts`, `duration_hours`); the old v30 continuity prose saying
it always models a complete first quarter is not a reason to rebuild that work.
The new local policy still needs remaining-segment cost and accounting semantics.

## Work packages

Sizes are relative implementation/review effort, not line counts.

| Package | Current gap and required work | Main implementation surfaces | Size |
|---|---|---|---|
| 1. Battery response and intent contract | Command v2 already supplies native solar capture at rated charging power with zero discharge. Complete independently permitted capture/house supply, zero-forecast opportunities, deliberate replenishment, hold and authorised export under the new policy. Establish hardware-realizable source/destination meanings, capability revisions, purposeful limits and state/coverage guards. Separate requested replenishment from aggregate ceiling; reject unsupported minimum-charge-plus-extra-solar requests. | Provider battery command/types; HA validator and battery adapter; OpenAPI and generated fixtures | Large |
| 2. Final household scorer and future economics | Price the final materialised household consistently, including battery interactions with EV, pool, boiler/room demand and shared constraints. Reoptimise affected future actions; audit losses, wear, terminal/reserve value, shaping and cost ownership. Existing auction/marginal diagnostics cannot substitute for future consequences. Resolve the current degradation parameter's wear versus terminal/risk roles. | `energy-optimisation.ts`, `dispatch-plan.ts`, household scoring/value-curve modules | Very large |
| 3. Bounded executable policy compiler | Compile complete alternatives, feasible reference cells, current-segment cost and future deltas, applicability, supported states and approximation evidence. Include worthwhile grid and zero-forecast choices. Bound work/wire size; measure held-out ranking error and compilation cost. The bounded exact compiler and diagnostic time/state coverage now exist; an executable production profile and consumer still do not. | New backend policy compiler and response model; staged worker/ingest integration | Very large |
| 4. Intermittent PV and headroom | Model correlated subquarter PV/load paths, native routing, clipping, gross cycling and resulting future state. Compare useful early drawdown with retention, including peak/trough paths with equal net quarter energy. Reuse PV forecasts/calibration but add and validate the fluctuation model. No fixed SOC or daily-yield trigger. | Response/scenario model, scorer/compiler, replay fixtures | Large / experimental |
| 5. Local household decision runtime | Decode/index accepted policy once. Use one short synchronous reducer for decisions, observations, constraints and reservations; queue asynchronous group effects. Compare supported complete alternatives, react to load steps and partial-relief cases, and coalesce replans. Replace the shared execution lock without introducing an independent battery optimiser. | HA controller/scheduler/events/coordinator; new policy/runtime owner | Very large |
| 6. Actual-energy and uncertainty ledger | Track gross charge/discharge, usable state, meter epochs/quality, pending physical effects and conditional reservations once across events, replans and restarts. Advance time-based state/deadlines even without changed sensor values. The pure gross-counter ledger, atomic checkpoint and per-stream settlement independent of expired policies are implemented; live source and durable-storage ports remain open. | HA observations, accounting/reducer and persistent state | Large |
| 7. Battery transitions, confirmation and recovery | Implement commissioned mode/limit transitions, whole-home supply-gap checks, decreases/increases ordering, safe repeatability and ambiguous/late-effect handling. Correct external drift while Controlling with bounded retries. Distinguish normal native regulation/full SOC/forecast drift from actual response faults. Confirmation must follow intent, not exact forecast watts. | HA battery actuator group, controller command/confirmation logic and scheduler | Large |
| 8. Modes, continuation and handover | Specify mixed-mode hypothetical versus real effects; Verification cannot fund live battery decisions. Fence mode exit, reconcile release, preserve retry pacing and absolute validity, adopt unchanged requests after routine restart, and retain durable issued-operation evidence. Keep approved handover for genuine release/expiry. No added outage reserve or change to the read-only cutoff. | HA lifecycle, reducer/journal, mode/configuration boundary | Large |
| 9. Transport, fixed plans, compact UI and audit | Version execution semantics across provider, worker, API, HA and portal; validate capabilities and reject unsupported replacement. Decide fixed-interval migration without reinterpreting old commands. Carry policy/intent/model/response identity and replay evidence. Replace forecast-as-imperative labels with concise intent; preserve simulated/live and measured/applied distinctions in diagnostics. | API/contracts, ingest/worker, fixed-plan/replay readers, presentation and portal | Medium–large |
| 10. Acceptance and commissioning | Cross-language fixtures and mutation tests; replay/physical/economic invariants; compiler/runtime benchmarks; failure/restart/drift/mixed-mode traces. Establish native routing, source evidence and latest-effect bounds on the actual inverter before relying on them. Run shadow/Verification first. | Test suites, replay harness, benchmark tools and installation evidence | Large, throughout |

## Battery-first boundary and dependencies

Battery-first does not require replacing every thermal controller, building a
notification system, shipping the proposed device catalog, replacing all forecast
providers, or migrating the entire planner to a different solver/language.
Retain supported existing components where their semantics satisfy the contract.

It does require honest whole-home accounting. Other loads remain measured or
explicitly modelled constraints/participants. A hypothetical EV/pool action cannot
release real headroom. Where shared service/recovery is part of an economic
comparison, the scorer must support it; do not freeze all future actions and call
that equivalent to the target. A deliberately narrower supported first policy is
possible, but its coverage and omissions must be explicit and tested.

The new home decision owner must not run alongside a competing controller for the
same battery. Existing non-battery paths need an explicit coexistence boundary
until they migrate: account for their real pending effects and actual load without
claiming policy authority over actions the new runtime cannot control. This is a
shared prerequisite, not a reason to silently broaden the battery-only release.

The linked architecture review now proposes the battery-first mixed-mode and
authority lifecycle contract. Production retry, response, relief and evidence
profiles still need implementation-level specification and validation. Model fidelity and compiler cost are evidence gaps that can change the
implementation size. Native Maximum Self Consumption or PV-first mode names do
not establish complete behaviour under backup functions, grid limits or EMS loss.

## Recommended sequence

1. **Contract/response and offline prototype (roughly 3–5 reviewable changes).**
   Freeze request semantics and battery-first/coexistence boundaries; create
   response fixtures and final-household scoring comparisons; benchmark a bounded
   compiler on captured inputs. This is the main scope/risk checkpoint.
2. **Local runtime and durable execution (roughly 4–6 changes).** Build the pure
   decision/ledger core, battery transitions and evidence, modes, bounded retries,
   restart adoption and release. Exercise event traces without hardware writes.
3. **End-to-end integration (roughly 3–5 changes).** Versioned producer/consumer
   wiring, fixed-plan handling, compact labels, diagnostic/replay evidence and
   shadow execution. Do not expose unsupported policies as controllable.
4. **Validation and physical commissioning (roughly 2–4 changes plus installation
   sessions).** Complete scenario and performance gates; commission relevant
   native modes, source restrictions, transitions and lifecycle before enablement.

Total planning allowance: approximately **12–20 substantive reviewable changes**
across the two repositories, with tests included in each. A rough single-engineer
order of magnitude is **8–12 focused engineering weeks plus commissioning time**,
not an estimate based on measured prototype velocity. The compiler/scorer and
subquarter response model could push it beyond that range; some adapter/runtime
work can proceed concurrently after the contract is settled. Re-estimate after
stage 1 rather than treating this as a fixed delivery date.

The charge-limit/UI symptom alone is small, but does not implement the battery
portion of the accepted plan. Do not count that shortcut as completion of this
scope, and do not defer restart/source/physical prerequisites until after enabling
broader battery authority.

## Acceptance examples specific to the battery

- Forecast charge 524 W, real surplus 3 kW; zero forecast followed by surplus;
  surplus beyond rated charging capability; full battery.
- PV 8 kW → 1 kW → 8 kW with house supply permitted and withheld; no mode chasing.
- Expensive now but more valuable stored energy later; high export opportunity;
  negative prices; low SOC; physical cutoff and conditional reserve release.
- Deliberate grid replenishment, no grid permission, and unsupported requested
  minimum-plus-surplus behaviour. Preserve source attribution uncertainty.
- Proactive headroom versus retention under equally energetic but differently
  correlated PV paths; compare real service, later purchases, clipping and wear.
- Unknown 2/8 kW load steps, transitions temporarily removing battery supply,
  partial relief, stale observations and uncertain/pending commands.
- Mode exit during an operation, repeated external drift, late success after
  timeout, restart/downtime, new plan mid-quarter, meter reset and genuine expiry.
- Planning/Verification loads differ from real demand; no fictitious headroom.
- Fixed-plan replacement, incompatible policy/capability versions and absent
  coverage; no inferred translation or reset of validity/accounting.

## Source map

Current HA implementation and boundaries:
[battery configuration](battery-control-configuration.md),
[operating modes](device-operating-modes.md),
[offline operation](offline-operation.md),
`battery_commands.py`, `controller.py`, `controller_scheduler.py`,
`controller_observations.py`, `presentation.py`, `verification.py` and tests under
`tests/` (Python modules are under `custom_components/shs_energy/`).

Canonical target documents in the sibling `smart-home-solutions-t-by` repository:

- `docs/energy-optimisation/controller-policy.md`: whole-home economics, compiler,
  applicability, headroom, module ownership and declared coverage.
- `docs/energy-optimisation/reactive-controls.md`: observation/decision cycle,
  metering, physical constraints, request confirmation and continuation.
- `docs/energy-optimisation/control-reconciliation.md`: shared ownership,
  asynchronous effects, automatic drift/retry, modes and outstanding questions.
- `docs/energy-optimisation/contracts-and-data.md`: versioning, identity, validity,
  watermarks, generated provider/consumer fixtures and future transport fields.
- `docs/energy-optimisation/planner.md` and `models-and-forecasts.md`: physical
  scorer, utility/terminal/wear ownership, forecast provenance and uncertainty.
- `docs/energy-optimisation/verification-and-delivery.md`: complete acceptance
  matrix, empirical/certified evidence distinctions, benchmarks and delivery gates.
- `ENERGY_OPTIMISATION_ARCHITECTURE_REVIEW.md`, D3–D6: settled battery, grid,
  authority, outage and commissioning decisions; D7 adds audit/replay scope.

The independent Opus/Codex review's useful refinements are incorporated above.
Its proposed interim marginal-diagnostic discharge rule was rejected; it is not a
work package or authorised simplification of the whole-home policy.
