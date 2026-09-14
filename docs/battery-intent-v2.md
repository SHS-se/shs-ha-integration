# Basic battery intent execution

Battery command version 2 (planner v32, HA 0.8.0-beta.81) makes the small solar
capture correction without implementing the full household policy design.

| Plan operation | Configured mode | Charge ceiling | Discharge ceiling |
|---|---|---|---|
| Solar capture | Baseline / Maximum Self Consumption | Rated charging power | 0 |
| Grid replenishment | Charging | Planned charging power | 0 |
| Supply house | Baseline / Maximum Self Consumption | 0 | Planned discharge power |
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
forecast. Supply-house limits, hold, replenishment and export retain their
existing intent. Solar capture is still selected only when the planner allocates
solar charging; zero-forecast opportunistic capture and independent economic
house-supply permission require further planner work.

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
