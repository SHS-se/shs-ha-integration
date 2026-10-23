# Authoritative plan and truthful interfaces

User requirement, 23 September 2026. Supersedes contradictory earlier designs.

The website Plan chart and the Home Assistant integration Schedule chart MUST
show the actual selected plan intended for execution, with the same plan identity,
quarters, device requests, energy flows and battery trajectory. An alternative
scenario must never be labelled as the actual plan. Verification simulates the
same requests; controlling permits hardware writes. It must not change bidding.
Historical measurements and controller adjustments are separate evidence, not
substitutes for the selected plan. Any planner/contract change MUST update and
verify both UIs and their shared consumer fixtures in the same delivery.

A UI exists to explain actual system behavior and provide effective controls.
A curve labelled current must be the resolved curve consumed by that plan, not
stored preferences or a reconstruction using newer measurements. Unsaved edits
must be labelled previews. Preserve meaningful controls; remove controls whose
meaning cannot be explained against the actual calculation.

Plans last for their original 72-hour forecast horizon. Routine measurement
exchanges must not rebid or extend that horizon. Automatic bidding happens on a
new published price release, after value curves are regenerated, and MUST also
occur for execution failures under the selective commitment-release requirement
below. Explicit manual replanning remains available on Plan. Other former triggers
retain their recommendation policy; execution-failure replanning is automatic.
A completed replan clears the recommendations it addresses. Verification/control
mode transitions change writer authority only, not the schedule: they request no
plan and recommend no replan, and a plan or battery reference captured under
either mode is accepted and executed under the other.

Realistic state above a desired target is not infeasibility. Invalid measurements
must be isolated to the affected device and explained; they must not discard
otherwise valid household planning. Targets and sensor validity are distinct.

## Replan recommendations

Quarterly telemetry exchanges do not authorize a new solve. Ingest compares the
published price rows with those recorded for the accepted plan; new publication,
an outstanding manual request or a mandatory execution-failure request permits
the next solve. Elapsed quarters simply leave the remaining original horizon
shorter. Forecast padding never identifies
a new price release. The original validity endpoint does not move.

The current row owns recommendation reasons and occurrence times. Both UIs read
that state. Curve/settings and planning configuration writes append a reason;
integration events outside the mandatory execution-failure path submit a
recommendation when the household must act manually.
A successful plan publication clears reasons that predate its solve; an event
that occurred during the solve remains visible. A failed solve clears nothing.

User requirement, 19 October 2026: a recommendation is shown only when the
household must act manually; it is never informational. A battery shortfall the
controller cannot recover before an objective's deadline
(`observed_state_cannot_meet_target`, `shortfall_without_recovery_authority`)
leaves the controller running and raises no recommendation. Under the 23 October
execution-failure requirement below, it MUST also force a replan when remaining
scheduled work is unexecutable and needs rescheduling; otherwise the next normal
plan incorporates the outcome. It is reported by the input-accuracy
`battery` check: the share of stored-energy targets due in the last 24 hours
that were reached, with missed deadlines as its issues. Reasons that leave the
battery without an executable plan (expiry, changed capacity, authority or
permissions, a missing battery contract, a rejected handover) still recommend.

Compare four consecutive completed quarters entirely covered by the accepted
plan. Warn when each quarter differs by more than 50%, or when the absolute
difference of the **rolling four-quarter totals** exceeds 6 kWh. Missing quarters
break the sequence. Pack warnings compare measured energy against the accepted
plan's state at capture time and trigger above 4 kWh. No threshold requests a
solve. Expiry recommends a manual replan and leaves the original endpoint intact.

## Execution-failure replanning and selective commitment release

User requirement, 23 October 2026. This is mandatory design, not an optional
recovery feature. It supersedes price-release/manual-only replanning and blanket
first-hour lock rules specifically for the execution failures described here.
The implementation follows the mandatory ownership and evidence path below.
Deployment remains a separate operation.

### Planning and availability

The planner MUST retain an unplugged car's future charging need and plan charging
opportunities while preserving truthful disconnected cable telemetry. Being away
or unplugged now MUST NOT make the EV unavailable for the entire horizon. Known
away intervals constrain scheduling; where return time is unknown, future
charging is explicitly conditional on connection, not an invented arrival time
or a promise of delivered energy. The executor still requires actual connection
and current permission before charging.

This applies to EV charging, pool heating, battery charging and other scheduled
services: temporary inability to execute does not erase the service objective or
stop otherwise executable household instructions. A satisfied target, thermostat
permission without a demand for heat, or ordinary forecast variance is not by
itself evidence of execution failure.

### Mandatory automatic trigger

When observed absence, equipment unavailability or an evidenced command/response
failure makes a scheduled request unexecutable and requires moving that work or
revising its shared resource allocation, the system MUST request a forced
household replan automatically. It MUST NOT wait for a new price release, a manual
click, four completed quarters of deviation, or a recommendation being accepted.
An unplugged car with only a later conditional charging opportunity does not
trigger merely because it is unplugged now. A car still unavailable when its
scheduled charging becomes due does trigger; local recovery authority does not
make this forced replan optional.

The request MUST name the affected physical owner(s), affected absolute command
intervals, observed cause, current availability/state and relevant outstanding
service. It uses the common capture, solve, publication and exact HA-acceptance
path. Repeated reports of the same unchanged failure MUST NOT create repeated
identical replans; changed availability, failed replacement execution or a lost
remaining opportunity is new relevant evidence. A failed request remains visible
and uses the existing request retry mechanism. Reconnection or equipment recovery
MUST trigger another replan when the remaining accepted schedule needs changing;
otherwise the existing executable schedule resumes under current authority.

### Implemented integration boundary and durable evidence

`shs_core.household.Household` owns the execution journal and serializes it with
snapshot capture, pending submissions, planning receipts and acknowledgements
under the existing exchange lock. `shs_core.execution_replan` merges evidence by
physical owner and absolute missed opportunity, independently of replacement
plan IDs. `DeviceOperations` reports structured inability through
`ScheduledController`; the native battery runtime uses its existing notification
queue so receipt/observation locks never wait on the household exchange lock.
Only Controlling execution can request this forced path. Verification, rejected
candidate handovers and unchanged available sensor timestamps cannot create
physical execution-failure evidence.

The ingest field `execution_replan` contains `request_id` (UUID),
`accepted_plan_id` (UUID), `observed_at` (ISO timestamp), and `changes`. Each
change contains `owner`, `status`, `cause` and absolute `blocked_intervals` with
`start`/`end`. Owners are `$ev`, `$pool`, `$battery`, or the raw planned-device key
from `operating_scope`. Status is `temporarily_absent`, `faulted`, or `recovered`.
A blocked request names its due accepted source interval; recovery has no blocked
interval. This envelope accompanies one captured snapshot and is carried through
its durable submission/job receipt, including lost-reply recovery. A forced
capture must receive a valid planning receipt; telemetry-only acceptance is a
contract failure. This path never impersonates a manual replan request.

Every ordinary or forced capture also carries `snapshot.execution_availability`
for currently unresolved conditions. It excludes recovered rows. A known missed
temporary opportunity blocks that interval only; current disconnection does not
invent a future arrival time. A fault persists until observed recovery and is
not assigned an invented repair time. Neither fault evidence nor inability to
send a command proves a held actuator stopped consuming power. Actual state,
SOC, targets, deadlines and measured delivery continue through the existing
snapshot/accounting fields; there is no second additive missed-energy input.

The journal retains pending evidence across restart and ambiguous transport,
uses snapshot receipt recovery before another capture, and clears only changes
included in the accepted submission. Newer evidence for another owner remains
pending. Failed or superseded jobs retain unresolved evidence for the next
existing exchange instead of repeatedly solving on every controller tick. A
late failed job cannot resurrect absence already observed recovered. A later
missed conditional charging interval is new absolute evidence; another report
or replacement plan ID for the same fault is not.

A successful still-authorised execution resumes an unchanged schedule directly.
Recovery forces another capture if absence or fault suppressed the remaining
accepted work, or its blocked-work capture is still in flight. When a generic command
is unavailable, the executor can read mapped sources without writing settings;
only the specific source previously observed unavailable can establish source
recovery. An unsupported result or an unchanged readable actuator cannot prove a
failed write repaired. Existing controller/pipeline errors remain visible, and
the configuration panel shows automatic replanning and conditional car charging
without asking for a manual replan.

### Preserve the next hour for unaffected loads

The normal hold MUST preserve the HA-accepted commands on
`[capture, capture + 60 minutes)`. At a quarter boundary this is four quarters;
with a partial current quarter, preserve its remainder and every future command
slot intersecting that hour. The hold is a command commitment, not a cooldown
between planning requests. Keep its absolute capture anchor through job retries.

For an execution-failure replan, the planner MUST release only the affected
owner's commitments needed to remove or reschedule the unexecutable work. This
includes both the blocked source interval and destination intervals needed to
move that same load inside the held hour, even if it was previously OFF there.
The affected owner's eligible source and destination intervals may be revised
within that hour; publication records the absolute intervals actually changed.
Every unaffected owner's held commands MUST remain exact,
including OFF requests,
charger currents, thermal targets, battery strategy and permissions. Releasing
one load MUST NOT clear the household prefix, release unrelated battery commands,
or introduce new first-hour work for an otherwise unaffected idle owner. Outside
the held hour, ordinary household optimisation may change the remaining schedule.

The released scope MUST be supported by the failure evidence. If a shared physical
equipment failure affects several owners, identify each affected owner and
interval; it is not permission to unlock unrelated equipment. If rescheduling the
affected load cannot meet its objective while the other holds, equipment limits
and permissions remain enforced, publish the best achievable affected service and
its shortfall. Do not silently relax the other holds or cancel explicit user
bookings; report any booking conflict. A cheaper schedule alone does not justify
releasing a held command.

### Accounting, continuity and explanation

Replacement plans MUST use current measured state and delivered service, carry
outstanding objectives and their original deadlines, and explicitly account for
missed service. They MUST NOT count commands as delivery, add an already
incorporated shortfall twice, or move a missed deadline to claim fulfilment.
Pool comfort and other services retain their own outcome measures; electrical
kWh is not a universal substitute for service delivery.

Until a replacement is accepted, unaffected executable instructions continue.
Existing equipment protections, writer authority, pending-effect accounting and
release semantics remain authoritative. A forced replan is not a handback or a
new permission. Both interfaces MUST explain the blocked request, actual outcome,
forced replan status and affected commitment release, separately from the original
plan. An automatic recovery event is not a request for manual user action.

### Required acceptance evidence

| Trace | Required result |
| --- | --- |
| Unplugged EV with future charging need | Future charging remains planned, connection telemetry remains false, and execution requires connection. |
| Car absent when charging is due | Forced replan without a price release or manual click; only evidenced affected commitments are released. |
| Pool or battery cannot execute scheduled work | Same automatic path and selective release as the EV; unaffected household execution continues. |
| Affected load moves to a previously OFF slot within the held hour | Its required source and destination commitments can change; unrelated owners' commands remain exact. |
| Other loads held ON or OFF in the next hour | Their exact commands survive the replacement; freed capacity does not unlock them. |
| Mid-quarter capture and request retry | Original absolute held intervals and partial current-quarter coverage survive. |
| Repeated unchanged failure, then recovery | No identical replan loop; changed availability replans when the remaining schedule requires it. |
| Replacement delayed or fails | Unaffected valid commands continue; request status and unresolved service remain visible. |
| Remaining service cannot fit without moving another held load | Preserve that load's hold and report the affected objective's shortfall or explicit booking conflict. |
| Replacement after missed delivery or deadline | Current state and actuals are incorporated once; the original miss and deadline remain accounted for. |

## Measured state and impossible readings

Implemented in `marginal-value-planner-v43`.

A store bound limits what a schedule does to a store, not where the store may
be. A car above its charge limit, a pool warmed past its stop temperature, a
pack below a cut-off raised after it discharged, and configured battery targets
below a live cut-off are planned as they are. Only charge that raises a store
further above its ceiling, or discharge that lowers it further below its floor,
is infeasible; the trajectory carries the measured state rather than moving it to
the bound. This applies equally to the auction, the independent scorer, cost
refinement and replan continuity.

A reading no device can produce isolates that device alone: a state of charge
or charge limit outside 0–100 %, a car capacity outside 1–500 kWh derived from
its usable energy, or a pool water temperature outside −5–60 °C. The device
leaves the plan exactly as a disabled capability does, and its historical load
remains household demand. Home Assistant also leaves out a device whose reading
is unavailable, non-numeric or impossible and reports it in the snapshot's
`measurement_issues`. The plan publishes the complete list in
`measurement_issues`; the website Plan page and the Home Assistant panel both
show it. Configuration errors remain setup failures and are not isolated. When
a left-out device reports usable readings again, ingest recommends a manual
replan (`measurement_recovered_<device>`), because plans are kept between price
releases.

In this integration `measurements.py` checks each device's readings before the
snapshot is built and switches off only the affected store for that snapshot,
exactly as the customer's own store switch would. Setup warnings keep reading
the configured options. The panel names the devices the current plan leaves
out, with a link to each source entity, and the affected device's status says
why it has no plan. A battery left out is handed back until a plan includes it;
no replan is requested, because the server recommends one once its reading is
real again. A percentage unit decides how a state of charge is scaled, so a car
at 1 % is not read as full; an empty car is planned with the battery size last
derived from its own readings; a departure outside the horizon, already past or
unset leaves that plan without one.
