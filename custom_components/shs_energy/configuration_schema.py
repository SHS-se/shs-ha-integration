"""Current persisted fields and strict save contracts, independent of runtime IO."""

from copy import deepcopy

if __package__:
    from .const import CONFIGURABLE_CATEGORIES
else:
    from const import CONFIGURABLE_CATEGORIES

OPTION_KEYS = frozenset({
    "automatic_setup",
    "battery_authority_confirm_entity",
    "battery_authority_confirm_state",
    "battery_authority_entity",
    "battery_capacity_kwh",
    "battery_charge_efficiency",
    "battery_charge_max_w",
    "battery_control_enabled",
    "battery_discharge_efficiency",
    "battery_discharge_is_negative",
    "battery_discharge_max_w",
    "battery_enabled",
    "battery_export_enabled",
    "battery_export_min_price_sek_per_kwh",
    "battery_export_reserve_soc",
    "battery_max_soc",
    "battery_measurement_charge_positive",
    "battery_min_soc",
    "battery_min_soc_entity",
    "battery_mode_baseline",
    "battery_mode_charge",
    "battery_mode_discharge",
    "battery_mode_entity",
    "battery_mode_idle",
    "battery_power_entity",
    "battery_power_measurement_entity",
    "battery_power_unit",
    "battery_soc_entity",
    "battery_target_is_hard",
    "battery_target_soc",
    "ev_charge_efficiency",
    "ev_charge_switch_entity",
    "ev_connected_entity",
    "ev_control_enabled",
    "ev_departure_entity",
    "ev_enabled",
    "ev_energy_remaining_entity",
    "ev_kwh_per_km",
    "ev_phase_count",
    "ev_phase_voltage",
    "ev_soc_entity",
    "ev_target_soc_entity",
    "forecast_resolution_minutes",
    "grid_export_limit_w",
    "grid_export_power_entity",
    "grid_import_limit_w",
    "outdoor_temperature_entity",
    "planning_mode",
    "pool_control_enabled",
    "pool_enabled",
    "pool_permission_entity",
    "pool_start_temperature_entity",
    "pool_stop_temperature_entity",
    "pool_temperature_maximum",
    "pool_temperature_minimum",
    "pool_volume_m3",
    "pool_water_temperature_entity",
    "pv_forecast_entities",
    "pv_forecast_latitude",
    "pv_forecast_longitude",
    "terminal_energy_value_sek_per_kwh",
    "terminal_soc_min",
    "weather_forecast_entity",
    "battery_control_override_entity",
    "ev_control_override_entity",
    "pool_control_override_entity",
}) | {f"entities_{category}" for category in CONFIGURABLE_CATEGORIES}
METADATA_KEYS = frozenset({"configuration_reviewed_at", "discovery_evidence", "_migration_report"})
PERSISTED_KEYS = OPTION_KEYS | METADATA_KEYS | {"device_control_mappings"}
ROOM_AREA_FIELD = "room_area_id"
COMMON_MAPPING_KEYS = frozenset({"control_type", "power", ROOM_AREA_FIELD})
MAPPING_KEYS = {
    "setpoint": COMMON_MAPPING_KEYS | {
        "temperature_entity_id", "setpoint_entity_id", "actuator_entity_ids",
        "companion_actuator_entity_ids", "permit_entity_id", "mode_entity_id",
        "offset_entity_id", "offset_minimum", "offset_maximum",
    },
    "switch_schedule": COMMON_MAPPING_KEYS | {
        "temperature_entity_id", "actuator_entity_ids", "companion_actuator_entity_ids",
    },
    "permit_inhibit": COMMON_MAPPING_KEYS | {"actuator_entity_ids", "max_inhibit_slots"},
    "variable_power": COMMON_MAPPING_KEYS | {"control_entity_id", "minimum_value", "maximum_value"},
}


def validate_mapping_keys(mapping):
    control_type = mapping.get("control_type")
    if control_type not in MAPPING_KEYS:
        raise ValueError("unsupported control type")
    unknown = set(mapping) - MAPPING_KEYS[control_type]
    if unknown:
        raise ValueError("unknown device fields: " + ", ".join(sorted(unknown)))


def merge_options(existing, incoming):
    """Apply only current public settings; never import old fields on save."""
    unknown = set(incoming) - OPTION_KEYS
    if unknown:
        raise ValueError("unknown configuration keys: " + ", ".join(sorted(unknown)))
    result = deepcopy(existing)
    result.update(deepcopy(incoming))
    return result
