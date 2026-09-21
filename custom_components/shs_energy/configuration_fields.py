"""Shared field descriptions: rendering and writes use the same definitions."""
from __future__ import annotations
from typing import Any
if __package__:
    from . import const as shs_const
    from .device_controls import is_room_thermal_control
else:
    import const as shs_const
    from device_controls import is_room_thermal_control

LABELS = {
    "monitoring": "Monitoring", "planning": "Planning", "control_verification": "Control verification",
    "controlling": "Controlling", "verified": "Verification logged",
    "switch_schedule": "Turns on and off", "setpoint": "Holds a temperature",
    "permit_inhibit": "Allowed to run", "variable_power": "Runs at a chosen power",
    "current_limit": "Charges at a chosen current", "fixed_full_load": "Runs at full power",
    "variable_full_load": "Varies its power", "duty_cycle": "Cycles on and off",
    "inverter": "Adjusts continuously", "ready": "Ready", "invalid": "Needs attention",
    "not_configured": "Not set up", "unavailable": "Unavailable", "disabled": "Off",
    "expired": "Expired", "advisory_only": "Advice only", "incomplete": "Waiting for inputs",
    "infeasible": "No workable schedule", "warning": "Needs attention", "error": "Needs attention",
    "fault": "Needs attention", "overridden": "Manual control", "unsupported": "Not supported",
    "commanded": "Target confirmed", "idle": "Waiting", "active": "Operating",
    "synchronised": "Up to date", "base_load": "Excluded", "controllable": "Included",
    "heating": "Heating", "cooling": "Cooling", "hot_water": "Hot water",
    "pool_heating": "Pool", "ev_charging": "Vehicle", "household": "Household",
    "property_energy": "Other equipment", "battery": "House battery", "pool": "Pool", "ev": "Vehicle",
    "not_requested": "Not needed", "waiting_for_history": "Collecting history",
    "observations_published": "Readings received", "device_mappings_required": "Device setup needed",
    "outdoor_sources_required": "Outdoor readings needed", "loaded": "Connected",
    "live": "Planning on", "configured": "Configured", "missing": "Missing",
    "baseline": "Own settings restored", "stopped": "Stopped", "limited": "At an operating limit",
    "scheduled": "Following the schedule", "confirmed": "Measured power confirmed", "pending": "Waiting to resume",
}


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
    if kind == "quantity" and not help_text:
        result["help"] = f"Choose a sensor, or enter a fixed value in {unit}. Sensor readings are converted using their reported units."
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
        "Room temperature",
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
            "Power or current control",
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


ACTUATOR_FIELD = {
    **_field(
        "actuator_entity_ids", "Control entity", "entities",
        domains=("switch", "climate", "input_boolean"), required=True,
        help_text="Choose one entity. To operate multiple devices together, create a single control entity in Home Assistant.",
    ),
    "max_items": 1,
}


CONTROL_FIELDS: dict[str, tuple[dict[str, Any], ...]] = {
    "setpoint": (
        TEMPERATURE_FIELD,
        _field(
            "setpoint_entity_id",
            "Temperature target",
            "entity",
            domains=("number", "input_number", "climate"),
            help_text="Use this when one entity contains the current target temperature.",
        ),
        ACTUATOR_FIELD,
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
        ACTUATOR_FIELD,
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
        ACTUATOR_FIELD,
        POWER_FIELD,
    ),
    "variable_power": _number_control_fields(),
}


EXECUTION_FIELDS = (
    _field("control_override_entity", "Manual override", "entity", domains=("input_boolean", "binary_sensor")),
)
for kind in ("setpoint", "switch_schedule", "permit_inhibit"):
    CONTROL_FIELDS[kind] += EXECUTION_FIELDS
CONTROL_FIELDS["setpoint"] += (
    _field("minimum_temperature_c", "Lowest allowed target", "number", unit="°C", minimum=5, maximum=35),
    _field("maximum_temperature_c", "Highest allowed target", "number", unit="°C", minimum=5, maximum=35),
)
CONTROL_FIELDS["switch_schedule"] += (
    _field("minimum_on_seconds", "Minimum continuous on time", "number", unit="s", minimum=0, maximum=900, help_text="Optional. Leave unset for no minimum on time."),
    _field("minimum_off_seconds", "Minimum continuous off time", "number", unit="s", minimum=0, maximum=900, help_text="Optional. Leave unset for no minimum off time."),
)


# Entities a planning path reads but never writes.
#
# An observation is not a control: it earns its place on the card because the
# model needs it, not because anything commands it. Keeping them in their own
# table is what stops the next one being added as another identity check.
POOL_POWER_SETTING_FIELD = _field(
    "power_setting_entity_id",
    "Power setting",
    "entity",
    domains=("number", "input_number", "sensor"),
    help_text=(
        "Optional. The heat pump's own target output setting. SHS only reads this, "
        "to learn how the pool's efficiency changes with it. Nothing writes to it, "
        "and the plan does not choose it."
    ),
)

OBSERVATION_FIELDS: dict[str, tuple[dict[str, Any], ...]] = {
    "pool": (POOL_POWER_SETTING_FIELD,),
}

# What a path's own executor runs, whatever method the website shows.
#
# `execute_pool` commands one relay, and it always has. A pool heater the
# website calls a setpoint has been driven that way for as long as there has
# been a pool; that substitution used to be forged inside `mapping_report`,
# which rewrote `control_type` and relabelled the report afterwards. Stating it
# once, per path rather than per pairing, means the card, the persisted keys,
# the entity checks and the executor all read the same answer — and a method
# added to the website later cannot quietly arrive without one.
PATH_CONTRACT: dict[str, str] = {
    "pool": "switch_schedule",
}

# The domains each path's executor can actually command. The pool writes one
# relay, so offering it a thermostat would promise something no code performs.
PATH_ACTUATOR_DOMAINS: dict[str, list[str]] = {
    "pool": ["switch", "input_boolean"],
}

# Which of the contract's fields a path's own executor actually reads.
#
# `execute_pool` commands one switch and takes the device's power for the
# model; it has never honoured a minimum on/off time, and the pool's manual
# override lives beside the pool card, not on the device. Offering those three
# would be the same promise-without-code the domain narrowing above avoids.
# A path absent from this table keeps its whole contract.
PATH_CONTRACT_KEYS: dict[str, frozenset[str]] = {
    "pool": frozenset({"actuator_entity_ids", "power"}),
}


def local_contract(control_type: str, path: str | None) -> str:
    """The contract the local executor runs for this pairing."""
    return PATH_CONTRACT.get(path or "", control_type)


def planning_path_of(device: dict[str, Any]) -> str | None:
    """The planning service this card is being drawn for.

    Read, never re-derived. Callers set `planning_system` from
    `mapped_planning_path`, which is the single routing authority, and `system`
    is that same answer already resolved for the system cards.
    """
    for key in ("planning_system", "system"):
        value = device.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def control_fields(
    control_type: str, path: str | None, category: str = "",
) -> tuple[dict[str, Any], ...]:
    """Every field one card shows, from the contract rather than the identity."""
    contract = local_contract(control_type, path)
    fields = CONTROL_FIELDS.get(contract, ())
    domains = PATH_ACTUATOR_DOMAINS.get(path or "")
    if domains:
        fields = tuple(
            {**field, "domains": list(domains)}
            if field["key"] == "actuator_entity_ids" else field
            for field in fields
        )
    kept = PATH_CONTRACT_KEYS.get(path or "")
    if kept is not None:
        fields = tuple(field for field in fields if field["key"] in kept)
    elif is_room_thermal_control(contract, category) and contract != "setpoint":
        fields = (OPTIONAL_TEMPERATURE_FIELD, *fields)
    fields = (*fields, *OBSERVATION_FIELDS.get(path or "", ()))
    primary = {"actuator_entity_ids": 0, "control_entity_id": 0, "power": 1}
    return tuple(sorted(fields, key=lambda field: primary.get(field["key"], 2)))


def mapping_keys(control_type: str) -> set[str]:
    """Every key this method may persist, on any path it can be routed to.

    Derived from the same catalogue the card renders, so a field cannot exist on
    a card and be rejected on save — the drift that kept the pool's own settings
    out of `MAPPING_KEYS` and forced a second hardcoded allowlist in migration.
    """
    return {"control_type", shs_const.ROOM_AREA_FIELD} | {
        field["key"]
        for path in (None, *OBSERVATION_FIELDS, *PATH_ACTUATOR_DOMAINS, "room")
        for field in control_fields(control_type, path)
    }


def _control_fields(device: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    """Return the control contract, adding room inputs to on/off heaters."""
    return control_fields(
        str(device.get("control_type") or ""),
        planning_path_of(device),
        str(device.get("category") or ""),
    )


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


def _configuration_sections(*, battery_control_required=False) -> list[dict[str, Any]]:
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
        {"id": "sharing", "tab": "energy", "title": "Device readings", "fields": [
            _field("excluded_device_readings", "Devices whose individual readings are not shared", "entities", domains=("sensor",))]},
        {
            "id": "metering",
            "tab": "energy",
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
            "tab": "energy",
            "title": "Solar and electrical measurements",
            "description": "Supplier and price area are configured on the Smart Home Solutions website; prices are fetched and calculated by the service.",
            "fields": [
                _field(c.OPT_PV_FORECAST_ENTITIES, "Solar forecast", "entities", domains=("sensor",)),
                _field("house_consumption_power_entity", "Instantaneous house consumption", "entity", domains=("sensor",), required=battery_control_required,
                       help_text="Gross appliance power before solar, excluding battery charging. Use a W or kW measurement at the household AC boundary."),
                _field("solar_production_power_entity", "Instantaneous solar production", "entity", domains=("sensor",), required=battery_control_required,
                       help_text="Reported plant solar power. The battery adapter accounts for conversion; energy counters and forecasts cannot authorize current supply."),
                _field("grid_power_entity", "Signed grid power", "entity", domains=("sensor",), required=battery_control_required,
                       help_text="Instantaneous W or kW: positive importing, negative exporting. Used for battery control and measured conversion losses."),
                _field(c.OPT_GRID_EXPORT_POWER_ENTITY, "Instantaneous grid-export power", "entity", domains=("sensor",)),
                _field(c.OPT_PV_FORECAST_LATITUDE, "Solar forecast latitude", "number", step=0.00001),
                _field(c.OPT_PV_FORECAST_LONGITUDE, "Solar forecast longitude", "number", step=0.00001),
            ],
        },
        {
            "id": "thermal_sources",
            "tab": "energy",
            "title": "Shared outdoor conditions",
            "description": "Shared outdoor readings help predict heating needs. Room temperature sources are set up with their devices.",
            "fields": [
                _field(c.OPT_OUTDOOR_TEMPERATURE_ENTITY, "Measured outdoor temperature", "entity", domains=("sensor",)),
                _field(c.OPT_WEATHER_FORECAST_ENTITY, "Outdoor weather forecast", "entity", domains=("weather",)),
            ],
        },
        {
            "id": "house_battery",
            "tab": "devices",
            "title": "House battery",
            "description": "Battery readings, operating limits and controls.",
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
                _field(c.OPT_BATTERY_CAPACITY_KWH, "Rated energy capacity", "quantity", unit="kWh", minimum=0.1, step=0.1),
                _field(c.OPT_BATTERY_CHARGE_MAX_W, "Maximum charge power", "quantity", unit="W", minimum=1, step=1),
                _field(c.OPT_BATTERY_DISCHARGE_MAX_W, "Maximum discharge power", "quantity", unit="W", minimum=1, step=1),
                _field(c.OPT_BATTERY_MIN_SOC, "Minimum charge", "quantity", unit="%", minimum=0, maximum=100, step=1, scale=100),
                _field(c.OPT_BATTERY_TARGET_SOC, "Preferred charge at the end of the plan", "number", unit="%", minimum=0, maximum=100, step=1, scale=100),
                _field(c.OPT_BATTERY_TARGET_IS_HARD, "Require the preferred end charge", "toggle"),
                _field(c.OPT_BATTERY_CHARGE_EFFICIENCY, "Charge efficiency", "number", unit="%", minimum=1, maximum=100, step=1, scale=100),
                _field(c.OPT_BATTERY_DISCHARGE_EFFICIENCY, "Discharge efficiency", "number", unit="%", minimum=1, maximum=100, step=1, scale=100),
                _field(c.OPT_BATTERY_EXPORT_ENABLED, "Allow planned battery export", "toggle"),
                _field(c.OPT_BATTERY_EXPORT_RESERVE_SOC, "Charge reserved before exporting", "number", unit="%", minimum=0, maximum=100, step=1, scale=100),
                _field(c.OPT_BATTERY_EXPORT_MIN_PRICE, "Minimum export price", "number", unit="SEK/kWh", minimum=0, step=0.01),
            ],
        },
        {
            "id": "battery_control",
            "tab": "devices",
            "title": "Battery control",
            "description": (
                "Battery command mappings and limits. Choose the device mode "
                "after reviewing its setup and verification evidence."
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
                    "Charging mode",
                    "battery_mode",
                    help_text="Choose an option reported by the selected control-mode entity.",
                ),
                _field(c.OPT_BATTERY_MODE_DISCHARGE, "Discharging mode", "battery_mode", help_text="Used for planned battery export. Supplying the house uses the baseline mode."),
                _field(c.OPT_BATTERY_MODE_IDLE, "Hold mode", "battery_mode"),
                _field(c.OPT_BATTERY_MODE_BASELINE, "Baseline mode", "battery_mode", help_text="Restored on disable, expiry, fault and startup. Sigenergy uses Maximum Self Consumption."),
                _field(c.OPT_BATTERY_CHARGE_LIMIT_ENTITY, "Charge power limit", "entity", domains=("number",), help_text="Non-negative charge ceiling. Units come from the selected entity."),
                _field(c.OPT_BATTERY_DISCHARGE_LIMIT_ENTITY, "Discharge power limit", "entity", domains=("number",), help_text="Non-negative discharge ceiling. Units come from the selected entity."),
                _field(c.OPT_BATTERY_CHARGING_ENTITY, "Battery charging sensor", "entity", domains=("binary_sensor",), help_text="Reports on when the battery is charging."),
                _field(c.OPT_BATTERY_DISCHARGING_ENTITY, "Battery discharging sensor", "entity", domains=("binary_sensor",), help_text="Reports on when the battery is discharging."),
                _field(
                    c.OPT_BATTERY_POWER_MEASUREMENT_ENTITY,
                    "Measured battery power",
                    "entity",
                    domains=("sensor",),
                    help_text="Signed battery-terminal power: positive charging, negative discharging. Used for physical confirmation and conversion-loss measurements.",
                ),
            ],
        },
        {
            "id": "scheduled_control",
            "tab": "devices",
            "title": "EV and pool control",
            "description": "Execute the current binding plan. Each device can be tested independently. Disable restores the settings captured before control; reactive adjustments are not included.",
            "fields": [
                _field(c.OPT_EV_CHARGE_SWITCH_ENTITY, "EV charging start/stop switch", "entity", domains=("switch", "input_boolean"), required=True, help_text="Required for execution. Off slots stop charging without writing a current below the charger's minimum."),
                *[_field(f"{device}_control_override_entity", f"{label} manual override", "entity", domains=("input_boolean", "binary_sensor", "switch"), help_text="On returns this device to its captured baseline and suspends planned commands.") for device, label in (("battery", "Battery"), ("ev", "EV"), ("pool", "Pool"))],
            ],
        },
        {
            "id": "electrical_limits",
            "tab": "devices",
            "title": "Electrical and horizon limits",
            "fields": [
                _field(c.OPT_GRID_IMPORT_LIMIT_W, "Grid import limit", "number", unit="W", minimum=1, step=1),
                _field(c.OPT_GRID_EXPORT_LIMIT_W, "Grid export limit", "number", unit="W", minimum=1, step=1),
                _field(c.OPT_TERMINAL_SOC_MIN, "Minimum charge at the end of the plan", "number", unit="%", minimum=0, maximum=100, step=1, scale=100),
                _field(c.OPT_TERMINAL_ENERGY_VALUE, "Remaining battery value", "number", unit="SEK/kWh", minimum=0, step=0.01),
            ],
        },
        {
            "id": "pool",
            "tab": "devices",
            "title": "Pool",
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
            "tab": "devices",
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
                    "Vehicle battery charge",
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


