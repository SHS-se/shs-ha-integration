# Control continuity: only the select releases

**Normative, 21 September 2026.** Implemented for the scheduled controller (pool,
EV, and generic room and hot-water devices) in 0.9.0-beta.37, and for the battery
in 0.9.0-beta.72 (user requirement, 3 October 2026); see
[the last section](#battery). Where other documents describe handover on restart,
on expiry or on faults, this document takes precedence.

## Rule

- Restarts are normal. An HA restart, an integration update, reload or unload, and
  an entity that becomes unavailable, stops reporting or comes back, MUST NOT make
  SHS change any device's state. Each device keeps operating at the last setting
  SHS sent.
- SHS MUST NOT toggle a switch or hand a device back to its own settings because
  of a restart or an unavailable entity.
- A device's execution-mode select (for example
  `select.smart_home_solutions_pool_heater_execution_mode`) determines its mode.
  SHS releases control only when that select says so.

## What releases control

Releasing hands the device back to the baseline captured when SHS first took
control, and is the only time SHS sends a device its own settings.

| Trigger | Why it is explicit |
|---|---|
| The select set to Verification | The user's choice on the select |
| The device leaves the select: demoted to Monitoring on the website, excluded in HA, its system disabled, or re-admitted after a changed planning choice (which resets it to Verification) | The select no longer offers Controlling |
| A configured manual-override entity turns on | A control the user configured for exactly this |

If the actuator is unavailable when a release is due, the handover waits and
completes as soon as it reports (status `pending`, then `fault` after five
minutes). Returning the select to Controlling before then cancels it.

A genuine external change (the actuator is available but not in the state SHS
commanded) latches the device as `overridden`. SHS sends nothing and leaves the
manual setting alone. Setting the select to Verification clears the latch;
Controlling then resumes from the current state.

## What never releases

| Situation | Behaviour |
|---|---|
| HA restart, integration update, reload or unload | No handover at stop and none at start. Ownership and the captured baseline stay journalled, and control resumes from them. |
| An unavailable or stale reading, actuators included | Hold: nothing is written. `pending` with `unavailable_since` for five minutes, then `fault`, which needs attention. |
| The entity returns | Its own state change starts the next evaluation immediately. |
| An unavailable actuator | Unknown, never an external change. |
| No current plan, a plan with no command for the device, or website choices that cannot be read | Hold, with the reason on the device card. |
| A fault: setup error, rejected or ambiguous service call, invalid plan | Hold and report. Latched until the plan, slot or configuration changes. |
| A configuration change that keeps the device Controlling (other devices, admissions, migrations) | No effect on ownership. |
| A Home Assistant metadata change: an entity renamed, added or moved to another area, another integration reloading, a core setting | No effect on ownership, the battery included (user requirement, 3 October 2026). The gateway refuses commands only until the app has received the new context; it does not revoke the battery writer, so nothing is handed back. |
| A changed control entity | Refused with a fault until the select goes to Verification (which hands back the entity SHS owned) and back to Controlling (which captures the new one). |

When an entity returns, SHS only carries out what the current plan and the select
already require. If the device is already in the planned state, nothing is sent.

## Limits that still apply while holding

- A permit/inhibit device that reaches its reviewed maximum pause
  (`max_inhibit_slots`) is allowed to run for one quarter while SHS keeps control,
  including when no plan is available. This used to be a fault that handed the
  device back until the plan changed.
- Native equipment protections are unaffected.

## Status values

| State | Meaning |
|---|---|
| `pending` (Waiting to resume) | An interrupted reading within the five-minute grace, or a handover waiting for its actuator. |
| `fault` | Needs attention. The device still holds its last setting. |
| `idle` | Holding with no current plan, or with unreadable website choices. |
| `limited` | Running for a quarter after the maximum pause. |

## Removing the integration

Only the select releases. Set each device to Verification before removing SHS so
that it is handed back to its own settings.

## Implementation

`controller.py`: `inactive_status` lists the only release reasons, `hold_reason`
holds on unreadable website choices, `report_gap` reports interrupted readings,
`check_targets` refuses a changed control entity, and `inhibit_limit` enforces the
maximum pause. `async_start` and `async_stop` send nothing. Tests:
`tests/test_control_continuity.py` and `tests/test_pool_switch_gap.py`.

## Battery

User requirement, 3 October 2026: the battery is released only when its control
mode says so. Otherwise it stays in the mode the controller last set. The earlier
exceptions (releasing to Maximum Self Consumption on a restart, a lost app, a
settings change, an expired plan, stale measurements or a refresh error) are
removed.

The battery runs through its own runtime (`battery_runtime.py`, the
`home_runtime.py` reducer and Home Assistant's `battery_gateway.py`). All three
follow the rule:

| Situation | Behaviour |
|---|---|
| The app stops, restarts, crashes or loses its connection; HA stops or restarts; the integration reloads | Nothing is written. HA fences the writer, so no stale command can be sent, and leaves the settings alone. The runtime resumes from them when it returns. |
| No current plan or slot, a plan without battery instructions, an expired plan reference | Hold, status `idle` with the reason. |
| Stale or missing measurements, a failing refresh | Hold, status `limited` or `fault` with the reason. |
| A settings change that keeps the battery Controlling | The new settings are bound in place. Changed meters keep the command journal. A changed control entity is refused with a fault until the select goes to Verification and back. |
| A manual-override entity that cannot be read | Hold. |

What still hands the battery back to Maximum Self Consumption at its rated
limits: it leaves Controlling and Verification (demoted on the website), it is
disabled or excluded, or a configured manual-override entity turns on. If the app
is not connected, Home Assistant completes that handover itself. Returning the
battery to Controlling before the handover finishes cancels it.

Consequence accepted with this requirement: a held command cannot follow the
house. A held grid charge keeps charging at its last limit when the house load
rises or the plan ends, and a held discharge keeps discharging, until SHS can
see and plan again or the battery's own protections stop it. The main fuse is
then protected only by the equipment's own limits.

Tests: `tests/test_battery_continuity.py`, `tests/test_battery_gateway.py`,
`tests/test_gateway_metadata.py` and the session tests in
`app/tests/test_engine.py`.
