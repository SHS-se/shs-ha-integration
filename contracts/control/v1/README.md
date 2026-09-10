# Control contract v1

Step 1 of the control implementation plan, 10 September 2026. This is a tested
contract and migration specification. It is not wired into the current API,
planner, executor, database or live configuration. Current executable plan schema
7 remains unchanged; publishing these files does not advertise new execution
support. The future executable schema must negotiate these contracts explicitly.

The entire `contracts/control/v1` directory is mirrored byte for byte between
`shs-ha-integration` and `smart-home-solutions-t-by`. Change it in one repository,
copy the bundle to the other, and run both native validators. `schema.json` is
JSON Schema draft 7; `validate.py` and `validate.ts` add cross-record constraints.
Neither validator is an execution-authority check. `examples.json` contains
representative documents; `fixtures.json` names valid examples and precise edits
that produce invalid examples, with their expected rejection codes.

```sh
# HA repository (use a virtual environment)
python -m pip install -r tests/requirements.txt
python -m unittest discover -s tests -p test_control_contract.py -v
# Website repository
deno test --allow-read tests/control-contract.test.ts
```

## Identity and document boundaries

The server allocates random UUIDv4 `control_id`, `source_id` and association IDs.
They are opaque and scoped by `home_id`; names, categories, HA entity IDs,
statistic IDs and hardware serials never generate these IDs. All storage keys,
foreign keys and lookups include the home. The API authenticates the caller's
home membership (or integration home credential) before looking up an ID. An ID
is not a capability token. Foreign-home IDs return no object, including on batch
reads, acknowledgements and plan publication. Validate every batch member.

| Document | Purpose | May contain raw HA entity identity? |
| --- | --- | --- |
| `definition` | Website desired settings, HA capability proposal, source links | No actuator entity IDs; historical statistic IDs are unchanged opaque history references |
| `local_binding` | Installation mapping, kept locally in HA | Registry `(domain, platform, unique_id)`, optional last entity name |
| `plan` | Commands bound to an accepted revision and separate electrical forecasts | No |
| `runtime` | Permission, agreement, active revision, lease, status and observations | No |
| `migration` | Reviewable one-way mapping report, not an instruction to mutate storage | Existing historical keys only |

Registry resolution uses the exact domain/platform/unique-ID tuple. The last
entity ID is a display hint, never a fallback lookup. A rename retains identity.
A missing identity, multiple matches or replacement requires explicit local
mapping and a binding revision. Group members are recorded and reserved by
resolved identity before ownership, not inferred each time a command executes.
Pool customer automation internals are outside those SHS reservations.

`source_id` links a control to observations without replacing any meter key or
historical samples. A source has explicit provenance, role and accounting use.
Canonical electrical energy may be counted once; aliases and derived/display
sources are observation-only. Two statistics that represent the same energy must
be declared aliases or an unresolved allocation, not two canonical sources.
Proving that relationship is a migration/setup responsibility; a schema cannot
infer wiring from two different strings. Battery is a plant store and cannot be
added to schedulable household load consumption. See [migration.md](migration.md).

## Ownership and revisions

| Fields | Owner and provenance | Revision/change class |
| --- | --- | --- |
| Control/source IDs, home-scoped references | Server allocation; migration evidence for old routes | Immutable identity |
| Name and reporting category | Website/user | Presentation; no desired execution revision |
| Inclusion, association, objective | Website/user or explicit migrated choice | Desired revision; inclusion removal is a permission/removal transaction |
| Load characteristic, running watts override, thermal conversion | Website/user | Desired + planning-model revision |
| Learned or measured running estimate | Model with evidence; never instantaneous power as a rating | Planning-model revision when adopted |
| Registry identity, operations, units, steps and physical bounds | HA/discovered | Binding revision |
| Semantic roles, mappings, handover, reviewed narrower limits | HA/user | Binding revision |
| Accepted tuple | Computed after durable HA validation and server acknowledgement | Exact `(desired, binding, contract.name, contract.version, planning_model)` |
| Local permission | HA/user, separately revisioned | Immediate local guard, no website grant |
| Active tuple, setup/sync/plan/operation status | Computed runtime report | No configuration revision |
| SOC, temperature, actual watts, availability, readbacks | HA/measured observations with sample time | No configuration revision |

Provenance identifies owner, source kind and evidence. A website-supplied estimate
remains an override until explicitly cleared. Discovery updates the observed
candidate, not the override. A model estimate should include sample count/window
in its evidence. Clearing an override is a desired/model edit; it explicitly
adopts a new model estimate or reports a missing estimate. No implicit fallback.

Counters are monotonically increasing positive integers within a control and
owner. Desired edits use an expected revision, multi-field saves are atomic, and
failed/retried network requests reuse their transaction/revision. Local mappings
must be persisted before acknowledgement. Accepted and active tuples can lag
current desired values; preserve them as separate snapshots. Do not pretend an
old accepted tuple is acknowledgement of a new request. Historical runtime
reports may reference old tuples; execution requires the exact current accepted
snapshot. Setup readiness is computed, not an editable boolean or method label. Unset
objectives/limits remain null with explicit setup gaps; never insert example
values just to make a pending definition validate.

Planning snapshots include all accepted tuples and energy-model revisions needed
for shared household/grid headroom. Recheck them before publication. A plan
references that snapshot, has UTC start/expiry, and cannot renew its own authority.
The later authority protocol uses a 15-second revision poll and a 120-second
renewable lease bounded by plan expiry. Restart never revives an expired lease.
Permission-off stops SHS locally and invokes the agreed handover immediately;
remote removal remains pending while disconnected. These software bounds do not
claim hardware recovery while HA itself is down.

## Ownership/change matrix

| Change class | Existing plan | Handover | Next action |
| --- | --- | --- | --- |
| Presentation only (name/category) | Remains valid | None | Update display only; association/executor unchanged |
| Planning input (objective, deadline, estimate, inclusion-on) | Old accepted bounded plan may remain until explicit replacement boundary | Only if replacement/guards require it | Validate, acknowledge new desired/model tuple, replace dependent household plan coherently |
| Binding/semantics (method, identity, units, modes, association, group members, relaxed limits) | Block old commands immediately when HA learns the change | Complete with old recorded bindings before reassignment | Persist/revalidate; require new agreement and matching plan |
| Restrictive local limit | New guard applies immediately; interrupt requests outside it | If active request violates new limit | Revalidate, acknowledge binding revision, replan dependent envelope |
| Permission-off/removal | Local off immediate; remote off pending acknowledgement or authority expiry | Contract-specific release/normal profile | No new execution until explicitly reauthorized |
| External override | Yield affected control; invalidate its authority | Respect manual-change policy; never fight the user's write | Report override; explicit resumption and matching plan |
| Live observation | No definition invalidation | Only when runtime guards require it | Refresh observations, constrain execution/replan as needed |

Handover failure remains visible and retains old ownership. Other controls may
continue only within a still-valid common resource envelope; do not mix unrelated
versions of coupled household plans. External change means a change to requested
actuator state; natural battery flow or thermostat cycling is not a manual edit.

## Battery intent

Every command specifies both non-negative W ceilings. Forecast electrical power
is a separate signed estimate (positive charging, negative discharging); it is
never used to reconstruct intent or set the ceilings. Local conversion to kW and
step rounding belongs at the actuator boundary. The fixture ratings/profile are
illustrative and do not commission this installation.

| Intent | Sigenergy policy | Additional command constraints |
| --- | --- | --- |
| `self_consumption` | Maximum Self Consumption | Both ceilings explicit; discharge serves house demand |
| `solar_charge` | Maximum Self Consumption | No grid charging; discharge may serve the house unless ceiling is zero |
| `charge_pv_first` | Command Charging (PV First) | Grid charging allowed by objective; discharge ceiling zero |
| `charge_grid_first` | Command Charging (Grid First) | Grid charging allowed; discharge ceiling zero; surplus PV contribution not promised |
| `discharge_pv_first` | Command Discharging (PV First) | Explicit battery-export permission; charge ceiling zero |
| `hold` | Standby | Both ceilings zero; physical PV behavior requires commissioning |

Supported intents are advertised locally and only commissioned scope may become
executable. SOC boundaries, export reserve, hardware floor, ratings and current
household headroom are guards. Ceilings never guarantee delivery. Handover restores
reviewed valid charge/discharge limits and Maximum Self Consumption. Reject the
unset sentinel, ESS First and inverter active-power adjustment mappings; do not
write whole-plant import/export/PV limits as part of this contract.

## Customer-operated pool service

**Boundary agreed with Phil on 10 September:** the customer automation owns all
Nibe register writes, pump actions, ordering, trigger handling and physical
handover. SHS owns the planning objective, versioned request, authorization and
honest reporting. SHS does not implement a general dependency engine or inspect,
reserve or command the automation's internal actuators. The same boundary applies
to Node-RED and HA automations.

The customer provides a dedicated HA script interface with a stable registry
identity, and a feedback sensor. The script accepts a complete request object;
SHS must not implement the request as a sequence of private Nibe/pump writes.
Step 2 validates the interface and step 6 implements transport/acknowledgement.
A configured script's existence is not proof that its service works correctly.

The adapter invokes the script with a single `request` field containing control
ID, accepted tuple, request ID, durable per-control request sequence, plan ID,
`heat|defer|release`, objective start/stop Celsius
(for heat/defer), and authorization expiry UTC. The customer interface must be
idempotent by request ID, reject expired/out-of-order sequences, retain the active
request's expiry and yield to its reviewed normal behavior on expiry/release.
Request expiry is at most 120 seconds after issue and no later than the parent
plan expiry. Release uses a null plan ID. Renewals advance the durable sequence
and explicitly extend expiry for the same accepted instruction; it cannot revive
a superseded definition. The script must be able to return/emit acknowledgement
of that request independently of observations that heating actually happened.
Expiry handling inside the customer's automation belongs to that automation.
If the interface cannot meet these conditions, setup remains incomplete. Script
payload and feedback are the `poolRequest` and `poolFeedback` definitions in
`schema.json` (see the customer request/release/feedback examples).

`heat` asks the automation to pursue the desired normal band. `defer` allows it
to defer heating within the locally advertised operating range; a lower-bound
thermostat call may still cause heat. `release` relinquishes SHS scheduling and
lets the customer automation resume its defined policy. It is not a pump-off or
heat-off command. Changing the website objective changes the full accepted band,
not a stale captured Nibe band. Request targets must fit advertised bounds/steps.

Feedback distinguishes accepted request, scheduled state, observed heating,
limited deferral, unavailable heat and release/failure. Correlate request ID and
accepted tuple; preserve sample timestamps. Script completion is not measured
heat. If no physical feedback exists, report unknown/unavailable observation;
do not manufacture a successful heating or stopped result. Missing request,
feedback, objective, bounds or temperature freshness is a visible setup item.

Consumption may include pool-attributed heat-pump electrical input and separately
metered circulation, once each. Those observations do not grant SHS ownership of
the pump. Room floor heating remains associated with a room even when categorized
as pool heating. Actual installation evidence and unresolved setup are in
[pool-installation-evidence.md](pool-installation-evidence.md).
