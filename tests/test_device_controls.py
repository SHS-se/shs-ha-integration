"""Pure tests for requested planning roles and local mapping readiness."""

from __future__ import annotations

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).parents[1] / "custom_components" / "shs_energy"))

from device_controls import (  # noqa: E402
    apply_requested_configuration,
    mapping_report,
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
        self.assertIn("room", report["mapping_error"])

        report = mapping_report("setpoint", {
            "control_type": "setpoint",
            "area_id": "office",
            "temperature_entity_id": "sensor.office_temperature",
            "actuator_entity_ids": [
                "switch.office_heater_left",
                "switch.office_heater_right",
            ],
        }, entity_names={
            "switch.office_heater_left": "Office heater left",
            "switch.office_heater_right": "Office heater right",
        }, area_names={"office": "Office"})
        self.assertEqual(report["mapping_status"], "ready")
        self.assertEqual(report["mapping_summary"]["entity_count"], 3)
        self.assertEqual(report["mapping_summary"]["room_name"], "Office")
        self.assertEqual(
            report["mapping_summary"]["controlled_devices"],
            ["Office heater left", "Office heater right"],
        )

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


if __name__ == "__main__":
    unittest.main()
