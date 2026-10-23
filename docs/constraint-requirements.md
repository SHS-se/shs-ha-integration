# Requirements for planner and controller constraints

User requirement, 17 September 2026. This decision supersedes earlier proposals
for invented validity bounds or timestamp-based event ordering.

Do not invent arbitrary validity constraints. Do not add or restore such a
constraint unless the user explicitly states it as a requirement. Statistical
confidence, forecast error, assumed timing or an implementation convenience is
not authority to introduce a rejection condition. Every new validity constraint
must identify the explicit requirement it implements.

Base-load forecasts estimate future quarter-hour energy demand for economic
planning. They do not specify what instantaneous house consumption is allowed
to be. Base-load confidence bounds have been removed from calculation, snapshot
and plan contracts, validation and forecast storage. Do not reintroduce them,
rename them as another admissibility range, or replace them with a different
prediction-based gate without an explicit user requirement.

The controller uses measured instantaneous consumption, solar production and
battery state to execute the planner's strategy under the
[plan-execution and deviation-accounting specification](controller-plan-execution.md).
The planner owns economic decisions and recovery authority. Energy deviations
are accounted for; they are not new forecast-validity gates or grid-use caps.
An ordinary difference between forecast and measurement calls for a different
action when appropriate; it must not cause the controller to give up solely
because the prediction was wrong. Unmodelled appliances remain part of measured
house demand.

Process events in the order received. Our own locally generated revisions may
track that order and identify superseded work. Timestamps from devices or other
sources must not be used to infer event ordering or reject a later received
update as time going backwards.

User requirement, 23 September 2026: realistic measured state beyond a desired
target — a car above its charge limit, a pool warmer than its stop temperature,
a pack below a raised cut-off, an empty car, a departure beyond the horizon — is
neither infeasibility nor invalid input. A reading no device can produce isolates
only its own device and is named for both interfaces; it never discards otherwise
valid household planning. See
[measured state and impossible readings](authoritative-plan-contract.md#measured-state-and-impossible-readings).

Configured equipment limits, user permissions and explicitly required data or
command checks retain their stated purpose. Do not turn them into a general
exception permitting invented checks, or confuse physical capability with
statistical prediction. Prefer removing unnecessary validation and state over
adding compensating mechanisms around it.

User requirement, 4 October 2026: an EV mapping's saved Minimum value and
Maximum value define the operating range of its Power or current control
entity. The entity's live `min` and `max` attributes do not invalidate that
configured range or block household planning. In particular, a saved 5–16 A
range remains valid when `number.tesla_model_y_charge_current` reports `max: 5`.
Use the configured range in the planning snapshot; retain the unit, step and
command checks for their existing purposes.

User requirement, 15 October 2026: source-report age is diagnostic information,
not a configuration error or an admission gate. An available, valid reading
remains usable even when its source timestamp is old or differs from the other
sensors. Do not require unchanged sensors to report on an invented cadence.
This applies to device execution and verification, battery capture/supply, and
planning inputs, including forecasts and SOC. Retain source timestamps and ages
in diagnostics. Unknown/unavailable readings, invalid values/units, local
transport/session validity, command acknowledgement and plan expiry keep their
existing purposes. Only explicit user settings release equipment.

User requirement, 15 October 2026: imperfect history and model quality produce
nonblocking diagnostic warnings, never missing configuration fields. Preserve
single usable quarter observations; interpolate empty historical profile quarters
and disclose estimates. With no history, use current measured residual demand as
an explicitly uncertain estimate. Five empty quarters elevate severity, not
admission. Incoming ignored-history warnings and plan warnings share the same
attention surface and clear when current evidence is usable.

Disagreeing device and household power meters must not stop the native battery
runtime. Preserve raw excluded demand, reconcile eligible supply within the
existing permission partition, log the discrepancy and show diagnostic attention.
The scheduled device executor no longer executes battery plans or confirms them
against source timestamps. Only the durable native battery runtime may write
battery settings. Explicit mode exit, including Verification, still performs its
native handover; restart, metadata changes, missing plans and data gaps hold the
last settings. Retired writer journals are fenced on load, never re-enabled as an
alternative executor.

The planner installation address owns location. HA has no competing PV coordinate
editors or coordinate mismatch gate. Pool intent transports an explicit permission
separately from forecast watts so missing statistical evidence cannot fabricate an
OFF request. Known physical limits and permission boundaries remain enforced.

User requirement, 16 October 2026: whole-house plan rejection is reserved for
unreadable or ambiguous executable contracts. Admission must not re-run the
planner's economic/service workload decisions or gate on configuration snapshots,
forecast completeness, comparison scenarios, descriptive copies, arbitrary
horizon lengths, statistical values or forecast-to-command equality. A partial
plan's usable instructions remain executable. Per-device command errors hold and
report only that device; current permissions and configured/native equipment
limits remain authoritative. Battery contract, receipt and objective accounting
checks retain their purpose, but a failed battery handover retains its accepted
account and must not discard other devices' replacement instructions. Report
configuration confirmation failures independently of accepting a delivered plan.
Only explicit mode/exclusion/override actions release equipment.

User requirement, 17 October 2026: normal collection of history, model learning
and limited fit observations do not create warning banners. Keep their evidence
in diagnostics and informational logs. Operational failures, including an
unavailable savings comparison, remain visible. Forecast quality is separate
from device permissions and plan admission.

User requirement, 17 October 2026: house import admission credits the
controlled battery's measured discharge, as the planner's grid balance does.
Solar production earns no credit. See
[house import safety](house-import-safety-2026-10.md#one-physical-admission-boundary).

User requirement, 17 October 2026 (evening): the planner states each interval's
battery mode — follow demand, cover demand above an import target, or a fixed
figure that also defends the house limit — and the exact load partition that
defines base load and the battery's eligible demand. HA executes that law and
does not infer membership from its own device list. While a capped instruction
is in force, house import admission also credits the discharge it promises. See
[battery execution contract v2](battery-execution-contract-v2.md).

User requirement, 19 October 2026: heaters need no minimum or maximum
temperature in Home Assistant. The room's comfort schedule on the website owns
the setpoint range and every setpoint command carries it. HA writes the planned
target rounded to the thermostat's own step, halves rounded up: a 22.5 °C plan
on a whole-degree thermostat (such as the BHT-002) is 23 °C. A rounded value
outside the command's range is still written, never rejected; the thermostat's
resolution, not a second local envelope, decides what can be executed.

User requirement, 23 October 2026: an unplugged car retains future charging
planning and truthful cable telemetry. When a scheduled load cannot execute and
its work must move, forced household replanning is mandatory, including for pool
heating, battery charging and other devices. Release only the affected owner's
commitments needed to remove or reschedule the unexecutable work, including its
source and destination slots. All other executable loads retain the normal
next-hour hold (four quarters at a quarter boundary,
including intersecting slots for a partial current quarter). No household-wide
unlock or manual-click requirement is permitted for this case. Preserve actuals,
outstanding service, original deadlines, equipment limits and permissions. See
[execution-failure replanning and selective commitment release](authoritative-plan-contract.md#execution-failure-replanning-and-selective-commitment-release)
for trigger, retry, accounting and acceptance requirements. The implemented
integration owns durable per-owner evidence, automatic capture and the common
receipt/acknowledgement path; ordinary captures retain unresolved faults.
Verification and unchanged failures do not create physical failure requests.
Integration, app replay and browser regressions verify this path; deployment
remains separate.
