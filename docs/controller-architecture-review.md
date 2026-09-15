# Controller architecture review and battery release gates

## Updated battery release requirements — 15 September 2026

The latest agreement replaces the four-mode target with three separately owned participation facts and adds explicit battery house-supply scope. The v35 rating-wide correction is not the final policy. Release gates now include metadata exclusion, immutable role/scope identity, Verification external demand, graph partition, aligned subgroup observations, solar attribution and native scope enforcement, as well as existing C + V and single-writer gates. Earlier test counts and completion claims below do not establish this new scope.

See the [agreed participation and battery supply specification](device-participation-and-battery-supply.md).
Documentation only; replacement implementation and coordinated rollout remain pending.

Current assessment, 15 September 2026: the usable battery policy and mixed-mode
software execution/ownership stage is implemented in `0.8.0-beta.91`. The
[execution design](battery-execution-design.md) and [binding contract](policy-binding.md)
record the resulting interfaces and behavior. Independent Claude Opus **Max** and
Codex designs were compared before implementation; final review reproduced and
closed mode-scope, stale-condition and ownership-handover defects.

**Do not turn battery Controlling back on yet.** The new compiler, evaluator and
reconciliation protocol are ready for live host/adapter integration, but
`ScheduledController` remains the installed writer. No live HA ports, shared final
writer arbiter or commissioned native adapter were installed by this work. A beta
with controls disabled can validate installation; it cannot prove these missing
stages. No deployment or device-setting change was made.

The original review below was against integration `35b2035` (beta.88) and backend
`383f36c`. Its findings and overnight-data analysis remain historical evidence.
The follow-ups identify what has since been fixed; the original one-millisecond
policy lease is no longer the active software contract.

Thermal modelling, direct user controls and notifications are explicitly deferred
by the user. They are not prerequisites for a deliberately bounded battery-first
release. Mixed operating modes, coexistence and battery deployment gaps are in scope.

## Follow-up: usable policy and mixed modes completed

- The backend now compiles `battery-execution-policy-v1`: bounded future
  continuation functions with explicit validity, source permissions, physical
  domains and exact component scoring. It includes continuous SOC coverage from
  a normally ranked idle family where feasible, while reporting remaining holes.
- HA evaluates the remaining quarter from live SOC, PV and real household demand,
  retaining ramp coupling, terminal utility, saturation and C/F/J reconciliation.
  There is no fixed energy allowance or observation-by-observation recompile.
- Forced charging uses **Command Charging (PV First)**. Grid First and implicit
  PV curtailment are excluded. Export reserve is distinct from physical cutoff;
  predicted crossing is rejected without inventing a native reserve-stop feature.
- Mixed scopes keep external EV/pool demand and unresolved effects in headroom
  accounting exactly once. A hypothetical stop cannot fund battery charging.
  Ownership handover cannot erase a known issued effect.
- Battery requests have one owner. Mode/revision changes require a new scope and
  matching policy. Confirmed writer grants fence preparation and final dispatch.
  Release has an independent revision; expiry, release failure, re-entry and
  restart preserve unresolved effects. Fresh re-entry may cancel an unissued
  release without forcing a baseline cycle.
- Same-target renewals preserve execution identity and original prepared-send
  deadlines. Discretionary changes require sustained advantage; ineligibility
  withdraws immediately. Fresh observations alone do not create journal churn.
- Source-watermark accounting survives delayed policy receipt, pruning and restart.
  Schema **5** rejects schema 4; the old exact-anchor reader remains offline
  analytical tooling. Live migration is a roll-forward host-stage decision.

Findings **2** and the software/protocol portion of **5** below are now addressed.
Finding **3** has an explicit native response model and catalog validation, but
physical commissioning is still outstanding. Findings 4, 6, 7 and 8 were already
fixed in beta.89 and remain covered.

The remaining gates before enablement are concrete:

1. Wire and test the backend policy refresh path and HA observation, meter, event,
   timer, journal and transport ports. Handle missing/expired policy and source
   revisions through the defined reducer protocol.
2. Enforce one durable grant arbiter at **every** legacy and new final battery
   write path, including startup, restoration and unload. Prove mixed-mode
   behavior with the existing EV/pool owners on the live integration.
3. Commission the real battery's native transition ordering, readback, settling
   and late-effect limits, physical limits, export-reserve protection, release,
   restart and abrupt-outage behavior. Synthetic response evidence is insufficient.
4. Replay representative installation data to measure coverage, compile cadence
   and economic quality, then conduct a supervised battery-only trial with the
   commissioned release path available. Finite-family exact scoring does not
   establish global optimality or a measured regret bound.

### Validation for beta.91

| Check | Result |
| --- | --- |
| Full HA Python suite | 613 passed |
| HA compileall and frontend Node tests | Passed; 59 frontend tests |
| Full backend Deno suite | 1,119 passed |
| Backend repository lint | 0 errors; 26 pre-existing warnings, unchanged from baseline |
| Backend production and dev test-mode frontend builds | Passed; existing large-chunk warning |
| Mocked-backend Playwright suites | 31 passed |
| Provider/consumer fixture regeneration check | Passed; 180 current-response and 96 continuation cases |
| New backend/scorer files, explicit scoped lint | 0 errors or warnings |

Continuation cells are checked against the authoritative household scorer at
boundaries and interior E/P points, including idle-family terminal utility and
PV-constrained bridge seeds. The old offline examples remain byte-identical after
scorer extraction. Schema-5 crash, ledger and policy traces replay with final send
authorization checks. The 64-cell evaluator bound is exercised. These are software
results; they do not establish native timing, production policy quality or live
writer exclusion.

Reproduce with:

```sh
# HA repository
python3.13 -m unittest discover -s tests -v
python3.13 -m compileall -q custom_components/shs_energy
node --test tests/frontend-config-save.test.js
python3.13 scripts/generate-execution-policy-fixtures.py ../smart-home-solutions-t-by --check

# Backend repository
deno task test
npm run lint
npm run build:test  # current backend dev branch; use npm run build on main
npm run test:e2e:local
```

## Earlier follow-up: four runtime defects fixed


The user scoped implementation to findings **4, 6, 7 and 8**. These are now fixed
in `0.8.0-beta.89`, with the [implementation contract and validation](runtime-recovery-fixes.md):

- Invalidated or advanced sequences cancel their unsent retries atomically while
  retaining issued effects.
- Each meter settles its policy accounting prefix at a real counter boundary;
  capacity maintenance continues through policy loss without resetting actuals.
- Transition calculation has unique tokens, deadlines, retryable failures with
  capped pacing, and explicit unsupported outcomes. Late replies are fenced.
- Configured finite relief rules admit proved non-worsening reductions under
  overload; observed and uncertain reservations remain until confirmation.

That beta used checkpoint schema **4**, rejecting earlier prototype schemas;
the new execution stage advances this to schema 5. The relief rule tests
use synthetic evidence; no native battery rule has been commissioned. The current status of the other
findings is recorded above. Production deployment and battery enablement still
require the native adapter, live host and writer-cutover gates.

## What has been completed

| Stage | Implemented and reusable | Limit of the evidence |
| --- | --- | --- |
| Shipping battery intent | Schema 8 with battery command **v2**, native solar capture at rated charge power and zero discharge, separate permissions/ceilings, mode-aware confirmation, compact labels | `ScheduledController` still owns live writes; wider economic capture/supply and continuity are not implemented |
| Offline household scorer | Physical trajectory validation and a shared component objective; battery efficiencies, gross wear and future consequences | Synthetic thermal capability is not a calibrated thermal model |
| Captured-plan accounting audit | Checks final materialised electrical balances and accounting inputs | Does not produce a fully resolved captured household economic model or prove commanded delivery |
| Exact battery compiler | Finite battery suffix reoptimisation; common reference; component C/F/J reconciliation; fixed-tail reversal and negative-price examples | One battery; non-battery demand is external; finite search is not continuous optimality |
| Time/state coverage | Time/energy rectangles, restricted physical feasibility witness, held-out economic error and ranking regret | Diagnostic-only; all other conditions frozen; empirical evidence is not a regional economic certificate |
| Household reconciliation core | Pure immutable reducer; complete group targets; persist-before-send; mode/generation fencing; uncertain attempts; bounded retry pacing; independent group progress | Synthetic adapters and effect protocol only; no HA event, timer, transport or durable-storage worker |
| Gross actual-energy ledger | Directional mWh, source revisions/epochs, uncertainty bounds, retention and watermark reconciliation in the household checkpoint | No live meter port, source-revision recovery or operational retention worker |
| HA execution policy | Closed native-response evaluator, bounded future functions, live conditions, context/ledger fencing and generated provider fixtures | Quarter-bounded finite family; coverage/latency/quality must be measured on installation data |
| Mixed-mode execution and ownership | Explicit executable scope, local native catalog, durable writer grants, independent release revision and schema-5 recovery | Pure reducer/final-dispatch protocol; live ports and common legacy arbiter remain to implement |

The implementation documents record historical test totals at their respective
commits. They must not be read as successive production rollout sign-offs.

## Original findings and battery deployment gaps

These are the original beta.88 findings, retained for traceability. Findings 4, 6,
7 and 8 were fixed in beta.89. The current execution-policy follow-up closes 2
and the software portion of 5; 1 and native/live portions of 3 and 5 remain gates.

1. **Live integration still uses the old owner.** `__init__.py` constructs
   `ScheduledController`; its execution lock spans device I/O, and startup/unload
   restore owned devices. Adding pure modules has not replaced that path. The new
   runtime's restart tests do not prove live restart continuation.
2. **Coverage does not cross the producer/consumer boundary.** Backend coverage
   permits time/energy interpolation for diagnostics; HA validates an exact problem
   and uses a one-millisecond lease. The coverage producer permits up to 2 MB,
   versus the 128 KB exact reader and 1 MB checkpoint. The backend also supports
   terminal utility that the current HA profile excludes. A versioned executable
   profile must reconcile supported conditions, error acceptance, wire/checkpoint
   bounds and dispatch revalidation. Extending the old lease is not a solution.
3. **Native response and physical scope remain unmodelled at this boundary.**
   Imposed charge/discharge paths are not Sigenergy modes and ceilings. Adapter
   identity strings are declarations, not commissioned evidence of routing,
   repeatability, late-effect bounds, control ownership or outage behaviour.
   Aggregate import/export envelopes do not establish per-phase limits or useful
   relief when already overloaded.
4. **The current reducer blocks reductions during an overload.** In a synthetic
   reproduction, 8 kW external import plus 4 kW observed charging against a 10 kW
   import limit leaves a stop-charging request at `physical_scope_blocked`, with
   zero preparations or sends. `_room` takes the maximum observed/pending/new
   envelope and requires the complete result within the limit. This is conservative
   for ordinary changes, but the target's commissioned partial-relief path is
   missing. Preserve uncertain reservations; introduce an explicitly proved
   non-worsening relief relation instead of treating a requested reduction as
   already delivered.
5. **Mixed-mode execution is not specified or implemented end to end.** The generic
   reducer models per-group authority; the economic binding explicitly requires
   one group. No provider contract distinguishes hypothetical household actions
   from the executable branch, and no live boundary accounts for old EV/pool
   controllers' possible effects. A dry-run stop cannot fund real battery charging.
6. **An inactive policy can stop continued metering.** After acceptance and expiry,
   `LedgerPruned` still forbids pruning past that policy's original watermark.
   Reproduced with the policy harness: 128 further counter intervals are accepted;
   sample 129 fails with retention-capacity exhaustion, and a prune advancing the
   anchor fails too. A new successful policy can advance the watermark, but a
   prolonged compiler outage has no independent settlement path. This is a
   recovery defect to fix before live integration, not permission to discard
   actuals or extend an expired policy. Sampling cadence determines elapsed time
   to exhaustion; the bound is 128 intervals by default, not 128 seconds.
7. **Transition calculation can wait indefinitely after failure.** `_need_transition`
   emits once and retains `waiting_for`. With that effect lost, ticks at 1001,
   2000 and 89999 ms in the runtime harness produce no replacement request while
   the observations/request remain valid. Invalid/stale results do not complete
   current work either. Actuator retry pacing exists; transition-job completion,
   failure and timeout pacing do not. Add identified success/unsupported/retryable
   failure outcomes and a bounded job deadline; fence late responses. Permanent
   unsupported transitions need a reason, not busy retries.
8. **A normal retry can produce an unpersistable checkpoint.** Reproduced from
   the runtime harness: send a step, report it ambiguous, then prepare an identical
   retry after the first attempt's latest-effect deadline. Before that retry is
   durable, fresh readback still shows the original controls. `_settle` removes
   the first attempt and clears its plan, but retains the new preparation; `_drive`
   leaves it waiting for durability. `encode_checkpoint` fails with
   `preparation has no matching sequence`. This is a concrete core invariant defect,
   not just a missing host. A preparation must remain owned by its valid sequence
   or be cancelled atomically when that sequence is invalidated; issued attempts
   must retain their uncertainty. Add crash-cut regression coverage for this path.
9. **The host protocol still needs failure and lifecycle design.** Define atomic,
   ordered durability; authority fencing at actual dispatch; recoverable sensor
   revisions; bounded queues; failed observation/transition calculation retries;
   event/deadline delivery; storage failure and retention exhaustion. Restart and
   mapping changes must preserve unresolved old effects, rather than lose their
   identity when rebuilding the configured group list. These are necessary work,
   not mechanical wiring.

Source evidence: `__init__.py:190–201`; `controller.py:1108–1114,1145,1273–1278`;
`home_runtime.py:514–560,589–628,651–665,671–685,730–766,819–820,869–907`;
`home_runtime_checkpoint.py:91–97`;
`energy_ledger.py:166–184,215–218`; backend
`battery-policy-coverage.ts:297–324,372–417`. These locations refer to the reviewed
commits above; subsequent implementation may move them.

These findings distinguish prototype restrictions from regressions in supported
behaviour. They do not imply that a passing synthetic protocol test establishes
hardware safety, physical response or production timing.

## Problem and caller usage for the next design

Retain the single household decision owner. Give it a production policy that is
applicable to current evidence, and an explicit authority/lifecycle state for each
physical actuator group. The backend owns future optimisation; the HA host owns
ordered event delivery and dispatch; the adapter owns device-specific transition
knowledge. The following signatures are proposed, not an existing API or schema.

```python
# One live owner; construction verifies exclusive actuator mappings.
home = await HomeControl.open(configuration, journal, observations, adapters)

# Mixed-mode plan acceptance checks the execution projection, capabilities,
# authority revisions, actuals and supported economic evidence together.
acceptance = home.offer_policy(payload)  # reader/profile checks are internal
home.post(ObservedFrame(frame_with_real_external_demand))

# A mode selection is a revisioned intent, not proof that release completed.
home.post(ModeRequested(group_id="battery", mode="planning", revision=12))
status = home.status("battery")  # requested=planning, phase=releasing, ...

# Verification uses the same rules in isolated simulated state and cannot
# obtain a live dispatch capability or mutate live actuals/reservations.
trace = simulate(policy, recorded_events, simulated_adapter)
```

## Shape and ownership

```text
ExecutionScope = (controlled_groups, external_groups, authority_revisions,
                  mapping_revision, response_revisions, constraint_profile)
AcceptedPolicy = (identity, execution_scope, cells, C/F bands, feasible_references,
                  acceptance_evidence, absolute_validity, actuals_watermark)
Authority = Passive(mode, revision)
          | Entering(revision, prerequisites)
          | Active(revision)
          | Releasing(requested_mode, revision, release_request)
          | ReleaseFault(requested_mode, revision, unresolved_effects, reason)
PhysicalFrame = (observed_groups, external_demand_bounds, constraint_evidence,
                 freshness, unresolved_effects)
TransitionJob = (token, request_revision, observation_revision, adapter_revision,
                 deadline, retry_not_before, completion_status)
AccountingCursor = (settled_through, gross_bounds, coverage, policy_provenance)
Status = (requested_mode, authority_phase, effective_request, observed_outcome,
          policy_identity, release_progress, uncertainty, next_deadline)
```

| Owner | Public boundary and substantial responsibility |
| --- | --- |
| Backend scorer/compiler | `compile_execution_policy(resolved_problem, scope, acceptance)` owns native-response scenarios, future solves, C/F representation and evidence |
| HA policy reader/evaluator | `read_execution_policy(bytes, profile)` / bounded evaluation own hostile input, indexes, coverage, identity and supported numerical claims |
| `home_runtime` | `reduce_home(state, event, now)` owns authority, selected requests, reservations, retries, actuals and the atomic logical commit |
| HA host | `post(event)` installs state and routes effects; owns event ordering, dispatch fences, bounded task queues and timers; never runs a second economic decision |
| Durable journal | Atomically stores ordered complete checkpoints and acknowledges the exact preparation; does not coalesce away required acknowledgements |
| Observation/meter port | Converts HA units, timestamp quality, source epochs and raw plant evidence into validated domain events; persists revision continuity |
| Battery adapter | Computes complete transitions and runs transport/readback against a commissioned capability profile; owns source/routing semantics, native guards, uncertainty and repeat evidence |

The runtime must settle accounting independently of policy execution lifetime.
Before advancing retention, persist gross measured/uncertain actuals and provenance
through an explicit accounting cursor atomically with the retained tail. A later
policy replacement reconciles the settled account and tail exactly once. Expiry
alone is not settlement; pruning cannot erase information still needed to refine
partial intervals. Test several retention windows, late cumulative samples, resets,
restart and eventual compilation recovery. This accounting obligation is now implemented for the offline runtime using
per-stream settled prefixes; arbitrary queries before the retained history still
fail because lifetime totals cannot reconstruct their time distribution.

No public load/validate/transform/save pipeline is added. The host hides transport
and lifecycle coordination rather than forwarding every reducer method. The policy
boundary hides wire representation; shared physical resources remain owned once.
A separate simulated state may reuse pure decisions but cannot share mutable live
ledger, reservation, journal or send state.

## Synthesis decision and alternatives

Use **Codex candidate A, one household authority runtime with separate real and
hypothetical planning views**, as the base. It preserves the implemented single
writer, checkpoint and accounting boundary and the agreed bounded C/F policy
representation. Adopt Claude's explicit native response classification, independent
release-template revision, legacy-owner exclusion, and its reproduced preparation/
checkpoint finding. Checking register equality alone cannot prove a native forced
operation responded; the adapter must supply physical-response evidence appropriate
to the operation.

Authority revisions must be persisted/recovered by the HA configuration boundary;
current saved modes do not supply the reducer's revision protocol. Refreshing a
release template or its rated-limit evidence must not masquerade as a new mode
selection and silently erase the live request. Give release configuration its own
revision; revalidate any affected physical capability and pending transition.

| Candidate | Caller and hidden complexity | Migration and operational assessment |
| --- | --- | --- |
| Codex A: single owner, real/hypothetical scope, bounded C/F alternatives | Compact policy/event API; central physical and accounting invariants; explicit scope and lifecycle | Selected; reuses the pure core, but needs the reproduced recovery fixes and live ports |
| Claude A: native responses plus end-of-segment value table over battery energy and import | Similar caller API; moves current response/scenario evaluation into HA and compiles a boundary value function | Retain as an experiment; structurally different from the adopted per-alternative bands and unvalidated on captured cases |
| Per-group actors plus a resource-lease arbiter and separate ledger actor | More coordination for shared reservations, revoke/late-effect races and atomic accounting | Rejected for current scale; no measurements justify splitting the one commit authority |
| Retrofit the old scheduler as the new battery executor | Small apparent diff but preserves competing restoration, fault and lock policy | Rejected; reuse validated device knowledge, not an independent battery decision owner |

Claude's value-table proposal is a plausible battery-specific research direction,
not a measured replacement for the agreed representation. Compare coverage, C/F
reconciliation, regret, compilation cost, wire size and HA work on identical
captured inputs before changing representations. Width sensitivity of a pruned
search is useful evidence, but is not itself a bound on ranking regret or proof of
optimality. The existing diagnostic cells remain valid within their stated frozen
conditions; the example benchmarks do not prove every realistic horizon fails.

Reject Claude's suggested blanket relief rule that ignores the frame whenever a
step envelope is no larger than the old reservation. A register decrease or equal
aggregate bound does not prove a safe transient, supply continuity or phase effect.
Keep commissioned relief explicit. Also retain the current exact-preparation
acknowledgement contract until ordered coalescing and crash semantics are designed
and proved together; changing `==` to `>=` alone is not the implementation plan.
The replay CLI currently defaults to the newest pending snapshot and removes older
ones (`scripts/replay-home-runtime.py:35–41`), so tests must explicitly cover skipped
preparation acknowledgements rather than treating every replay as a faithful
ordered-worker model.

Do not adopt unverified claims that all HA sources lack source timestamps, that
Command modes necessarily persist through an outage, or that no hardware watchdog
exists. Establish timestamp/latency provenance and outage behaviour for the actual
source/device. Align producer/consumer physical tolerances (currently 1e-7 versus
1e-8 in relevant checks), keep economic qualification separate from native guards,
and specify conflicting transport outcomes in the production profile.

Tradeoffs accepted:

- We accept conservative scope exclusions in exchange for never spending
  hypothetical or unconfirmed headroom.
- We accept immutable snapshots and whole-checkpoint writes in exchange for one
  reviewable crash/accounting boundary; measured worst-case cost can falsify this.
- We accept a narrowly declared battery release in exchange for deferring thermal
  control without pretending its future actions are controllable.
- We accept independent queued device I/O in exchange for keeping waits outside
  the reducer, while preserving one physical authority and commit owner.

The proposed host is a substantive ownership boundary, not a public series of
pass-through stages. Separate actors, duplicate physics and a second horizon
optimiser are not added to solve missing port contracts.

## Mixed operating modes: proposed battery-first contract

Keep the four customer modes. Separate participation in a **conditional plan**
from membership in the **live execution scope**; both derive from one versioned
intent source and do not create competing optimisers.

| Mode | Planning/diagnostic meaning | What battery execution may rely on |
| --- | --- | --- |
| Monitoring | Observe; do not schedule that device as a controlled action | Real observed demand plus justified uncertainty bounds |
| Planning | Include conditional future opportunities; issue no actuator requests | Actual demand, never assumed execution or hypothetical released headroom |
| Control verification | Evaluate simulated commands and label their outcomes as simulated | Actual demand and outstanding real effects, never simulated delivery |
| Controlling | Eligible for requests inside the accepted live policy and native authority | Conservative observations and pending effects; a new command is not confirmation |

For a first battery-only policy, every other device is an external electrical
participant, even if an existing SHS controller operates it. The resolved current
frame and future external-demand model must carry that fact. Do not promise
rescheduling authority over those devices or use their conditional desired schedule
as guaranteed delivery. If their possible changes cannot be bounded for a required
physical decision, that decision is outside supported scope. Unknown thermal
behaviour does not justify zero demand or an invented thermal model.

Only one SHS owner may write the battery's complete mode/ceiling/authority surface.
Exclude it from the old scheduled owner at cutover. Existing EV/pool owners may
coexist only behind the external-demand boundary; migrating their control into the
new runtime is a later explicit scope change. Do not wrap the old battery executor
under the new owner while retaining its independent restoration/fault policy.

For shared physical controls, reject incompatible command authority at the group
boundary; never silently promote a Planning sibling to Controlling. Independent
service modes are allowed only after a supported adapter establishes independent
levers and the common plant constraint. Shared thermal command design is deferred;
it is not needed to operate an independently scoped battery.

| Transition | Required behaviour |
| --- | --- |
| Passive to Controlling | Increment authority revision; enter `Entering`; require current observations, validated capability/mapping, resolved old effects and a policy matching the new authority before optimisation sends |
| Controlling to passive | Fence unsent optimisation immediately; preserve issued effects and retries; only approved release may write; expose requested passive mode separately from releasing state |
| Rapid re-entry during release | Increment revision again and cancel only unsent obsolete work; reconcile issued release/optimisation effects; require a matching policy before resuming; preserve retry pacing |
| Release failure | Keep the journal, physical uncertainty and visible `ReleaseFault`; retry only supported release operations; never report successful release merely because a selector changed |
| Routine restart/reload | Reconfirm saved authority against configuration, reconcile physical state and ambiguous attempts, retain absolute deadlines and accounting; adopt unchanged valid operation without a baseline cycle |
| Real expiry/removal/permanent shutdown | Perform the approved device-specific release when physically supported; retain failure evidence; do not invent a lease extension or default settings |

Persist authority revision and release phase with attempts. Lifecycle integration
must distinguish a supported routine suspension from genuine shutdown and bound
what may happen while HA cannot act. An HA-independent higher backup reserve is
not added; the native cutoff remains the separate physical protection.

Required mixed-mode traces: verified EV stop versus real 7 kW draw; battery entry
with an old policy; mode exit between preparation and send; rapid exit/re-entry
with delayed effects; failed release followed by restart; overlapping shared
controls; and unchanged observations advancing deadlines. Assert zero live writes
from isolated verification and zero fictitious headroom in every case.

## Decisions still needed before implementation can be called deployable

1. **Executable economic and response profile.** Specify accepted evidence class,
   economic error/regret limits and calibration corpus; practical coverage across
   remaining time, energy, PV/load and changing conditions; policy expiry and
   outside-coverage behaviour; numerical/uncertainty deadbands and sustained-
   advantage timing. Define native buffering/replenishment/source restrictions,
   correlated PV scenarios and whether initial scope excludes proactive headroom.
   Excluded operations must be visible; they cannot be advertised as supported.
2. **Physical transition and relief contract.** Establish actuator authority,
   readback, transient loss of battery supply, plant/per-phase constraints,
   repeatability, dispatch/order/latest-effect bounds, legal monotone relief and
   confirmed handover. Define observation degradation and outage limits from
   evidence on the actual installation. Mode names and native ratings alone do
   not establish these facts.
3. **Production host and acceptance profile.** Specify durability/dispatch ordering,
   queue bounds, error recovery, live source timestamps/revisions, meter retention
   through long policy loss, restart/remap reconciliation, legacy-owner cutover,
   and size/latency budgets that the producer, reader and checkpoint all meet.
   Implement the mixed-mode contract above and expose requested versus effective
   authority consistently in diagnostics.

Thermal modelling, direct user controls and notifications remain separately
tracked future work. Status/fault evidence required for commissioning is included;
a notification delivery framework is not.

| Deferred workstream | Definition/design still required before that feature ships |
| --- | --- |
| Thermal modelling | General room/pool/tank dynamics, losses, COP and shared-compressor allocation; calibrated state and held-out recovery/service evidence before thermal policy execution |
| Direct user controls | Room adjustments and charge-by-time requests through one revisioned intent owner; persistence, expiry, conflicting edits and acknowledgement of effective intent |
| Notifications | Triggers, timing, location treatment, channels, configuration, deduplication and durable retry/delivery; an unplugged EV example is not a complete notification contract |

## Release gates and next implementation step

| Gate | Required evidence | Current result |
| --- | --- | --- |
| Core recovery | Valid retry checkpoints, bounded transition-job recovery, continued accounting through policy loss and commissioned overload relief | Passed in offline scope; all four reproduced defects fixed, native relief evidence still belongs to the physical adapter gate |
| Executable policy | Versioned native-response profile, bounded supported cells, accepted numerical evidence, generated hostile-wire fixtures and actual-context dispatch checks | Open; diagnostic coverage and exact synthetic binding only |
| Physical adapter | Tested native routing, source permissions, readback, transitions, late effects, relief, handover and supported HA outage behaviour | Open; no commissioned adapter for the new runtime |
| Live host | Ordered durable writes, atomic dispatch fencing, bounded effects, revision recovery, restart/remap traces and failure recovery | Open; no live ports |
| Mixed modes/coexistence | Tested execution projection, lifecycle states, isolated verification and exactly one battery writer | Open; proposal above, no end-to-end implementation |
| End-to-end verification | Actual provider-to-HA path, captured inputs, measured coverage, counterfactual error, max-size/performance tests on supported HA hardware | Open; current checks are offline/local |
| Battery enablement | Compatible deployed provider/consumer, fresh plan and mappings, scoped physical commissioning record, observed successful release/recovery | Not established by this review |

The four pure-runtime recovery fixes and their failure/crash traces are complete.
Next define the executable battery profile and mixed-mode execution scope with
provider/consumer fixtures; keep unsupported/diagnostic profiles non-executable.
Build the host and adapter against that contract in an isolated replay/verification
harness, including the overload-reduction counterexample. Only then wire the new
battery owner into HA, verify it with live writes disabled, and perform the scoped
physical commissioning needed before enabling normal control. Do not deploy first
and rely on later thermal, notification or recovery work to supply prerequisites.

## Validation in this review

- Full integration suite: **565 Python tests passed**.
- Frontend configuration/schedule suite: **58 tests passed**.
- Backend battery compiler and time/state coverage suites: **25 tests passed**.
- Both real-compiler HA fixtures regenerated and matched committed bytes.
- Integration compilation and all three committed runtime replay CLIs passed.
- Four extra synthetic probes reproduced overload-relief blocking, lost transition
  work, expired-policy retention exhaustion and the invalid retry checkpoint.

No live HA state, installed version, commissioning log or physical battery response
was inspected in this review. Historical beta.27 installation/persistence evidence
and local beta.88 source tests do not establish the current installation's readiness.

Follow-up validation: **583 Python tests**, including 18 new recovery regressions,
and **58 frontend tests** pass. The three schema-4 replay traces and real-compiler
fixture checks pass. Independent review also compared randomized multi-stream
settlement against an unpruned oracle and round-tripped randomized runtime states.
These checks do not change the physical deployment assessment.

## Installation correction, 15 September: forced-charging mode

Phil confirmed that forced battery charging must use **Command Charging (PV First)**.
Grid First curtails solar to charge from the grid and is excluded from normal
operation. The supplied `history.csv` shows PV falling from 0.948 kW to zero after
Grid First selection, then recovering to 0.936 kW after returning to PV First,
with the charge ceiling unchanged at 3.0 kW. The
[battery contract](battery-control-configuration.md) records the event timestamps.
The overnight configuration's PV First mapping was correct; older Grid First
physical-response records do not justify retaining that mode. This resolves the
mapping question from the overnight-data review, while broader native transition,
recovery and outage commissioning remain separate release gates.
