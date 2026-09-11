# Device make/model catalog — requirements

Status: incremental design, started 11 September 2026. This is a new requirements
record, not a revival of the rolled-back catalog design or approval to implement
a catalog. Add decisions as concrete device cases become understood.

## Agreed requirements

The integration needs device knowledge that distinguishes what equipment can
measure from how it accepts commands. Customers should select their equipment;
they should not have to reconstruct its electrical model with configuration knobs.

A future catalog must distinguish the vehicle, charger and integration providing
the control. An amperage command exposed by a vehicle is not necessarily a command
to the wall charger. Make/model alone may not identify the available controls.

Controls and Planning remain two stacked sections. Controls contains sensor and
actuator connections and operating limits. Planning contains model properties and
planning preferences. Pool water temperature has one selector in Controls; the
pool model uses that same source. Pool volume belongs in Planning. Excluded
devices are hidden initially and can be shown with the page toggle.

## Current European AC profile

Use internal constants of three phases and 230 V phase-to-neutral for the currently
supported European AC current-controlled charging profile. Estimated input power
is `3 × 230 × current`, or **690 W/A**, assuming approximately unity power factor.
At 5 A this estimates 3,450 W; at 16 A it estimates 11,040 W.

These are internal defaults, not customer-editable options. Remove the phase-count
and phase-voltage fields, their public configuration keys and migration paths that
restore them. Retired saved values must not override the profile or reappear after
an upgrade. The internal planning contract may carry the resulting electrical
parameters; that does not make them customer settings.

This is a supported-profile assumption, not a fact that all European homes,
chargers or cars use three phases. Tesla's European Wall Connector documentation
explicitly supports single-phase and three-phase installations. Its specifications
list 230 V phase-to-neutral for single-phase and 400 V phase-to-phase for three-phase
wye. Future coverage of other installations belongs in catalog requirements,
without restoring these customer options.

Sources: [Tesla European wiring guidance](https://energylibrary.tesla.com/docs/Public/Charging/WallConnector/Gen3/Install/3PT2/EMEA/en-us/GUID-84A0811C-F362-4026-BF76-0C737A489F4B.html)
and [Tesla European specifications](https://energylibrary.tesla.com/docs/Public/Charging/WallConnector/Gen3/Install/3PT2/EMEA/en-us/GUID-0300D171-3BA7-4A8A-BB60-019AB9E31BD5.html).

## First concrete case: this Tesla Model Y installation

- Measured charging power: `sensor.tesla_model_y_charger_power`.
- Command: `number.tesla_model_y_charge_current`, in amperes.
- Supported commands in this installation: integer values from 5 through 16 A.
- Use the internal AC estimate to relate planned watts to legal current commands.
  Preserve the command entity's bounds and step; do not send watts to an ampere input.
- Measured watts remain the observed consumption. Estimated watts used for future
  planning are distinct from the power sensor's readings.

These entity names and bounds describe this installation, not every Tesla.
The current change does not introduce learned W/A calibration.

## Future cases and open questions

Some equipment may accept power commands directly. Before implementing catalog
support, establish its actual command units, bounds, step, start/stop behavior and
available measurements. Do not apply AC amperage conversion to a direct power command.

The user may buy a Sigenergy DC charger next year. Record this as a future case;
there is insufficient information to implement it now. Do not model its DC output
using `3 × 230 × current`. We still need its exact model, integration interface,
command semantics, measurements and conversion-loss behavior.

Catalog selection/discovery, profile identity, installation variants, supported
integrations and behavior for unsupported equipment remain undecided. No catalog
schema, automatic device probing, DC controller or speculative compatibility layer
is authorized by this document.
