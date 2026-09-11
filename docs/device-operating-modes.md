# Device operating modes and control verification

Each equipment card has one mode selector on Schedule. Devices contains setup fields only:

| Mode | Collect readings | Include in optimisation | Record proposed commands | Call actuators |
| --- | --- | --- | --- | --- |
| Monitoring | Yes | No | No | No |
| Planning | Yes | Yes | No | No |
| Control verification | Yes | Yes | Yes | No |
| Controlling | Yes | Yes | No | Yes |

The website still supplies each device's planning method and inclusion contract.
Local modes restrict that contract; they cannot activate a device excluded on the
website. New devices default to monitoring. Pool water heating and EV charging
have one physical system controller each; other meter cards retain their own
participation choices. A device whose command method is unsupported cannot be
controlled merely by selecting a mode.

The integration-wide planning selector is removed. Planning runs when at least
one device requests a planning, verification or controlling mode. Internal
`planning_mode` and system `*_control_enabled` status values are derived from
`device_modes`, not independently persisted permissions. Old permission flags
are imported once by config-entry migration 12 and then removed. A previously
disabled global planner never becomes live through migration; existing live
planning and authorised current controllers preserve their modes. Retired,
incompatible controller permissions remain disabled.

Changing participation requests a fresh plan. Moving between planning,
verification and controlling does not reload the integration or reset unrelated
controllers. Leaving controlling first restores settings still owned by SHS;
that handover can make real service calls and can remain pending on an error.
Verification begins only after the previous ownership has been released.

## Shared verification file

Use **Schedule → Control verification → Download control verification**. The
admin-only download contains all devices in the integration entry in one JSON
file. Its persistent HA storage journal is
`.storage/shs_energy.verification.<config_entry_id>`. It is separate from the
restoration journal and is never uploaded to the website.

Each attempt contains:

- UTC attempt and command timestamps, plan ID, issue time, schema, and slot start.
- The full plan slot for comparison with the controller requests.
- Exact Home Assistant domain, service, entity and value, including no-op targets
  marked `would_call: false`.
- Observations used during validation, their report times, and refusal reasons.
- A configuration scope with the integration version; full options are stored
  once per scope in `configurations`.
- Separate `plan` and hypothetical `handover` command phases. Handover means
  “if control stopped now”, not a scheduled action at that timestamp.

Verification invokes the real execution methods and intercepts service calls
after command validation. A private in-memory actuator view lets later commands
see earlier proposed values without changing Home Assistant. It never saves a
restoration record or reports physical confirmation. Identical attempts within
the same plan slot are deduplicated; changed decisions and subsequent slots are
recorded. The newest 20,000 attempts are retained, with a discarded-attempt count.
Download periodically if you need to retain a longer commissioning history.
The file contains local entity IDs and configuration and should be shared as a
commissioning artifact, not as redacted diagnostics.

## Coverage and limits

The export gives covered/total operation counts and explicit observed/missing
operations for each configuration and integration version. The operation
catalogue contains all six battery intents plus handover; pool heat/defer plus
handover; EV charge/stop plus handover; and the supported per-device setpoint,
on/off and permit/inhibit operations plus handover.

Only successfully generated branches count. A blocked command does not increase
coverage. A deferred or failed hypothetical handover remains missing even when
the plan operation succeeds. Retention removes corresponding old coverage too.

This is command-generation coverage, not exhaustive code coverage. It does not
prove physical response, every numeric boundary, fault recovery, cross-slot
relay timing, or exclusive ownership against external automations. In particular:

- Battery verification cannot prove that the inverter accepts remote writes or
  actually charges/discharges within the requested ceilings.
- Pool verification cannot prove water flow, heat delivery, or the thermostat's
  behaviour after a shifted band. Reviewed bounds can prevent full deferral.
- Freshness checks still apply, including battery direction observations. A
  derived binary sensor that only reports on changes can become stale.
- Restart, disable, expiry and mapping-change handovers rely on a running HA
  process and reachable devices. There is no independent hardware watchdog in
  this integration to release settings while HA is down.

Review a representative file against its plan before selecting controlling,
then commission physical response and handover on the installation. Keep the
local equipment's safety protections active.
