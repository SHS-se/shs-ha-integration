# Control continuity: only the select releases

**Normative, 21 September 2026.** Implemented for the scheduled controller (pool,
EV, and generic room and hot-water devices) in 0.9.0-beta.37. The battery runtime
does not follow it yet; see [the last section](#battery-runtime-not-yet-conforming).
Where other documents describe handover on restart, on expiry or on faults, this
document takes precedence.

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

## Battery runtime (not yet conforming)

The battery runs through its own runtime (`battery_runtime.py` and the
`home_runtime.py` reducer), which still releases to Maximum Self Consumption:

- on HA stop and on integration unload or reload (`close(release=True)`);
- when its measurements are stale, or its plan contract expires or changes
  authority: the reducer withdraws execution, and an owned battery without a
  fresh target is given the release target;
- on any options change, which rebinds its authority, and on refresh errors.

Holding the battery's last command while it cannot see live measurements is a
decision to make first. For example, a held grid charge cannot see the house load
rise and could exceed the main fuse limit, which the release currently prevents.
