# Hot-water permission and heating completion

User requirement, 14 October 2026: execute the planner's binary daily allowance
schedule, completing an active heating run before applying a scheduled block.
This supersedes the measured-house high/low threshold policy.

The planner sends `permit_inhibit` true or false. On an inhibited quarter, an
observed on permission with heater power above 500 W remains on until power is
strictly below 500 W. Exactly 500 W retains permission. An already off device
is not started by this rule. Power changes trigger evaluation within the same
quarter; observed switch state reconstructs completion after restart, without
adding a timer or ephemeral-only latch. A later allowed quarter simply allows.

Use the device mapping's actual instantaneous heater power sensor (W/kW).
A configured fixed rating cannot detect completion. Setup/readiness corrections
name the device Power editor, not the house-consumption field. A shared meter
must not be presented as isolated heater demand without evidence.

Unavailable readings hold the last setting under existing continuity rules.
The rule applies to scheduled inhibition, leaving explicit mode handover,
manual override and actuator ownership unchanged. Verification observes the same
decision without writes. Live explanations identify finishing heating.

The battery runtime removes water from its measured supply partition unless the
current slot explicitly grants `boiler_battery_allowed` from forecast replaceable
surplus. This remains true in a blocked quarter while heating finishes.
The controller's extra consumption remains measured deviation; it does not
create a planner tank model or a heating-energy guarantee.

The generated producer fixture is tested with the real HA contract validator.
See the [planner design](../../smart-home-solutions-t-by/docs/energy-optimisation/hot-water-demand-control-2026-10.md).
