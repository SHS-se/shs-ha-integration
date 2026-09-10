# One-way migration specification

This is the reviewable migration design for step 1, not an applied database or HA
options migration. It must run only during the negotiated rollout after the new
API and local validators exist. The current execution path remains unchanged by
this contract bundle. No legacy executable fallback is introduced.

## Inventory and persistent identity

1. Snapshot each home's existing meter IDs, statistic IDs, timestamps, samples,
   category, explicit inclusion, control type, energy-model choices/overrides,
   room/EV/store associations, local mapping and ownership journal. Keep this
   snapshot read-only for comparison. Do not copy credentials into reports.
2. Allocate one server control ID per logical job, persisted in a unique
   `(home_id, migration_id, legacy_control_key)` mapping. Retries reuse that row;
   never regenerate IDs from a name, category or current entity ID. A composed
   service can have several old meter keys linked to one control. Keep an alias
   row for each old control key to that same control ID.
3. Allocate source IDs referencing existing meter/statistic keys. Update no
   historical key, sample, start/end time or total. Meter entity renames are
   aliases to the same persisted source; record continuity rather than rewriting
   history. Monitor-only meters do not need a control to keep ingesting/history.
4. Record explicit planning associations using server IDs and an installation
   mapping from local room registry area or EV/store identity. Do not expose raw
   actuator IDs in website requests. Do not derive future routing from category.
5. Produce `migration` report rows, with preserved statistic keys, explicit
   association (or null), and field-specific gaps. Report source evidence for
   every inferred route. A `mapped` row means its association is known; it is
   not execution readiness, a capability acknowledgement or a permission grant.

## Route decisions

| Existing evidence | Association/contract decision | Missing/ambiguous outcome |
| --- | --- | --- |
| Setpoint mapping with explicit room area (any category) | `room` + `temperature_target`, preserve objective and estimate | `room_association_missing` if area unresolved |
| Switch schedule in heating/cooling with one explicit room area and room objective | `room` + `relay_schedule` | `room_association_missing` / `objective_missing`; do not convert to timed run |
| Variable-power EV mapping with explicit vehicle/charger association | `ev` + `adjustable_output` | `ev_association_missing`; do not infer a car from category alone |
| Permit/inhibit with an identified hot-water service and objective | `hot_water` + `permission` | `service_association_missing` |
| Generic relay with explicit duration/deadline objective | `timed_load` + `relay_schedule` | Preserve inclusion; `objective_missing` |
| Battery store configuration with SOC/measurement references | `battery` + `battery_dispatch` | New intent/dual-limit binding, normal profile and validated scope required |
| Existing pool heater/pump route tied to the same pool store | One `pool` + `pool_service`, preserving separate meter links | `customer_interface_missing`; old hardware bindings do not become customer interface bindings |
| Category says pool but setpoint belongs to a room | Preserve explicit `room` association | Category cannot promote it to pool service |
| Only category or similarly named entity available | No automatic association | `association_unresolved`, keep monitoring and explicit inclusion pending |
| Two candidate controls own the same registry actuator/group member | No accepted new owner until resolved | `actuator_ownership_conflict`, identify both control keys and local roles |

A known legacy route can seed an explicit association only when supported by the
saved control method and service/room/store identity. Preserve explicit choices
including `false` and zero; do not replace them with inferred defaults. No plan
should silently drop an included unresolved device: show pending setup, retain
its observations/base-load accounting, and block/rebuild affected electrical
plans where old allocation cannot remain valid. Pure category edits after
migration never change the association or execution method.

## Accounting invariants

- Every original statistic key still exists with identical historical samples.
  Assert before/after sets and sample hashes/counts in migration tests. The
  migration writes links/metadata only; no UPDATE/DELETE of historical samples.
- Each electrical component enters the household balance once. Choose canonical
  sources by evidenced lineage, not matching current values. Alias statistics,
  utility-meter resets and aggregate totals are observation-only when their
  constituents are counted. Never add a parent meter plus its children.
- Shared heat-pump electricity needs a declared allocation: pool, room and hot
  water fractions partition one canonical plant input, or a verified disjoint
  priority-gated series supplies that partition. Do not sum allocated series
  and full-plant electricity. Unverified overlap becomes
  `shared_energy_allocation_unverified` and blocks model acceptance.
- Thermal produced energy is not electrical input. COP/conversion must have an
  explicit model/provenance. No unit conversion is inferred from a name.
- Keep pump electricity as a source link even though the customer owns the pump.
  It is not a second SHS pump executor. Account for filtration/baseline operation
  separately from incremental heating demand when constructing the later model.
- Reconcile historical wiring/source-change boundaries explicitly. A new source
  starts a new provenance/allocation interval; it must not rewrite earlier
  history or pretend that old electric-heater measurements were Nibe electricity.

The fixture suite rejects duplicated counted sources and counted aliases. It
cannot establish physical equivalence of differently named meters. Step 3/4
migration tests must compare real persisted data and enforce home-scoped keys,
RLS/authorization, idempotence and transaction interruption semantics.

## Local binding and ownership cutover

Resolve saved entity names to exact registry identity once while retaining local
evidence. If removed/replaced or ambiguous, report `registry_mapping_required`;
never search for a similar name as a substitute. Persist the new mapping proposal
before acknowledging it. New capability/contract acceptance starts with no live
lease and no new permission grant. Preserve existing permission intent as an
unacknowledged setup choice where required; never turn an old disabled control on.

Old signed battery power and mode fields are not reinterpreted as ESS limits.
Do not carry the inverter adjustment into the new control. Retire those fields
only after old ownership has been released by the old code/journal procedure.
If a journal holds old signed-target values or an invalid restoration sentinel,
block cutover with `legacy_handover_required`; review a valid release/update
procedure before replacing software that knows that journal format. Failed or
pending restoration stays durable and visible. Never discard the journal to
make migration succeed. The new controller must not execute old plans.

For the pool, remove SHS ownership of direct Nibe bands, Nibe permission groups
and circulation only through that same reviewed release/cutover procedure. Keep
the customer's Node-RED/HA automation running as the hardware owner; do not
suspend or replace it as if it were a competitor. Bind its dedicated request and
feedback interfaces. Do not migrate `companion_actuator_entity_ids` into the new
pool control. Another independently configured generic SHS control that still
owns these actuators must be explicitly removed/reassigned before commissioning.
The schema does not reach inside customer automation to discover dependencies.

## Rollout transaction

Server stores the desired proposal and local binding proposal separately;
acknowledgement identifies their exact tuple. Install backend schema/validation
first, then the HA implementation and UI/planner changes in negotiated order.
Unsupported executable versions fail explicitly. Keep historical telemetry
contracts available for observation where required by the rollout, but never
reinterpret an old executable battery/pool command as a new one.

Publish a plan only from a consistent accepted household snapshot; discard it if
any required accepted or planning-model revision changed during calculation.
Do not grant a lease to unresolved controls or resurrect a lease after restart.
Retry migration/save/acknowledgement transactions without reallocating identity
or incrementing revisions for network failures. The later applied migration must
prove these properties with interrupted-save/retry and cross-home access tests.
