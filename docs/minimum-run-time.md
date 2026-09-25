# Minimum run time

Each appliance control card offers **Minimum run time** under **Add a setting**.
It is hidden until added, accepts minutes, and is optional for switch schedules,
permit/inhibit controls, thermostats, and variable-power controls. An absent or
zero value means no minimum run time. Battery charge/discharge operations are not
appliance runs.

Once a device is enabled, SHS cannot stop it before the configured time elapses.
The same command guard covers ordinary plans, replans, temperature and charge
targets, exclusions, overrides, and restoration. Removing or shortening the
setting during a run does not cancel the duration already promised. A replaced
actuator gets its own run history while the old actuator retains its promise.
The deadline allows a stop; it does not itself require one.

The controller journals real starts separately from control ownership. It saves
an intended start before calling Home Assistant and retains elapsed time across
integration reloads, HA restarts, and upgrades. Unknown observations preserve the
journal. Real observed off/on transitions start a new run in receipt order.
A switch or permission entity is enabled when on; a power control is enabled
above zero. A climate entity is enabled in its active HVAC mode. A numeric
thermostat target is enabled above its measured room temperature. SHS holds
thermostat target reductions during the protected period.

Verification observes real device transitions without sending commands. Moving
from verification to controlling uses that same run history. Moving from
controlling to verification relinquishes ownership without restoring or changing
the device, including when a handover was already pending.

Snapshots carry the configured duration, current enabled state, and remaining
seconds in each device model's `minimum_run`. Planner v45 reserves ongoing runs
before economic allocation, counts their energy and heat, and considers minimum
run lengths when choosing future starts. Fixed-plan suffixes inherit unfinished
runs. Durations use actual slot lengths, including the partial first slot.

This feature spans both repositories: publish the planner changes alongside the
integration beta. The integration's local command guard enforces the deadline;
the planner changes make the planned consumption and scheduling reflect it.
