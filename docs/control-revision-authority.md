# Revision agreement and execution authority

Step 3 adds the control-v1 agreement path in both repositories. It does not enable
controls or deploy the database/edge function. Step 4 connects the ordinary website
editors and the optimizer's battery/pool instructions to this path. Step 5
connects the [battery adapter](battery-execution.md); step 6 connects the
[customer pool request interface](pool-execution.md) to its execution guard. Existing schema-7 statistics,
monitoring and ownership recovery remain on their existing path until the explicit
migration. They cannot be used to execute a control-v1 plan.

## Settings and persistence

The dedicated authenticated `control-agreement` edge endpoint exchanges settings
and authority every 15 seconds, independently of statistics, recorder work and
replanning. Only one request per HA entry may be in flight. Network failure leaves
revisions unchanged and cannot extend a lease. An error appears in the HA panel.

The server holds one `energy_control_agreements` document per home. A service-only
compare-and-swap RPC commits all related fields atomically. Website requests use
the caller's subscriber/staff home access, not a body-supplied customer ID. Device
requests use the authenticated token's home. Subscriber reads are protected by RLS;
subscribers cannot directly mutate the table or call the service RPC.

`edit_revision` protects the complete browser edit, including presentation fields.
The server assigns desired and energy-model revisions. A semantic edit advances
`epoch`, which invalidates authority renewal for the whole coupled household.
Presentation edits advance only the edit revision. This deliberately conservative
first implementation gives every control the same household/grid dependency group;
there is no partial replacement of coupled commands. A source-accounting change
also changes the epoch. Removal retains an identity tombstone.

HA advertises only persisted binding revision, semantic capabilities, reviewed
limits, handover and setup gaps. Entity IDs, registry identities, automation
internals and actuator ownership stay local. Availability changes do not invent a
binding revision. Reusing a binding revision with different semantics is refused;
delayed lower revisions cannot replace a newer binding. The first paired device
that supplies bindings owns the home revision space; another device token cannot
silently take it over. Re-pairing needs a reviewed server-side ownership transfer.

HA validates the complete desired/local composition and persists desired settings,
the epoch and accepted tuples using the same verified atomic storage mechanism as
local binding setup. Only a subsequent poll sends that persisted acknowledgement.
An interrupted or failed save cannot publish a new acknowledgement. A lost reply
can be retried with the same tuple. Server acknowledgement separately records that
the server received it. Receipt of removals is acknowledged at the household epoch.

## Plans and authority

The server's `generateControlPlan(db, homeId, solve)` entry point captures the
accepted definition before invoking the solver and checks its epoch, accepted
revisions and publication revision again in the publication transaction. Step 4
must supply the actual v1 solver here. No translation from legacy signed watts is
provided. Every command carries its accepted tuple, and every included control
must appear exactly once. A concurrently completed newer publication supersedes
an older solving run. Publication retries with an identical plan are idempotent.
Retired plan IDs are retained through their validity windows to reject replay.

HA must receive and validate the plan before requesting authority for its ID. The
server grants at most 120 seconds, ending no later than plan expiry. It cannot renew
a removed control, old binding, obsolete desired epoch or superseded plan. The last
grant remains visible after a remote edit as `change_pending_receipt_or_expiry`.
This is a pending remote change, not a claim that disconnected hardware stopped.

HA revokes local authority as soon as it receives a changed epoch, even if saving
the new definition fails. A local binding/capability change is checked at every
guard. Each adapter must call `ControlAgreement.guard(control_id, accepted,
plan_id)` immediately before every write and after every await. The guard returns
the exact command, checks current registry-backed capabilities, the local permission
revision, current plan and accepted tuple, UTC expiry and a monotonic deadline.
Permission-off or a changed permission revision revokes authority immediately at
the guard. The request-dispatch monotonic timestamp conservatively accounts for
network delay. Clock jumps greater than five seconds revoke the lease; a server
clock outside the request interval plus five seconds is rejected.

No lease, plan, permission or active-operation report is restored from the agreement
store. Startup restores definitions/accepted revisions only and must establish a
fresh agreement and lease. The default permission provider is off; this step adds
no enablement API. A lease is software authority, not evidence of a hardware action.
Server active state is an explicit, timestamped HA report, never inferred from plan
publication, acceptance or lease renewal. Without a report it remains unknown.
The adapter reports each observed control under `active.controls[control_id]`,
including its accepted tuple, acknowledgement and its own observation timestamp.
Pool reports separately identify the customer operation, request ID/sequence,
feedback sample time and request authorization expiry. A battery report cannot
imply that the pool is operating; the portal checks each control independently.

The 120-second bound is a software guard while HA runs. It does not promise an
inverter watchdog or completed physical handover. Pool Nibe writes, pump operation,
sequencing, triggers and physical handover remain entirely in customer automation.

## Validation

- Python tests exercise durable-save failures, interruption after save, restart,
  local disable/binding changes, request overlap/unload, clock jumps, delayed and
  expired responses, mismatched command tuples and incomplete household plans.
- An optional cross-repository test runs the actual Python agreement against the
  actual TypeScript server transitions, including a reply lost after server commit.
- TypeScript tests cover atomic edits, stale tabs, delayed bindings/acknowledgements,
  removal, plan replacement, publication races, lease bounds and CAS contention.
- `scripts/test-control-agreement-sql.mjs` in the website repo executes the migration
  in isolated PGlite PostgreSQL and verifies stale-writer rejection, RLS, home
  isolation and service-only writes. It takes a locally installed PGlite module
  path; it never connects to a deployed database.

Deploy the new migration and endpoint before HA polling is installed in step 7.
There is no fallback from this endpoint to the statistics upload or legacy execution.
