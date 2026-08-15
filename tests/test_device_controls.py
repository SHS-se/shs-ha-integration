"""Pure tests for requested planning roles and local mapping readiness."""

from __future__ import annotations

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).parents[1] / "custom_components" / "shs_energy"))

from device_controls import (  # noqa: E402
    MAPPING_SCHEMA_VERSION_FIELD,
    MIGRATED_ROOM_AREA_FIELD,
    apply_requested_configuration,
    is_room_thermal_control,
    mapping_report,
    migrate_device_control_mapping,
    migrate_device_control_mappings,
    recover_legacy_ev_options,
    requested_controllable_devices,
)


class DeviceControlMappingTests(unittest.TestCase):
    def test_setpoint_requires_a_home_assistant_room(self) -> None:
        report = mapping_report("setpoint", {
            "control_type": "setpoint",
            "temperature_entity_id": "sensor.office_temperature",
            "actuator_entity_ids": ["switch.office_heater"],
        })
        self.assertEqual(report["mapping_status"], "invalid")
        self.assertIn("assign a Home Assistant area", report["mapping_error"])

        report = mapping_report("setpoint", {
            "control_type": "setpoint",
            "temperature_entity_id": "sensor.office_temperature",
            "actuator_entity_ids": [
                "switch.office_heater_left",
                "switch.office_heater_right",
            ],
        }, entity_names={
            "switch.office_heater_left": "Office heater left",
            "switch.office_heater_right": "Office heater right",
        }, area_names={"office": "Office"}, entity_area_ids={
            "switch.office_heater_left": "office",
            "switch.office_heater_right": "office",
        })
        self.assertEqual(report["mapping_status"], "ready")
        self.assertEqual(report["mapping_summary"]["entity_count"], 3)
        self.assertEqual(report["mapping_summary"]["room_name"], "Office")
        self.assertEqual(
            report["mapping_summary"]["controlled_devices"],
            ["Office heater left", "Office heater right"],
        )

    def test_setpoint_rejects_actuators_in_different_rooms(self) -> None:
        report = mapping_report("setpoint", {
            "control_type": "setpoint",
            "temperature_entity_id": "sensor.house_temperature",
            "actuator_entity_ids": ["switch.office", "switch.bedroom"],
        }, area_names={"office": "Office", "bedroom": "Bedroom"},
            entity_area_ids={
                "switch.office": "office",
                "switch.bedroom": "bedroom",
            })
        self.assertEqual(report["mapping_status"], "invalid")
        self.assertIn("must all belong to one", report["mapping_error"])

    def test_mismatched_control_type_is_not_configured(self) -> None:
        report = mapping_report("permit_inhibit", {
            "control_type": "switch_schedule",
            "actuator_entity_ids": ["switch.boiler"],
            "min_run_slots": 4,
        })
        self.assertEqual(report["mapping_status"], "not_configured")
        self.assertIsNone(report["mapped_control_type"])

    def test_deleted_entity_invalidates_an_otherwise_complete_mapping(self) -> None:
        report = mapping_report("switch_schedule", {
            "control_type": "switch_schedule",
            "actuator_entity_ids": ["switch.pool_heater"],
            "min_run_slots": 4,
        }, {"switch.some_other_device"})
        self.assertEqual(report["mapping_status"], "invalid")
        self.assertIn("no longer exist", report["mapping_error"])

    def test_pending_request_remains_in_base_load(self) -> None:
        devices = [{
            "key": "sensor.hot_water_energy",
            "suggested_load_type": "duty_cycle",
        }]
        requested = {
            "sensor.hot_water_energy": {
                "planning_role": "controllable",
                "control_type": "permit_inhibit",
                "load_type": "duty_cycle",
            }
        }
        apply_requested_configuration(devices, requested, {})
        self.assertEqual(devices[0]["planning_role"], "base_load")
        self.assertIsNone(devices[0]["control_type"])
        self.assertEqual(devices[0]["mapping_status"], "not_configured")

    def test_ready_request_becomes_a_separate_controllable_device(self) -> None:
        devices = [{
            "key": "sensor.ev_energy",
            "suggested_load_type": "variable_full_load",
        }]
        requested = {
            "sensor.ev_energy": {
                "key": "sensor.ev_energy",
                "name": "Car",
                "planning_role": "controllable",
                "control_type": "variable_power",
                "load_type": "variable_full_load",
            }
        }
        mappings = {
            "sensor.ev_energy": {
                "control_type": "variable_power",
                "control_entity_id": "number.ev_current",
                "minimum_value": 6,
                "maximum_value": 16,
            }
        }
        apply_requested_configuration(devices, requested, mappings)
        self.assertEqual(devices[0]["planning_role"], "controllable")
        self.assertEqual(devices[0]["control_type"], "variable_power")
        self.assertEqual(devices[0]["mapping_status"], "ready")
        self.assertEqual(requested_controllable_devices(requested)[0]["name"], "Car")

    def test_number_mapping_rejects_inverted_limits(self) -> None:
        mapping = {
            "control_type": "variable_power",
            "control_entity_id": "number.ev_current",
            "minimum_value": 20,
            "maximum_value": 16,
        }
        report = mapping_report("variable_power", mapping)
        self.assertEqual(report["mapping_status"], "invalid")
        self.assertIn("below maximum", report["mapping_error"])

    def test_switch_minimum_run_is_optional_and_power_is_one_field(self) -> None:
        report = mapping_report("switch_schedule", {
            "control_type": "switch_schedule",
            "actuator_entity_ids": ["switch.pool_heater"],
            "power": 3600,
        })
        self.assertEqual(report["mapping_status"], "ready")
        self.assertEqual(report["mapping_summary"]["reviewed_power_w"], 3600)

    def test_on_off_heat_pump_is_mapped_to_its_actuator_room(self) -> None:
        self.assertTrue(is_room_thermal_control("switch_schedule", "cooling"))
        report = mapping_report(
            "switch_schedule",
            {
                "control_type": "switch_schedule",
                "temperature_entity_id": "sensor.entrance_temperature",
                "actuator_entity_ids": ["climate.entrance_aircon"],
            },
            area_names={"entrance": "Entrance"},
            entity_area_ids={"climate.entrance_aircon": "entrance"},
            room_control=True,
        )
        self.assertEqual(report["mapping_status"], "ready")
        self.assertEqual(report["mapping_summary"]["room_key"], "entrance")

    def test_existing_on_off_mapping_remains_ready_without_room_upgrade(self) -> None:
        report = mapping_report(
            "switch_schedule",
            {
                "control_type": "switch_schedule",
                "actuator_entity_ids": ["switch.office_heater"],
            },
            area_names={"office": "Office"},
            entity_area_ids={"switch.office_heater": "office"},
            room_control=True,
        )
        self.assertEqual(report["mapping_status"], "ready")
        self.assertNotIn("room_key", report["mapping_summary"])

    def test_migrated_room_preserves_a_valid_setpoint_mapping(self) -> None:
        mapping, changed = migrate_device_control_mapping(
            {
                "control_type": "setpoint",
                "area_id": "office",
                "temperature_entity_id": "sensor.office_temperature",
                "actuator_entity_ids": ["switch.office_heater"],
                "override_entity_id": "input_text.retired_override",
            },
            entity_area_ids={"sensor.office_temperature": "office"},
        )
        self.assertTrue(changed)
        self.assertEqual(mapping[MIGRATED_ROOM_AREA_FIELD], "office")
        report = mapping_report(
            "setpoint",
            mapping,
            {
                "sensor.office_temperature",
                "switch.office_heater",
            },
            area_names={"office": "Office"},
            entity_area_ids={},
        )
        self.assertEqual(report["mapping_status"], "ready")
        self.assertEqual(report["mapping_summary"]["room_name"], "Office")

    def test_room_recovery_retries_when_registries_are_not_ready(self) -> None:
        original = {
            "control_type": "setpoint",
            "temperature_entity_id": "sensor.office_temperature",
            "actuator_entity_ids": ["switch.office_heater"],
        }
        waiting, changed = migrate_device_control_mapping(original)
        self.assertFalse(changed)
        self.assertNotIn(MAPPING_SCHEMA_VERSION_FIELD, waiting)

        recovered, changed = migrate_device_control_mapping(
            waiting,
            entity_area_ids={"sensor.office_temperature": "office"},
        )
        self.assertTrue(changed)
        self.assertEqual(recovered[MIGRATED_ROOM_AREA_FIELD], "office")
        self.assertIn(MAPPING_SCHEMA_VERSION_FIELD, recovered)

    def test_every_historical_mapping_contract_is_migrated_losslessly(self) -> None:
        mappings = {
            "setpoint": {
                "control_type": "setpoint",
                "area_id": "office",
                "temperature_entity_id": "sensor.office_temperature",
                "setpoint_entity_id": "climate.office",
                "actuator_entity_ids": ["climate.office"],
                "comfort_high_entity_id": "input_number.office_high",
                "comfort_low_entity_id": "input_number.office_low",
                "override_entity_id": "input_text.office_override",
                "override_timer_entity_id": "input_number.office_timer",
                "power_entity_id": "sensor.office_power",
            },
            "switch": {
                "control_type": "switch_schedule",
                "actuator_entity_ids": ["switch.pool"],
                "availability_entity_id": "input_boolean.pool_enabled",
                "power_entity_id": "sensor.pool_power",
                "power_w": 900,
                "min_run_slots": 4,
            },
            "permit": {
                "control_type": "permit_inhibit",
                "actuator_entity_ids": ["switch.boiler"],
                "availability_entity_id": "input_boolean.boiler_enabled",
                "power_w": 3000,
                "max_inhibit_slots": 4,
            },
            "variable": {
                "control_type": "variable_power",
                "power_control_entity_id": "number.pool_output",
                "availability_entity_id": "input_boolean.pool_enabled",
            },
            "current": {
                "control_type": "current_limit",
                "current_control_entity_id": "number.ev_current",
                "connected_entity_id": "binary_sensor.ev_connected",
                "soc_entity_id": "sensor.ev_soc",
                "target_soc_entity_id": "number.ev_target_soc",
                "min_current_a": 6,
                "max_current_a": 16,
                "current_step_a": 1,
                "phase_count": 3,
                "voltage": 230,
                "battery_capacity_kwh": 77,
                "power_entity_id": "sensor.ev_power",
            },
        }
        original = {key: dict(value) for key, value in mappings.items()}
        migrated, changed = migrate_device_control_mappings(
            mappings,
            entity_area_ids={
                "climate.office": "office",
                "sensor.office_temperature": "office",
            },
            entity_limits={"number.pool_output": (0, 100)},
        )
        self.assertTrue(changed)
        self.assertEqual(set(migrated), set(original))
        for key, old_mapping in original.items():
            for field, value in old_mapping.items():
                if field == "control_type" and value == "current_limit":
                    continue
                self.assertEqual(migrated[key][field], value)

        self.assertEqual(migrated["setpoint"]["power"], "sensor.office_power")
        self.assertEqual(migrated["switch"]["power"], "sensor.pool_power")
        self.assertEqual(migrated["permit"]["power"], 3000)
        self.assertEqual(migrated["variable"]["control_entity_id"], "number.pool_output")
        self.assertEqual(migrated["variable"]["minimum_value"], 0)
        self.assertEqual(migrated["variable"]["maximum_value"], 100)
        self.assertEqual(migrated["current"]["control_type"], "variable_power")
        self.assertEqual(migrated["current"]["control_entity_id"], "number.ev_current")
        self.assertEqual(migrated["current"]["minimum_value"], 6)
        self.assertEqual(migrated["current"]["maximum_value"], 16)
        repeated, changed = migrate_device_control_mappings(
            migrated,
            entity_area_ids={
                "climate.office": "office",
                "sensor.office_temperature": "office",
            },
            entity_limits={"number.pool_output": (0, 100)},
        )
        self.assertFalse(changed)
        self.assertEqual(repeated, migrated)

    def test_legacy_ev_card_telemetry_moves_to_current_settings(self) -> None:
        mapping = {
            "control_type": "current_limit",
            "connected_entity_id": "binary_sensor.ev_connected",
            "soc_entity_id": "sensor.ev_soc",
            "target_soc_entity_id": "number.ev_target_soc",
            "departure_entity_id": "sensor.ev_departure",
            "energy_remaining_entity_id": "sensor.ev_energy_remaining",
            "power_entity_id": "sensor.car_charging_power",
        }
        options, changed = recover_legacy_ev_options({}, {"car": mapping})
        self.assertTrue(changed)
        self.assertEqual(
            options,
            {
                "ev_connected_entity": "binary_sensor.ev_connected",
                "ev_soc_entity": "sensor.ev_soc",
                "ev_target_soc_entity": "number.ev_target_soc",
                "ev_departure_entity": "sensor.ev_departure",
                "ev_energy_remaining_entity": "sensor.ev_energy_remaining",
            },
        )
        migrated_mapping, _changed = migrate_device_control_mapping(mapping)
        self.assertEqual(
            migrated_mapping["power"], "sensor.car_charging_power"
        )

    def test_current_ev_settings_win_over_legacy_card_values(self) -> None:
        current = {"ev_soc_entity": "sensor.current_ev_soc"}
        migrated, changed = recover_legacy_ev_options(
            current,
            {"car": {"soc_entity_id": "sensor.old_ev_soc"}},
        )
        self.assertFalse(changed)
        self.assertEqual(migrated, current)


if __name__ == "__main__":
    unittest.main()
