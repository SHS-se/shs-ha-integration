"""Shared field descriptions: rendering and writes use the same definitions."""
from __future__ import annotations
from typing import Any
if __package__:
    from . import const as shs_const
    from .device_controls import is_room_thermal_control
else:
    import const as shs_const
    from device_controls import is_room_thermal_control

def _field(
    key: str,
    label: str,
    kind: str,
    *,
    help_text: str = "",
    domains: tuple[str, ...] = (),
    required: bool = False,
    choices: tuple[tuple[str, str], ...] = (),
    unit: str | None = None,
    step: float | None = None,
    minimum: float | None = None,
    maximum: float | None = None,
    scale: float | None = None,
) -> dict[str, Any]:
    """Describe one field for the deliberately generic panel renderer."""
    result: dict[str, Any] = {
        "key": key,
        "label": label,
        "kind": kind,
        "help": help_text,
        "required": required,
    }
    if domains:
        result["domains"] = list(domains)
    if choices:
        result["choices"] = [
            {"value": value, "label": choice_label}
            for value, choice_label in choices
        ]
    if unit is not None:
        result["unit"] = unit
    if step is not None:
        result["step"] = step
    if minimum is not None:
        result["minimum"] = minimum
    if maximum is not None:
        result["maximum"] = maximum
    if scale is not None:
        result["scale"] = scale
    return result


POWER_FIELD = _field(
    "power",
    "Power",
    "power",
    help_text="Choose a power sensor, or enter reviewed watts directly.",
)

def _temperature_field(*, required: bool) -> dict[str, Any]:
    """Describe a mandatory setpoint source or optional room upgrade."""
    return _field(
        "temperature_entity_id",
        "Room or process temperature",
        "entity",
        domains=("sensor", "climate"),
        required=required,
        help_text=(
            "The measured temperature used to learn how this zone responds. Room heaters share this source; changing it updates every heater in the room."
            if required
            else "Optional. Add this to include an existing on/off control in room comfort planning; its saved schedule remains valid without it."
        ),
    )


TEMPERATURE_FIELD = _temperature_field(required=True)
OPTIONAL_TEMPERATURE_FIELD = _temperature_field(required=False)


def _number_control_fields() -> tuple[dict[str, Any], ...]:
    """Describe the shared variable-power number-entity contract."""
    return (
        _field(
            "control_entity_id",
            "Controlled number entity",
            "entity",
            domains=("number", "input_number"),
            required=True,
        ),
        _field(
            "minimum_value",
            "Minimum value",
            "number",
            minimum=0,
            step=0.1,
            required=True,
            help_text="Automatically proposed from Home Assistant when available. A saved value takes precedence.",
        ),
        _field(
            "maximum_value",
            "Maximum value",
            "number",
            minimum=0.1,
            step=0.1,
            required=True,
            help_text="Automatically proposed from Home Assistant when available. A saved value takes precedence.",
        ),
        POWER_FIELD,
    )


CONTROL_FIELDS: dict[str, tuple[dict[str, Any], ...]] = {
    "setpoint": (
        TEMPERATURE_FIELD,
        _field(
            "setpoint_entity_id",
            "Direct setpoint",
            "entity",
            domains=("number", "input_number", "climate"),
            help_text="Use this when one entity contains the current target temperature.",
        ),
        _field(
            "actuator_entity_ids",
            "Controlled heater or climate actuator(s)",
            "entities",
            domains=("switch", "climate", "input_boolean"),
            required=True,
            help_text="These entities reveal actual heating duty. Their Home Assistant area defines the room; this page does not operate them.",
        ),
        _field(
            "companion_actuator_entity_ids",
            "Required companion actuator(s)",
            "entities",
            domains=("switch", "climate", "input_boolean"),
            help_text="For coupled equipment such as a circulation pump.",
        ),
        POWER_FIELD,
        _field(
            "permit_entity_id",
            "Heating permission",
            "entity",
            domains=("switch", "input_boolean"),
            help_text="Optional. A machine that decides its own duty can be permitted or withheld instead of being given a temperature.",
        ),
        _field(
            "mode_entity_id",
            "Demand or operating mode",
            "entity",
            domains=("select", "input_select", "climate", "water_heater"),
            help_text="Optional. For equipment whose demand is chosen from named modes rather than a number.",
        ),
        _field(
            "offset_entity_id",
            "Bounded offset",
            "entity",
            domains=("number", "input_number"),
            help_text="Optional. Nudges the machine's own curve instead of overriding it. Both bounds are required with it.",
        ),
        _field(
            "offset_minimum",
            "Minimum offset",
            "number",
            step=0.1,
            help_text="The most this may lower the machine's own target.",
        ),
        _field(
            "offset_maximum",
            "Maximum offset",
            "number",
            step=0.1,
            help_text="The most this may raise it. Requests are clamped here, never beyond.",
        ),
    ),
    "permit_inhibit": (
        _field(
            "actuator_entity_ids",
            "Permit/inhibit actuator(s)",
            "entities",
            domains=("switch", "input_boolean", "climate"),
            required=True,
            help_text="The local thermostat keeps ownership of the duty cycle.",
        ),
        POWER_FIELD,
        _field(
            "max_inhibit_slots",
            "Maximum continuous inhibit",
            "number",
            unit="15-minute slots",
            minimum=1,
            step=1,
            required=True,
        ),
    ),
    "switch_schedule": (
        _field(
            "actuator_entity_ids",
            "Scheduled switch actuator(s)",
            "entities",
            domains=("switch", "input_boolean", "climate"),
            required=True,
        ),
        _field(
            "companion_actuator_entity_ids",
            "Required companion actuator(s)",
            "entities",
            domains=("switch", "input_boolean", "climate"),
        ),
        POWER_FIELD,
    ),
    "variable_power": _number_control_fields(),
}


EXECUTION_FIELDS = (
    _field("control_enabled", "Control according to the plan", "toggle",
           help_text="Enable only after assigning control ownership here and disabling competing automations. Unsupported plans never operate the device."),
    _field("control_override_entity", "Manual override", "entity", domains=("input_boolean", "binary_sensor")),
)
for kind in ("setpoint", "switch_schedule", "permit_inhibit"):
    CONTROL_FIELDS[kind] += EXECUTION_FIELDS
CONTROL_FIELDS["setpoint"] += (
    _field("minimum_temperature_c", "Lowest allowed target", "number", unit="°C", minimum=5, maximum=35),
    _field("maximum_temperature_c", "Highest allowed target", "number", unit="°C", minimum=5, maximum=35),
)
CONTROL_FIELDS["switch_schedule"] += (
    _field("minimum_on_seconds", "Minimum continuous on time", "number", unit="s", minimum=0, maximum=900),
    _field("minimum_off_seconds", "Minimum continuous off time", "number", unit="s", minimum=0, maximum=900),
)


def _control_fields(device: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    """Return the control contract, adding room inputs to on/off heaters."""
    control_type = str(device.get("control_type") or "")
    fields = CONTROL_FIELDS.get(control_type, ())
    if (
        is_room_thermal_control(control_type, str(device.get("category") or ""))
        and control_type != "setpoint"
    ):
        return (OPTIONAL_TEMPERATURE_FIELD, *fields)
    return fields


def section_fields(section: dict[str, Any]) -> list[dict[str, Any]]:
    """Every field a section persists, its own switch included.

    A section's `toggle` is deliberately not in `fields`: it states whether the
    equipment is here at all, so it belongs beside the title rather than among
    the settings it governs, and keeping it out of `fields` is what stops any
    renderer drawing it as one. It still has to be validated and saved like
    anything else, which is what this is for.
    """
    toggle = section.get("toggle")
    return [*([toggle] if toggle else []), *section["fields"]]


def _configuration_sections() -> list[dict[str, Any]]:
    """Return the editable non-device configuration grouped for the panel."""
    c = shs_const
    category_labels = (
        ("grid_import", "Grid import"),
        ("grid_export", "Grid export"),
        ("solar_production", "Solar production"),
        ("total_consumption", "Whole-home consumption"),
        ("battery_charge", "Battery charge energy"),
        ("battery_discharge", "Battery discharge energy"),
        ("heating", "Heating energy"),
        ("hot_water", "Hot-water energy"),
        ("cooling", "Cooling energy"),
        ("property_energy", "Property energy"),
        ("pool_heating", "Pool-heating energy"),
        ("ev_charging", "EV-charging energy"),
        ("household", "Household energy"),
    )
    return [
        {
            "id": "planning",
            "tab": "overview",
            "title": "Planning",
            "description": "Monitoring remains active when planning is off. Enabled controllers execute binding plans and restore their baseline when planning stops.",
            "fields": [
                _field(
                    c.OPT_PLANNING_MODE,
                    "Planning mode",
                    "select",
                    choices=((c.PLANNING_MODE_DISABLED, "Off — monitoring only"), (c.PLANNING_MODE_LIVE, "Live planning")),
                    required=True,
                )
            ],
        },
        {
            "id": "metering",
            "tab": "inputs",
            "title": "Energy Dashboard meters",
            "description": "Every listed sensor is summed into its category. Device meters remain classified separately by the website.",
            "fields": [
                _field(
                    f"{c.OPT_PREFIX_ENTITIES}{category}",
                    label,
                    "entities",
                    domains=("sensor",),
                )
                for category, label in category_labels
            ],
        },
        {
            "id": "prices_forecasts",
            "tab": "inputs",
            "title": "Solar and electrical measurements",
            "description": "Supplier and price area are configured on the Smart Home Solutions website; prices are fetched and calculated by the service.",
            "fields": [
                _field(c.OPT_PV_FORECAST_ENTITIES, "Solar forecast", "entities", domains=("sensor",)),
                _field(c.OPT_GRID_EXPORT_POWER_ENTITY, "Instantaneous grid-export power", "entity", domains=("sensor",)),
                _field(c.OPT_PV_FORECAST_LATITUDE, "Solar forecast latitude", "number", step=0.00001),
                _field(c.OPT_PV_FORECAST_LONGITUDE, "Solar forecast longitude", "number", step=0.00001),
            ],
        },
        {
            "id": "thermal_sources",
            "tab": "thermal",
            "title": "Shared outdoor conditions",
            "description": "Room sensors and actuators are mapped on each setpoint-controlled device. These shared sources let the website learn weather response and project it forward.",
            "fields": [
                _field(c.OPT_OUTDOOR_TEMPERATURE_ENTITY, "Measured outdoor temperature", "entity", domains=("sensor",)),
                _field(c.OPT_WEATHER_FORECAST_ENTITY, "Outdoor weather forecast", "entity", domains=("weather",)),
            ],
        },
        {
            "id": "house_battery",
            "tab": "storage",
            "title": "House battery",
            "description": "Export is a customer preference and remains advisory until a reviewed local battery executor exists.",
            "toggle": _field(
                c.OPT_BATTERY_ENABLED,
                "This home has a house battery",
                "toggle",
                help_text=(
                    "Switched off, so no battery is planned, charged or "
                    "discharged. Metering is unaffected."
                ),
            ),
            "fields": [
                _field(c.OPT_BATTERY_SOC_ENTITY, "Battery state of charge", "entity", domains=("sensor",)),
                _field(c.OPT_BATTERY_CAPACITY_KWH, "Usable capacity", "number", unit="kWh", minimum=0.1, step=0.1),
                _field(c.OPT_BATTERY_CHARGE_MAX_W, "Maximum charge power", "number", unit="W", minimum=1, step=1),
                _field(c.OPT_BATTERY_DISCHARGE_MAX_W, "Maximum discharge power", "number", unit="W", minimum=1, step=1),
                _field(c.OPT_BATTERY_MIN_SOC_ENTITY, "Discharge cut-off SOC sensor", "entity", domains=("sensor", "number")),
                _field(c.OPT_BATTERY_MIN_SOC, "Minimum SOC", "number", unit="%", minimum=0, maximum=100, step=1, scale=100),
                _field(c.OPT_BATTERY_MAX_SOC, "Maximum SOC", "number", unit="%", minimum=0, maximum=100, step=1, scale=100),
                _field(c.OPT_BATTERY_TARGET_SOC, "Preferred terminal SOC", "number", unit="%", minimum=0, maximum=100, step=1, scale=100),
                _field(c.OPT_BATTERY_TARGET_IS_HARD, "Make terminal target mandatory", "toggle"),
                _field(c.OPT_BATTERY_CHARGE_EFFICIENCY, "Charge efficiency", "number", unit="%", minimum=1, maximum=100, step=1, scale=100),
                _field(c.OPT_BATTERY_DISCHARGE_EFFICIENCY, "Discharge efficiency", "number", unit="%", minimum=1, maximum=100, step=1, scale=100),
                _field(c.OPT_BATTERY_EXPORT_ENABLED, "Allow planned battery export", "toggle"),
                _field(c.OPT_BATTERY_EXPORT_RESERVE_SOC, "Export reserve SOC", "number", unit="%", minimum=0, maximum=100, step=1, scale=100),
                _field(c.OPT_BATTERY_EXPORT_MIN_PRICE, "Minimum export price", "number", unit="SEK/kWh", minimum=0, step=0.01),
            ],
        },
        {
            "id": "battery_control",
            "tab": "controller",
            "title": "Battery control",
            "description": (
                "Scheduled battery execution. Filling "
                "this in does not start control: the switch below does. Enable "
                "after the response, sign and confirmation behaviour "
                "have been checked on this installation."
            ),
            "toggle": _field(
                c.OPT_BATTERY_CONTROL_ENABLED,
                "Execute the battery plan",
                "toggle",
                help_text=(
                    "Switched off, so the battery is planned but never "
                    "written to, and its own controller keeps deciding."
                ),
            ),
            "fields": [
                _field(
                    c.OPT_BATTERY_MODE_ENTITY,
                    "Control mode",
                    "entity",
                    domains=("select", "input_select"),
                    help_text="The entity that selects charging, discharging or holding.",
                ),
                _field(
                    c.OPT_BATTERY_MODE_CHARGE,
                    "Mode value meaning charge",
                    "text",
                    help_text="Copy the option exactly as the mode entity spells it.",
                ),
                _field(c.OPT_BATTERY_MODE_DISCHARGE, "Mode value meaning discharge", "text"),
                _field(c.OPT_BATTERY_MODE_IDLE, "Mode value meaning hold", "text"),
                _field(c.OPT_BATTERY_MODE_BASELINE, "Baseline mode", "text", help_text="Restored on disable, expiry, fault and startup. Sigenergy uses Maximum Self Consumption."),
                _field(c.OPT_BATTERY_MEASUREMENT_CHARGE_POSITIVE, "Measured battery power is positive when charging", "toggle", help_text="The measurement sign is independent of the power command sign."),
                _field(
                    c.OPT_BATTERY_POWER_ENTITY,
                    "Power target",
                    "entity",
                    domains=("number", "input_number"),
                    help_text="The number written to request charge or discharge power.",
                ),
                _field(
                    c.OPT_BATTERY_POWER_UNIT,
                    "Power target unit",
                    "select",
                    choices=tuple((value, value) for value in c.BATTERY_POWER_UNITS),
                    help_text="What the target above is written in. Inverters commonly take kW where the planner works in W.",
                ),
                _field(
                    c.OPT_BATTERY_DISCHARGE_IS_NEGATIVE,
                    "Discharge is written as a negative number",
                    "toggle",
                    help_text="Settle this by measurement, not assumption; an inverted sign charges when the plan says discharge.",
                ),
                _field(
                    c.OPT_BATTERY_POWER_MEASUREMENT_ENTITY,
                    "Measured battery power",
                    "entity",
                    domains=("sensor",),
                    help_text="Used to confirm what the battery actually did. A command is not evidence that it happened.",
                ),
                _field(
                    c.OPT_BATTERY_AUTHORITY_ENTITY,
                    "Remote control switch",
                    "entity",
                    domains=("switch", "input_boolean"),
                    help_text="Optional. The entity that claims remote control from the inverter's own controller.",
                ),
                _field(
                    c.OPT_BATTERY_AUTHORITY_CONFIRM_ENTITY,
                    "Remote control confirmation",
                    "entity",
                    domains=("sensor", "binary_sensor"),
                    help_text="Required with the switch above: reads back whether remote control was actually granted.",
                ),
                _field(
                    c.OPT_BATTERY_AUTHORITY_CONFIRM_STATE,
                    "State confirming remote control",
                    "text",
                    help_text="The value that entity reports while the planner holds authority.",
                ),
            ],
        },
        {
            "id": "pool_control",
            "tab": "controller",
            "title": "Pool temperature control",
            "description": "Settings the controller writes when Control pool heating is enabled. The pool's planning inputs remain on Storage & EV.",
            "fields": [
                _field(
                    c.OPT_POOL_START_TEMPERATURE_ENTITY,
                    "Start heating below",
                    "entity",
                    domains=("number", "input_number"),
                    help_text="Optional. For a pool held in a temperature band rather than switched on and off; the controller moves the band and the heat pump still picks when to run. One band per pool, however many meters heat it.",
                ),
                _field(
                    c.OPT_POOL_STOP_TEMPERATURE_ENTITY,
                    "Stop heating at",
                    "entity",
                    domains=("number", "input_number"),
                    help_text="Required with the entity above: writing one end alone inverts or collapses the band.",
                ),
                _field(
                    c.OPT_POOL_TEMPERATURE_MINIMUM,
                    "Coldest the band may be set to",
                    "number",
                    unit="°C",
                    step=0.1,
                    help_text="Both bounds are required with a temperature band. Requests are clamped here, never beyond.",
                ),
                _field(
                    c.OPT_POOL_TEMPERATURE_MAXIMUM,
                    "Warmest the band may be set to",
                    "number",
                    unit="°C",
                    step=0.1,
                ),
            ],
        },
        {
            "id": "scheduled_control",
            "tab": "controller",
            "title": "EV and pool control",
            "description": "Execute the current binding plan. Each device can be tested independently. Disable restores the settings captured before control; reactive adjustments are not included.",
            "fields": [
                _field(c.OPT_EV_CONTROL_ENABLED, "Control EV charging", "toggle"),
                _field(c.OPT_EV_CHARGE_SWITCH_ENTITY, "EV charging start/stop switch", "entity", domains=("switch", "input_boolean"), help_text="Required for execution. Off slots stop charging without writing a current below the charger's minimum."),
                _field(c.OPT_POOL_CONTROL_ENABLED, "Control pool heating", "toggle"),
                _field(c.OPT_POOL_PERMISSION_ENTITY, "Pool accessory permission", "entity", domains=("switch", "input_boolean"), help_text="Optional. Enabled with a heat slot and restored on handover. Off slots lower the temperature band."),
                *[_field(f"{device}_control_override_entity", f"{label} manual override", "entity", domains=("input_boolean", "binary_sensor", "switch"), help_text="On returns this device to its captured baseline and suspends planned commands.") for device, label in (("battery", "Battery"), ("ev", "EV"), ("pool", "Pool"))],
            ],
        },
        {
            "id": "electrical_limits",
            "tab": "storage",
            "title": "Electrical and horizon limits",
            "fields": [
                _field(c.OPT_GRID_IMPORT_LIMIT_W, "Grid import limit", "number", unit="W", minimum=1, step=1),
                _field(c.OPT_GRID_EXPORT_LIMIT_W, "Grid export limit", "number", unit="W", minimum=1, step=1),
                _field(c.OPT_TERMINAL_SOC_MIN, "Hard minimum terminal SOC", "number", unit="%", minimum=0, maximum=100, step=1, scale=100),
                _field(c.OPT_TERMINAL_ENERGY_VALUE, "Remaining battery value", "number", unit="SEK/kWh", minimum=0, step=0.01),
            ],
        },
        {
            "id": "pool",
            "tab": "storage",
            "title": "Pool store",
            "description": (
                "Water temperature is what the planner schedules against, so an "
                "already-warm pool asks for nothing and a cloudy forecast can make "
                "heating it early worthwhile. Volume converts energy into degrees. "
                "The pool's heat loss and its heat pump's efficiency against air "
                "temperature are learned from measurement and never entered."
            ),
            "toggle": _field(
                c.OPT_POOL_ENABLED,
                "This home has a heated pool",
                "toggle",
                help_text=(
                    "Switched off, so no pool heating is planned. Metering is "
                    "unaffected."
                ),
            ),
            "fields": [
                _field(
                    c.OPT_POOL_WATER_TEMPERATURE_ENTITY,
                    "Pool water temperature",
                    "entity",
                    domains=("sensor",),
                ),
                _field(
                    c.OPT_POOL_VOLUME_M3,
                    "Pool volume",
                    "number",
                    unit="m³",
                    minimum=0.5,
                    step=0.5,
                ),
            ],
        },
        {
            "id": "ev",
            "tab": "storage",
            "title": "Electric vehicle",
            "description": (
                "Vehicle state supplies the charging need, and the car's own charge "
                "limit caps it. An optional departure timestamp gives that "
                "limit a specific deadline; without one, the planner uses the "
                "end of its rolling horizon. Charger current bounds "
                "and the optional charging-power sensor come from its Variable Power "
                "device card. The electrical model below converts those amps into "
                "watts, so it has to match the cable actually installed: a "
                "single-phase charger set to three phases is planned at three times "
                "the power it can deliver."
            ),
            "toggle": _field(
                c.OPT_EV_ENABLED,
                "This home charges an electric vehicle",
                "toggle",
                help_text=(
                    "Switched off, so no vehicle charging is planned. Metering "
                    "is unaffected."
                ),
            ),
            "fields": [
                _field(
                    c.OPT_EV_CONNECTED_ENTITY,
                    "Vehicle connected state",
                    "entity",
                ),
                _field(
                    c.OPT_EV_SOC_ENTITY,
                    "Vehicle battery SOC",
                    "entity",
                    domains=("sensor",),
                ),
                _field(
                    c.OPT_EV_TARGET_SOC_ENTITY,
                    "Vehicle charge limit",
                    "entity",
                    help_text=(
                        "The car's own charge limit, which it will refuse to "
                        "charge past. The planner treats it as a hard ceiling "
                        "on the vehicle store, so it never buys more range "
                        "than this allows, and plans toward it when no "
                        "departure is set."
                    ),
                ),
                _field(
                    c.OPT_EV_DEPARTURE_ENTITY,
                    "Optional departure timestamp",
                    "entity",
                    help_text=(
                        "When provided, the entity must contain a timezone-aware "
                        "timestamp. Leave it empty to plan toward the charge "
                        "limit over the rolling horizon."
                    ),
                ),
                _field(
                    c.OPT_EV_PHASE_COUNT,
                    "Charger phases",
                    "number",
                    step=1,
                    minimum=1,
                    maximum=3,
                    help_text=(
                        "How many phases the charge cable uses. One for a "
                        "single-phase installation, three for the common Swedish "
                        "three-phase one."
                    ),
                ),
                _field(
                    c.OPT_EV_PHASE_VOLTAGE,
                    "Phase voltage",
                    "number",
                    unit="V",
                    step=1,
                    minimum=100,
                    maximum=500,
                ),
                _field(
                    c.OPT_EV_CHARGE_EFFICIENCY,
                    "Charging efficiency",
                    "number",
                    step=0.01,
                    minimum=0.5,
                    maximum=1,
                    help_text=(
                        "Share of the energy drawn from the wall that reaches the "
                        "battery. Lowering it makes the planner buy more to deliver "
                        "the same range."
                    ),
                ),
                _field(
                    c.OPT_EV_KWH_PER_KM,
                    "Consumption",
                    "number",
                    unit="kWh/km",
                    step=0.005,
                    minimum=0.05,
                    maximum=1,
                    help_text=(
                        "What the car uses per kilometre. This converts state of "
                        "charge into range, which is the unit the planner values the "
                        "car in — so it is also what makes a winter kilometre worth "
                        "more than a summer one."
                    ),
                ),
                _field(
                    c.OPT_EV_ENERGY_REMAINING_ENTITY,
                    "Usable energy remaining",
                    "entity",
                    domains=("sensor",),
                    help_text=(
                        "Used with current SOC to derive usable battery capacity "
                        "automatically."
                    ),
                ),
            ],
        },
    ]


