# Pool heater switch execution

The configured Control entity is the pool actuator. The integration calls only
`switch.turn_on/off` or `input_boolean.turn_on/off` for the pool. It never reads
or writes hardware start/stop temperature registers and has no second accessory
permission switch.

The planner owns the heating schedule (`slot.pool_w`). The website's **Preferred
level** is an economic preference: inexpensive heating may carry the pool above
that level. Its **Stop at** temperature is the upper cutoff. The planner publishes
that cutoff as `plan.pool.stop_temperature_c` in each applicable plan projection,
using the pool utility curve's zero-value endpoint, and uses it as its dispatch
ceiling. A curve without that endpoint publishes null; pool execution reports
that a temperature is missing rather than guessing one.

The controller reads the configured water sensor (including existing filtered
sensor source/freshness checks). It requests the switch **on** only when the plan
requests heating and water is below Stop at. Otherwise it requests **off**. Equality
is off. It adds no hysteresis, temperature writes, or local price decisions.
The equipment or the user's HA switch implementation owns how permission becomes
physical heat. A successful switch command does not prove heat delivery.

## Calls and ownership

```python
request, sources, fresh_until = controller.pool_request(options, slot)
await controller.capture("pool", options, [request["control_entity"]])
await controller.command(request["control_entity"], request["requested_switch_state"])
# On leaving control:
await controller.restore("pool")
```

The same pool owner selection serves the card and controller. Its existing
`device_control_mappings[key].actuator_entity_ids` contains exactly one switch.
`pool_request` reads and validates that binding, the explicit plan cutoff, and
the measured temperature. Execution, preview, and verification share this request.
The existing controller lock and journal remain the sole command owner.

Verification records proposed switch commands and simulated handover without
writing or capturing real ownership. Entering Controlling captures the actual
switch state before issuing a command. Leaving Controlling, changing the mapping,
or losing plan authority restores that captured state: initially on returns to on;
initially off returns to off. A mapping change restores the old entity before
acquiring the new one. Restarts restore through the saved journal.

Old temperature-controller records are retired without restoring their number
values. Any recorded on/off permission is restored, and the saved temperature
values remain in a notice for manual inspection. Old start/stop/permission fields
are removed by the existing options migration.

## Setup and explanation

The pool card keeps the existing control editor and shared water sensor. It no
longer offers start/stop registers or thermostat levers. Shared validators check
the switch binding and water sensor, carry errors to their actual editors, and
block permission when setup is incomplete. Cards, sensors and diagnostics report
the measured temperature, Stop at cutoff, requested switch state, and plain-language
reason. Verification wording describes what would happen.

## Design decision

Independent Codex and Claude candidates both recommended replacing the dedicated
pool adapter's actuator translation while keeping its service identity. This
implementation takes the temperature-aware request from the Codex candidate and
shared setup validation from the Claude candidate. A display-only temperature was
rejected because the requested controller must compare measured temperature.
Moving pool execution into the generic device adapter was rejected because it
would also change planner commands and permission identities without helping this
change. Using Stop at preserves the planner's existing preference semantics;
using Preferred level as a hard cutoff would require a separate economic change.

Regression coverage exercises initial switch states, scheduled and deferred slots,
temperatures below/at/above the cutoff, verification without writes, restoration,
legacy journal retirement, field correction/navigation, and planner contract output.
