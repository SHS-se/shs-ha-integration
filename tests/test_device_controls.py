"""Pure tests for requested planning roles and local mapping readiness."""

from __future__ import annotations

from pathlib import Path
import sys
import unittest

sys.path.append(str(Path(__file__).parents[1] / "custom_components" / "shs_energy"))

from device_controls import (  # noqa: E402
    apply_requested_configuration,
    battery_control_errors,
    pool_control_errors,
    is_room_thermal_control,
    planning_path,
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
        self.assertIn("assign a Home Assistant area", report["mapping_error"])

        report = mapping_report("setpoint", {
            "control_type": "setpoint",
            "temperature_entity_id": "sensor.office_temperature",
            "actuator_entity_ids": ["switch.office_heater"],
        }, entity_names={"switch.office_heater": "Office heater"},
            area_names={"office": "Office"},
            entity_area_ids={"switch.office_heater": "office"})
        self.assertEqual(report["mapping_status"], "ready")
        self.assertEqual(report["mapping_summary"]["entity_count"], 2)
        self.assertEqual(report["mapping_summary"]["room_name"], "Office")
        self.assertEqual(report["mapping_summary"]["controlled_devices"], ["Office heater"])

    def test_multiple_actuators_and_companions_are_not_ready(self):
        for kind in ("setpoint", "switch_schedule", "permit_inhibit"):
            mapping = {
                "control_type": kind, "temperature_entity_id": "sensor.office_temperature",
                "actuator_entity_ids": ["switch.a", "switch.b"], "max_inhibit_slots": 2,
            }
            report = mapping_report(kind, mapping)
            self.assertEqual(report["mapping_status"], "invalid")
            self.assertIn("exactly one control entity", report["mapping_error"])
            mapping["actuator_entity_ids"] = ["switch.a"]
            mapping["companion_actuator_entity_ids"] = ["switch.b"]
            self.assertIn("combined switching", mapping_report(kind, mapping)["mapping_error"])

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
        })
        self.assertEqual(report["mapping_status"], "not_configured")
        self.assertIsNone(report["mapped_control_type"])

    def test_deleted_entity_invalidates_an_otherwise_complete_mapping(self) -> None:
        report = mapping_report("switch_schedule", {
            "control_type": "switch_schedule",
            "actuator_entity_ids": ["switch.pool_heater"],
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
            "category": "ev_charging",
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

    def test_saved_room_association_remains_current_configuration(self):
        report = mapping_report("setpoint", {
            "control_type": "setpoint", "room_area_id": "office",
            "temperature_entity_id": "sensor.temperature",
            "actuator_entity_ids": ["climate.heater"],
        }, {"sensor.temperature", "climate.heater"},
            area_names={"office": "Office"}, entity_area_ids={})
        self.assertEqual(report["mapping_status"], "ready")
        self.assertEqual(report["mapping_summary"]["room_key"], "office")



class PlanningPathTests(unittest.TestCase):
    def test_a_meter_category_never_dictates_the_control_contract(self) -> None:
        # A floor heater metered as pool heating but asked to hold a setpoint
        # belongs to its room, not to the pool service.
        self.assertEqual(planning_path("setpoint", "pool_heating"), "room")
        self.assertEqual(planning_path("switch_schedule", "pool_heating"), "pool")
        self.assertEqual(planning_path("switch_schedule", "heating"), "room")
        self.assertEqual(planning_path("permit_inhibit", "hot_water"), "boiler")
        self.assertEqual(planning_path("variable_power", "ev_charging"), "ev")

    def test_pairings_no_model_can_plan_are_named(self) -> None:
        self.assertIsNone(planning_path("switch_schedule", "household"))
        self.assertIsNone(planning_path("variable_power", "heating"))
        self.assertIsNone(planning_path(None, "heating"))

    def test_a_setpoint_pool_room_heater_stays_ready(self) -> None:
        devices = [{
            "key": "sensor.pool_room_floor_heater_energy",
            "category": "pool_heating",
            "suggested_load_type": "fixed_full_load",
        }]
        requested = {
            "sensor.pool_room_floor_heater_energy": {
                "planning_role": "controllable",
                "control_type": "setpoint",
            }
        }
        mappings = {
            "sensor.pool_room_floor_heater_energy": {
                "control_type": "setpoint",
                "temperature_entity_id": "sensor.basement_bathroom_temperature",
                "actuator_entity_ids": ["climate.pool_bathroom_floor_thermostat"],
            }
        }
        apply_requested_configuration(
            devices,
            requested,
            mappings,
            area_names={"basement_bathroom": "Basement bathroom"},
            entity_area_ids={
                "climate.pool_bathroom_floor_thermostat": "basement_bathroom"
            },
        )
        self.assertEqual(devices[0]["mapping_status"], "ready")
        self.assertEqual(devices[0]["planning_role"], "controllable")
        self.assertEqual(devices[0]["mapping_summary"]["room_key"], "basement_bathroom")

    def test_an_unplannable_pairing_stays_in_base_load_with_a_reason(self) -> None:
        devices = [{
            "key": "sensor.washing_machine_energy",
            "category": "household",
            "suggested_load_type": "duty_cycle",
        }]
        requested = {
            "sensor.washing_machine_energy": {
                "planning_role": "controllable",
                "control_type": "switch_schedule",
            }
        }
        mappings = {
            "sensor.washing_machine_energy": {
                "control_type": "switch_schedule",
                "actuator_entity_ids": ["switch.washing_machine"],
                "power": 2000,
            }
        }
        apply_requested_configuration(devices, requested, mappings)
        self.assertEqual(devices[0]["mapping_status"], "invalid")
        self.assertEqual(devices[0]["planning_role"], "base_load")
        self.assertIn("no model for", devices[0]["mapping_error"])


if __name__ == "__main__":
    unittest.main()


def _room(**extra):
    """A complete setpoint mapping, before any optional thermal lever."""
    return {
        "control_type": "setpoint",
        "temperature_entity_id": "sensor.lounge",
        "actuator_entity_ids": ["climate.lounge"],
        **extra,
    }


class ThermalLeverTests(unittest.TestCase):
    """A heat pump is permitted, nudged, or told a mode — not given watts."""

    def report(self, mapping):
        return mapping_report(
            "setpoint", mapping, {"sensor.lounge", "climate.lounge",
                                  "switch.permit", "select.demand",
                                  "number.offset"},
            entity_area_ids={"climate.lounge": "lounge"},
            area_names={"lounge": "Lounge"},
        )

    def test_an_existing_setpoint_mapping_is_unchanged(self) -> None:
        """The new levers are optional, so no saved mapping may break."""
        self.assertEqual(self.report(_room())["mapping_status"], "ready")

    def test_permission_and_mode_need_no_extra_configuration(self) -> None:
        report = self.report(_room(
            permit_entity_id="switch.permit",
            mode_entity_id="select.demand",
        ))
        self.assertEqual(report["mapping_status"], "ready")
        self.assertIn("permit_entity_id", report["mapping_summary"]["configured_fields"])
        self.assertIn("mode_entity_id", report["mapping_summary"]["configured_fields"])

    def test_an_offset_without_bounds_is_refused(self) -> None:
        """The one lever that can drive equipment past what was reviewed."""
        report = self.report(_room(offset_entity_id="number.offset"))
        self.assertEqual(report["mapping_status"], "invalid")
        self.assertIn("minimum offset is required", report["mapping_error"])
        self.assertIn("maximum offset is required", report["mapping_error"])

    def test_an_inverted_offset_band_is_refused(self) -> None:
        report = self.report(_room(
            offset_entity_id="number.offset",
            offset_minimum=3.0,
            offset_maximum=-3.0,
        ))
        self.assertEqual(report["mapping_status"], "invalid")
        self.assertIn("below maximum offset", report["mapping_error"])

    def test_a_bounded_offset_is_ready(self) -> None:
        report = self.report(_room(
            offset_entity_id="number.offset",
            offset_minimum=-3.0,
            offset_maximum=3.0,
        ))
        self.assertEqual(report["mapping_status"], "ready")

    def test_a_zero_minimum_offset_is_a_bound_not_a_blank(self) -> None:
        """0 is falsey; a bound of zero must not read as unset."""
        report = self.report(_room(
            offset_entity_id="number.offset",
            offset_minimum=0,
            offset_maximum=3.0,
        ))
        self.assertEqual(report["mapping_status"], "ready")

    def test_a_deleted_lever_entity_is_caught(self) -> None:
        report = self.report(_room(permit_entity_id="switch.gone"))
        self.assertEqual(report["mapping_status"], "invalid")
        self.assertIn("no longer exist", report["mapping_error"])


def _battery(**extra):
    return {
        "battery_enabled": True,
        "battery_control_enabled": True,
        "battery_mode_entity": "select.mode",
        "battery_mode_charge": "Command Charging (PV First)",
        "battery_mode_discharge": "Command Discharging (PV First)",
        "battery_mode_idle": "Standby",
        "battery_charge_limit_entity": "number.charge_limit",
        "battery_discharge_limit_entity": "number.discharge_limit",
        "battery_charging_entity": "binary_sensor.charging",
        "battery_discharging_entity": "binary_sensor.discharging",
        "battery_mode_baseline": "Maximum Self Consumption",
        "battery_power_measurement_entity": "sensor.battery_power",
        "house_consumption_power_entity": "sensor.house",
        "solar_production_power_entity": "sensor.solar",
        "grid_power_entity": "sensor.grid",
        "battery_soc_entity": "sensor.soc",
        **extra,
    }


class BatteryControlTests(unittest.TestCase):
    """Plant-level, because there is one battery and it is already a store."""

    def test_a_complete_mapping_has_no_errors(self) -> None:
        self.assertEqual(battery_control_errors(_battery()), [])

    def test_control_switched_off_asks_for_nothing(self) -> None:
        """A home that never wants the planner writing must not be nagged."""
        self.assertEqual(battery_control_errors({"battery_control_enabled": False}), [])

    def test_every_missing_field_is_reported_in_one_pass(self) -> None:
        errors = battery_control_errors({
            "battery_enabled": True, "battery_control_enabled": True,
        })
        self.assertIn("battery mode entity is required", errors)
        self.assertIn("charge power limit entity is required", errors)
        self.assertIn("Measured battery power is required for battery control", errors)
        self.assertIn("battery state of charge entity is required", errors)
        self.assertIn("the mode value meaning charge is required", errors)

    def test_reusing_one_mode_value_is_refused(self) -> None:
        """Charge and discharge are separate modes, not one signed request."""
        errors = battery_control_errors(_battery(battery_mode_discharge="Standby"))
        self.assertIn(
            "charge, discharge and idle must be different mode values", errors
        )

    def test_reusing_one_direction_sensor_is_refused(self):
        self.assertTrue(any("different entities" in error for error in battery_control_errors(
            _battery(battery_discharging_entity="binary_sensor.charging"))))

    def test_no_manual_authority_fields_are_required(self):
        self.assertEqual(battery_control_errors(_battery()), [])

    def test_control_without_a_battery_is_refused(self) -> None:
        errors = battery_control_errors(_battery(battery_enabled=False))
        self.assertIn("this home is not marked as having a house battery", errors)


class PoolSwitchTests(unittest.TestCase):
    def test_required_inputs_and_correction(self):
        fields = {}
        self.assertEqual(len(pool_control_errors({}, {}, field_errors=fields)), 2)
        self.assertEqual(set(fields), {'pool_water_temperature_entity', 'actuator_entity_ids'})
        options = {'pool_water_temperature_entity': 'sensor.water'}
        for targets in ([], ['number.start'], ['switch.one', 'switch.two']):
            self.assertTrue(pool_control_errors(options, {'actuator_entity_ids': targets}))
        self.assertEqual(pool_control_errors(options, {'actuator_entity_ids': ['switch.pool']}), [])


class PoolDeviceMappingTests(unittest.TestCase):
    def test_pool_setpoint_report_identifies_pool_service_without_room_or_temperature_controls(self):
        report = mapping_report('setpoint', {
            'control_type': 'setpoint', 'actuator_entity_ids': ['switch.pool_heater'],
            'temperature_entity_id': 'sensor.filtered_pool_water_temperature',
            'power': 'sensor.pool_heater_power',
        }, pool_control=True, pool_water_entity='sensor.filtered_pool_water_temperature')
        self.assertEqual(report['mapping_status'], 'ready')
        self.assertEqual(report['mapped_control_type'], 'setpoint')
        self.assertEqual(report['mapping_summary'], {
            'control_type': 'setpoint', 'entity_count': 2,
            'configured_fields': ['actuator_entity_ids', 'power', 'temperature_entity_id'],
            'power_entity_name': 'sensor.pool_heater_power', 'planning_service': 'pool',
        })

    def test_a_pool_device_is_not_asked_for_a_band(self) -> None:
        """The regression: every switch_schedule meter offered its own band.

        The pool service is built from a heater and its circulation pump. Each
        one carrying a band asked for it twice, let two mappings disagree about
        the same window, and left two writers on one pair of registers.
        """
        for mapping in (
            {
                "control_type": "switch_schedule",
                "actuator_entity_ids": ["switch.pool_heater"],
            },
            {
                "control_type": "switch_schedule",
                "actuator_entity_ids": ["switch.pool_pump"],
            },
        ):
            report = mapping_report(
                "switch_schedule", mapping,
                {"switch.pool_heater", "switch.pool_pump"},
            )
            with self.subTest(mapping=mapping["actuator_entity_ids"]):
                self.assertEqual(report["mapping_status"], "ready")
                self.assertNotIn(
                    "start_temperature_entity_id",
                    report["mapping_summary"]["configured_fields"],
                )

    def test_the_pool_still_routes_to_the_pool_model(self) -> None:
        self.assertEqual(planning_path("switch_schedule", "pool_heating"), "pool")
