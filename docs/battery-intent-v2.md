# Basic battery intent execution

## Superseded correction; retained implementation record — 15 September 2026

Planner v35 and HA beta.94 describe the already-made rating-wide full-house permission correction below. This is not the agreed replacement design or a recommendation to roll it out further. The user selected explicit battery supply scope, measured eligible demand and actual-state economic evaluation. Keep the historical behaviour/version evidence; do not reinterpret current schema-2 commands as carrying scope or undo the change automatically during documentation work.

See the [agreed participation and battery supply specification](device-participation-and-battery-supply.md).
Documentation only; replacement implementation and coordinated rollout remain pending.

Battery command version 2 separates native permissions from forecast power.
Planner v35 and the accompanying HA update extend the solar-capture correction
to full household supply without implementing the full household policy design.

| Plan operation | Configured mode | Charge ceiling | Discharge ceiling |
|---|---|---|---|
| Solar capture | Baseline / Maximum Self Consumption | Rated charging power | 0 |
| Grid replenishment | Charging | Planned charging power | 0 |
| Supply whole forecast deficit | Baseline / Maximum Self Consumption | 0 | Rated discharge power |
| Supply part of forecast deficit | Baseline / Maximum Self Consumption | 0 | Planned discharge power |
| Export | Discharging | 0 | Planned total discharge power |
| Hold | Hold | 0 | 0 |
| Baseline self-consumption | Baseline / Maximum Self Consumption | Rated charging power | Rated discharge power |

For example, a solar-charge forecast of 523.94 W with an 8.8 kW battery rating
produces an 8.8 kW charging ceiling. The inverter can capture a 3 kW surplus and
can stop when there is no surplus or the battery is full. Discharge remains
blocked for that operation; this does not prohibit household grid imports.

The controller applies the explicit ceiling from the plan, checks it against the
current equipment rating and actuator bounds, and rounds down to the supported
step. It does not substitute its own price rule or infer permissions from the
forecast. When the battery covers the complete forecast deficit (within 0.01 W
serialization tolerance), the planner permits native house supply up to rated
power. Deliberate partial supply, hold, replenishment and export retain their
selected ceilings. Classification uses the final household forecast, so loads
added after battery dispatch can still leave a partial, capped operation.

For the 15 September 17:30 case, 833.16 W demand minus 427.56 W solar predicts
405.60 W discharge. That prediction remains in the plan, while the command
authorizes house supply up to 9600 W. The inverter follows actual net demand;
this does not command 9600 W discharge or authorize battery export. Falling
demand or rising PV reduces native discharge automatically.

This correction permits additional discharge between replans. Physical minimum
SOC remains protected; an economic reserve for dearer future demand is not
guaranteed by matching the full forecast deficit. Deliberate partial operations
remain capped. Actual-state future-cost ranking, zero-forecast opportunistic
capture and automatic charge-timing changes remain in the
[opportunity-cost design](battery-opportunity-cost.md).

Mode names are user-configured hardware mappings. The baseline mode must provide
native self-consumption without forced grid charging or battery export. A
commissioned Command Charging (PV First) mapping can use solar with grid support
for deliberate replenishment; the existing Grid First mapping is not silently
changed. A combined charging ceiling does not express a guaranteed minimum grid
charge plus unlimited additional solar capture. Physical source attribution is
not inferred from the plan or the mode name.

Native regulation is confirmed using mode/limit readback and fresh measured
power within the authorised ceilings. A difference from forecast watts is not
an execution fault for native solar capture, house supply or self-consumption.
Forced grid charging and export retain their physical-response checks.

The schedule shows one concise intent label. Expected flow remains in the plan
and diagnostics; it is not presented as an exact charging instruction. A label
is planned intent, not evidence of the source or amount of delivered energy.

## Rollout

The enclosing plan remains schema 8; the independently versioned battery command
changes from 1 to 2. HA accepts only version 2 commands. Older integrations reject
version 2, and the updated integration rejects version 1. Update both producer
and integration and obtain a fresh plan before expecting battery execution.
Existing fixed plans with version 1 commands must be rescinded and recreated;
they are never silently widened or translated. The provider and consumer tests
use the same regenerated planner fixtures.

No hardware mode mapping, operating permission or deployed installation is
changed by these code changes. Broader reserve/headroom economics and reactive
household control remain in the implementation scope document.

## Historical v35 full house-supply rollout (superseded target)

Install the updated HA validator before deploying planner v35, then request a
fresh plan. Older HA validators reject supply-house forecasts below their command
ceiling. The command remains schema 2 because its fields still express explicit
native ceilings. Existing fixed commands retain their exact limits; HA never
widens an old plan locally. The controller continues to reject forecasts above
ceilings, disallowed directions, requests above current equipment ratings and
discharge at the physical minimum SOC.
