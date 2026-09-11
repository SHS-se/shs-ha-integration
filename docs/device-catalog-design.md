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

## Proposal: select the controlling integration

Recorded 11 September 2026. **Discussion only; do not implement integration
selection yet.** Phil proposes selecting which installed integration controls the
battery, EV, heat pump or other equipment, instead of assembling its controller
from individual entity selectors, sign toggles and free-text mode names. This
extends the catalog requirements above; it is not approval for a catalog schema
or a new control implementation.

### Why the current Sigenergy configuration is error-prone

Phil identifies two measurements of the same battery flow:

- `sensor.sigen_plant_battery_power`: positive charging, negative discharging.
- `sensor.sigen_plant_battery_power_inverted`: the opposite sign convention.

A customer can choose either legitimate sensor and then accidentally configure
the sign toggle for the other. The application should understand the selected
source's semantics through a supported adapter, rather than ask the customer to
reconstruct them. These are **measurements**, not writable power commands.
Knowing a measurement's sign does not establish the sign or meaning of an actuator.

`binary_sensor.sigen_plant_battery_charging` and
`binary_sensor.sigen_plant_battery_discharging` report direction explicitly and
are candidates for adapter-managed observations. They are not options that can be
written to `select.sigen_plant_remote_ems_control_mode`. Nor should they be assumed
to be independent physical evidence: the adapter investigation must establish
whether they are derived from the same power reading, their thresholds and update
timing, and what inconsistent or unavailable states mean.

The mode selector has a related problem. Selecting its entity does not explain
what each option does. A free-text baseline such as `Maximum Self Consumption`
can be mistyped or cease to match the installed integration's options. A dropdown
populated from the select would prevent typing errors, but would still leave the
customer to interpret manufacturer-specific behavior.

The proposed adapter would map application intents to known supported mode
options and command entities, and validate that mapping against the installed
interface. Displayed option names alone are not a universal semantic contract.
Use stable option identifiers if the integration exposes them; otherwise maintain
and validate the exact options for the supported interface. An unknown mode or
changed option set requires an explicit unsupported/setup state, not a guessed
replacement or a concealed default string.

### What the selection would identify

The likely choice is an **installed integration instance and the equipment it
controls**, not just a manufacturer name. A home can have multiple installations
of one integration, multiple devices within it, or separate sources for telemetry
and commands. Vehicle-side and charger-side EV control must remain distinct.

Discovery could use integration/device registry associations and integration-owned
entity identities instead of literal installation-specific entity names. The
adapter would resolve those identities to current entity IDs and expose a readable
summary of its selected sensors, actuators, units, modes and capabilities. Exact
identity and rediscovery rules remain design work; selecting an integration does
not by itself prove that a particular make/model or firmware supports every action.

Customer preferences such as planning participation and export permission remain
separate from equipment semantics. Selecting a supported integration must not
itself authorize live control. Internal profile details do not justify restoring
customer phase-count or voltage settings.

### Pros and cons

| Benefit | Cost or limitation |
| --- | --- |
| Fewer selections and fewer combinations that look valid but command the wrong behavior. | Each supported integration needs an adapter, tests and ongoing maintenance. |
| Units, measurement direction and mode meanings can be verified once for a supported interface. | Integration, firmware and model changes can invalidate those assumptions; compatibility must be explicit. |
| A concrete supported-equipment list makes capabilities and support expectations clearer. | Initial coverage is narrower than a generic entity form; unsupported equipment needs an honest product state. |
| Discovery can survive user-renamed entities through registry identity. | Multiple devices, duplicate integration instances and replacement/reinstalled equipment still require disambiguation. |
| One adapter can coordinate mode selection, limits, authority and restoration. | An adapter defect can affect every installation using it, so representative commissioning and regression coverage matter more. |
| Customers configure intent rather than technical sign conventions. | Automation can obscure its choices unless the resolved bindings and reasons for unavailable actions remain inspectable. |
| Equipment-specific controls can represent actual capabilities instead of pretending every device accepts signed watts. | The planner/executor contract may need explicit intents and capability limits; a selector alone does not fix that mismatch. |

### Assessment and questions before implementation

This is a promising direction for equipment we explicitly support. It moves
complexity out of customer setup into maintained, testable device knowledge; it
does not eliminate that complexity. A thin adapter per supported interface is a
more concrete starting point for discussion than a universal catalog of every
make and model.

Before implementing, decide the boundary between integration adapters and model
variants, how separate telemetry/control integrations compose, how a customer
chooses among multiple devices, and how unsupported interfaces are presented.
Also decide which installation-specific choices genuinely remain editable and
which are facts supplied by the adapter. Do not introduce a generic advanced
mapping fallback by assumption.

The existing battery-control notes still govern the Sigenergy investigation:
mode semantics, separate non-negative ESS charge/discharge limits, authority,
physical confirmation and restoration must be established. Integration selection
would package that knowledge for setup; it would not replace it or establish
unmeasured behavior.

## Clarification: automatic prerequisites and writable limits

Recorded 11 September 2026 after reviewing the configured battery. These decisions
supersede the earlier requirement to have the customer manually configure an
authority handshake and establish restorable register values before control.
They supersede beta.55 behavior and are implemented by the schema-8 controller
except for automatic manufacturer prerequisite discovery, which remains deferred.

### Authority is an integration detail, not three customer settings

Do not require a customer to map a remote-control switch, a confirmation sensor
and a confirming state. Those are Sigenergy interface details. If a supported
adapter needs them, it should identify and manage them automatically within the
customer's existing permission for battery operation. The future integration
selection UI is still deferred; documenting automatic prerequisite handling does
not authorize building that UI now.

Without automatic handling, attempting an authorized operation and reporting a
clear control failure is preferable to requiring these three manual fields.
Distinguish a failed service call, a setting that was not accepted, and accepted
settings whose physical result does not match the requested operation. Successful
completion of a Home Assistant service call alone must not be reported as proof
that the battery followed the plan. Do not turn every manufacturer prerequisite
into another generic configuration option.

### Prior limit-register contents are not a setup requirement

The charge/discharge limit entities are writable controls. Write the intended
limits and verify acceptance; report a failure if the operation does not work.
Do not reject control because the previous register contents are an unset sentinel
or are outside the range permitted for new commands. Do not ask the customer to
repair the previous contents just to let SHS replace them.

Continue to validate the commands SHS sends: units, non-negative values, supported
bounds/step and the battery's rated limits still apply. Removing the pre-read
barrier does not mean replaying arbitrary old register contents during handover.
The prior snapshot-and-restore design therefore needs revision as part of the
controller implementation, rather than merely deleting its sentinel check.

Approved handover policy (confirmed by Phil on 11 September 2026): return to
**Maximum Self Consumption** and set both ceilings from the configured rated-power
sources—currently **8.8 kW charge and 9.6 kW discharge**. Resolve the sources again
at handover; do not depend on previous register contents or add manually entered
normal limits. The schema-8 controller implements this policy, including restart
recovery from its saved mapping. These values are user-approved, but this does
not claim that all operations have been tested on hardware.

If a supported integration exposes a defined reset-to-normal operation, evaluate
it as adapter-specific behavior rather than assuming it matches this policy.

### Plan-to-controller contract

The approved operating policy is implemented in snapshot/plan schema 8. The contract
carries the intended operation and both power ceilings; net watts alone cannot
express the necessary distinctions:

- Normal self-consumption, solar-only charging and house supply without battery
  export use the established self-consumption behavior, with ceilings expressing
  which flows are permitted.
- Grid-assisted charging uses the documented command-charging behavior.
- Deliberate battery export uses Command Discharging (PV First), only when the
  plan explicitly permits battery export. ESS First remains excluded.
- Hold is explicit; a zero net-power value must not implicitly choose between
  hold and normal operation.

The implementation coordinates planner output, versioned contract validation,
local execution, restoration and reporting; see [battery execution](battery-control-configuration.md). Source/destination restrictions
must be enforced during planning and conveyed to the controller; a mode selection
added after optimization cannot repair a plan that ignored those restrictions.

Outstanding physical observations are engineering validation work, not requests
for more customer configuration: Standby during surplus PV, Grid First with
surplus PV, self-consumption with a zero discharge ceiling, transition timing,
write rejection/read-only operation, and behavior when Home Assistant stops.
Implement and test the contract with explicit expectations, then verify those
expectations on the installation before describing every operation as proven.
Neither register acknowledgment nor unit tests establish those physical behaviors.
