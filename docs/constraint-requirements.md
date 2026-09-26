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
