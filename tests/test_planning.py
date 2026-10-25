"""Behavioural tests for deferrable-service construction.

This logic lived inside the coordinator, which the suite cannot import because
Home Assistant is not installed. It was covered only by assertions against the
coordinator's source text, which is how a pool-room floor heater held at a
setpoint came to kill the whole electrical plan while every test stayed green.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import sys
import unittest

sys.path.append(str(Path(__file__).parents[1] / "custom_components" / "shs_energy"))

from shs_core.optimisation import (  # noqa: E402
    OptimisationInputError,
    build_base_load_model,
)
from shs_core.planning import (  # noqa: E402
    build_device_models,
    build_services,
    disabled_store_paths,
    pool_heater_settings,
    pool_heating_running,
    unplanned_services,
)

TODAY = date(2026, 8, 15)
START = datetime(2026, 8, 15, 0, 0, tzinfo=timezone.utc)
HORIZON = [START + timedelta(minutes=15 * index) for index in range(96 * 3)]


def device(
    key: str,
    category: str,
    control_type: str,
    *,
    watts: float = 3_000,
    forecast_w: float = 0.0,
) -> dict[str, object]:
    return {
        "key": key,
        "name": key,
        "statistic_id": key,
        "category": category,
        "control_type": control_type,
        "planning_role": "controllable",
        "active_power_w": watts,
        "profile_sample_count": 1_000,
        "forecast_w_by_slot": [forecast_w] * len(HORIZON),
        "inference": {"daily_energy_kwh_by_weekday": {day: forecast_w * 24 / 1000 for day in range(7)}},
    }


def daily(**meters: float) -> dict[str, dict[str, float]]:
    """Thirty complete days of the same energy on each named meter."""
    return {
        (TODAY - timedelta(days=offset)).isoformat(): dict(meters)
        for offset in range(1, 31)
    }


class PoolServiceTests(unittest.TestCase):
    """The bug: a meter category never dictates the control contract."""

    pool_switch = device("sensor.pool_heater_energy", "pool_heating", "switch_schedule")
    pool_pump = device("sensor.pool_pump_energy", "pool_heating", "switch_schedule")
    pool_room = device(
        "sensor.pool_room_floor_heater_energy", "pool_heating", "setpoint", watts=800
    )
    mappings = {
        "sensor.pool_heater_energy": {
            "control_type": "switch_schedule",
            "actuator_entity_ids": ["switch.pool_heater"],
        },
        "sensor.pool_pump_energy": {
            "control_type": "switch_schedule",
            "actuator_entity_ids": ["switch.pool_pump"],
        },
        "sensor.pool_room_floor_heater_energy": {
            "control_type": "setpoint",
            "temperature_entity_id": "sensor.basement_bathroom_temperature",
            "actuator_entity_ids": ["climate.pool_bathroom_floor_thermostat"],
        },
    }

    def plan(self, models: list[dict[str, object]]):
        return build_services(
            {"device_control_mappings": self.mappings},
            HORIZON,
            models,
            read_entity=lambda entity_id: self.fail(
                f"a pool plan must not read {entity_id}"
            ),
            local_tz=timezone.utc,
        )

    def test_generated_pool_services_have_no_minimum_runtime(self) -> None:
        services, _samples, _ev = self.plan([self.pool_switch, self.pool_pump])
        self.assertTrue(services)
        for service in services:
            self.assertNotIn("min_run_slots", service)

    def test_pool_water_setpoint_and_pump_have_separate_running_power(self) -> None:
        heater = device("water-heater", "pool_heating", "setpoint", watts=1156)
        pump = device("pump", "pool_heating", "switch_schedule", watts=764)
        floor = self.pool_room
        mappings = {
            **self.mappings,
            "water-heater": {"control_type": "setpoint", "temperature_entity_id": "sensor.pool_water"},
            "pump": {"control_type": "switch_schedule", "actuator_entity_ids": ["switch.pump"]},
        }
        services, _, _ = build_services(
            {"pool_water_temperature_entity": "sensor.pool_water", "device_control_mappings": mappings},
            HORIZON, [heater, pump, floor], read_entity=lambda _: None, local_tz=timezone.utc,
        )
        self.assertEqual(services[0]["control"]["power_w"], 1920)
        # A different temperature is a room, even with exactly the same category.
        mappings["water-heater"]["temperature_entity_id"] = "sensor.room_air"
        services, _, _ = build_services(
            {"pool_water_temperature_entity": "sensor.pool_water", "device_control_mappings": mappings},
            HORIZON, [heater, pump, floor], read_entity=lambda _: None, local_tz=timezone.utc,
        )
        self.assertEqual(services[0]["control"]["power_w"], 764)

    def test_a_setpoint_heater_in_the_pool_category_does_not_break_the_plan(
        self,
    ) -> None:
        # Before the fix this raised "must use switch_schedule control" and no
        # plan was published at all.
        services, _samples, _ev = self.plan(
            [self.pool_switch, self.pool_pump, self.pool_room]
        )
        self.assertTrue(services)
        self.assertTrue(all(service["device"] == "pool" for service in services))

    def test_the_room_heater_is_not_charged_to_the_pool_service(self) -> None:
        with_room, _samples, _ev = self.plan(
            [self.pool_switch, self.pool_pump, self.pool_room]
        )
        without_room, _samples, _ev = self.plan([self.pool_switch, self.pool_pump])
        self.assertEqual(
            [service["required_kwh"] for service in with_room],
            [service["required_kwh"] for service in without_room],
        )
        # Demand comes from temperature, not historical daily energy.
        self.assertEqual(with_room[0]["required_kwh"], 0.0)

    def test_the_service_is_rated_from_its_own_devices(self) -> None:
        services, _samples, _ev = self.plan(
            [self.pool_switch, self.pool_pump, self.pool_room]
        )
        self.assertEqual(services[0]["control"]["power_w"], 6_000)

    def test_pool_hardware_contract_does_not_require_a_daily_budget(self) -> None:
        services, samples, _ev = self.plan([self.pool_switch, self.pool_pump])
        self.assertEqual(samples["pool_heating"], 0)
        self.assertEqual(len(services), 1)
        self.assertEqual(services[0]["required_kwh"], 0)
        self.assertEqual(services[0]["earliest_start"], HORIZON[0].isoformat())
        self.assertEqual(services[0]["deadline"],
                         (HORIZON[-1] + timedelta(minutes=15)).isoformat())


class BoilerServiceTests(unittest.TestCase):
    def test_replan_subtracts_delivered_boiler_energy_from_the_whole_day(self):
        boiler = device("boiler", "hot_water", "permit_inhibit", forecast_w=5000 / 24)
        horizon = HORIZON[48:]
        actuals = [{"start": HORIZON[10].isoformat(), "device_energy_kwh": {"boiler": 3, "preheat": 9}}]
        services, _, _ = build_services({"device_control_mappings": {"boiler": {
            "control_type": "permit_inhibit", "actuator_entity_ids": ["switch.water"]}}},
            horizon, [boiler], device_actuals=actuals, read_entity=lambda _: None, local_tz=timezone.utc)
        self.assertAlmostEqual(services[0]["required_kwh"], 2)
        self.assertAlmostEqual(services[1]["required_kwh"], 5)
        self.assertAlmostEqual(services[2]["required_kwh"], 5)

    def test_unknown_boiler_power_retains_its_permission_service(self):
        boiler = device("sensor.water", "hot_water", "permit_inhibit", watts=None)
        services, _samples, _ev = build_services(
            {"device_control_mappings": {"sensor.water": {
                "control_type": "permit_inhibit", "actuator_entity_ids": ["switch.water"]}}},
            HORIZON, [boiler], read_entity=lambda _: None, local_tz=timezone.utc)
        self.assertTrue(services)
        self.assertTrue(all(s["control"]["rated_power_w"] is None for s in services))
        self.assertTrue(all(s["required_kwh"] == 0 for s in services))

    def test_a_permit_inhibit_water_heater_becomes_a_duty_cycle_service(self) -> None:
        boiler = device(
            "sensor.hot_water_energy", "hot_water", "permit_inhibit",
            watts=2_800, forecast_w=350,
        )
        services, samples, _ev = build_services(
            {"device_control_mappings": {
                "sensor.hot_water_energy": {
                    "control_type": "permit_inhibit",
                    "actuator_entity_ids": ["switch.water_boiler"],
                    "max_inhibit_slots": 20,
                },
            }},
            HORIZON,
            [boiler],
            read_entity=lambda entity_id: self.fail("no entity read is needed"),
            local_tz=timezone.utc,
        )
        self.assertTrue(services)
        self.assertEqual(samples["hot_water"], 1_000)
        for service in services:
            self.assertEqual(service["device"], "boiler")
            self.assertEqual(service["control"]["type"], "duty_cycle")
            self.assertNotIn("max_consecutive_inhibit_slots", service["control"])

    def test_expected_power_above_running_watts_preserves_forecast_demand(self) -> None:
        boiler = device(
            "sensor.hot_water_energy", "hot_water", "permit_inhibit",
            watts=300, forecast_w=350,
        )
        services, _samples, _ev = build_services(
            {"device_control_mappings": {
                "sensor.hot_water_energy": {
                    "control_type": "permit_inhibit",
                    "actuator_entity_ids": ["switch.water_boiler"],
                    "max_inhibit_slots": 20,
                },
            }},
            HORIZON,
            [boiler],
            read_entity=lambda entity_id: self.fail("no entity read is needed"),
            local_tz=timezone.utc,
        )
        self.assertTrue(services)
        for service in services:
            self.assertEqual(service["control"]["rated_power_w"], 300)
            self.assertEqual(service["control"]["expected_power_w_by_slot"], [350] * len(HORIZON))
            self.assertEqual(service["required_kwh"], 8.4)


class EvServiceTests(unittest.TestCase):
    charger = device(
        "sensor.car_charging_energy", "ev_charging", "variable_power", watts=11_000
    )
    options = {
        "device_control_mappings": {
            "sensor.car_charging_energy": {
                "control_type": "variable_power",
                "control_entity_id": "number.charge_current",
                "minimum_value": 6,
                "maximum_value": 16,
            },
        },
        "ev_connected_entity": "binary_sensor.connected",
        "ev_soc_entity": "sensor.soc",
        "ev_target_soc_entity": "number.target_soc",
        "ev_energy_remaining_entity": "sensor.energy_remaining",
    }

    def setUp(self) -> None:
        self.states = {
            "number.charge_current": {
                "state": "10",
                "attributes": {
                    "unit_of_measurement": "A", "min": 6, "max": 16, "step": 1
                },
            },
            "binary_sensor.connected": {"state": "on", "attributes": {}},
            "sensor.soc": {"state": "50", "attributes": {"friendly_name": "Car"}},
            "number.target_soc": {"state": "80", "attributes": {}},
            "sensor.energy_remaining": {"state": "37.5", "attributes": {}},
        }

    def plan(self, options: dict[str, object]):
        return build_services(
            options,
            HORIZON,
            [self.charger],
            read_entity=lambda entity_id: self.states[entity_id],
            local_tz=timezone.utc,
        )

    def test_saved_current_range_is_authoritative_over_entity_bounds(self) -> None:
        options = {**self.options, "device_control_mappings": {
            self.charger["key"]: {
                **self.options["device_control_mappings"][self.charger["key"]],
                "minimum_value": 5,
            },
        }}
        for minimum, maximum in ((0, 5), (10, 13), (None, None), ("unknown", "unavailable")):
            with self.subTest(entity_minimum=minimum, entity_maximum=maximum):
                self.states["number.charge_current"]["attributes"].update(min=minimum, max=maximum)
                services, _, battery = self.plan(options)
                self.assertEqual(services[0]["control"]["min_current_a"], 5)
                self.assertEqual(services[0]["control"]["max_current_a"], 16)
                self.assertEqual(services[0]["control"]["current_step_a"], 1)
                self.assertTrue(battery["connected"])

    def test_entity_unit_and_step_are_still_required(self) -> None:
        attributes = self.states["number.charge_current"]["attributes"]
        attributes["unit_of_measurement"] = "W"
        with self.assertRaisesRegex(OptimisationInputError, "number.charge_current must declare unit A"):
            self.plan(self.options)
        attributes.update(unit_of_measurement="A", step=0)
        with self.assertRaisesRegex(OptimisationInputError, "number.charge_current step must be positive"):
            self.plan(self.options)

    def test_a_connected_car_reports_its_charge_limit_across_the_horizon(
        self,
    ) -> None:
        services, _samples, battery = self.plan(self.options)
        self.assertNotIn("departure", battery)
        self.assertNotIn("departure_target_soc", battery)
        self.assertEqual(battery["charge_limit_soc"], 0.8)
        self.assertTrue(battery["connected"])
        deadline = datetime.fromisoformat(services[0]["deadline"])
        self.assertEqual(deadline, HORIZON[-1] + timedelta(minutes=15))

    def test_unplugging_preserves_the_charging_plan_and_hardware_contract(self) -> None:
        connected_services, _, connected_battery = self.plan(self.options)
        self.states["binary_sensor.connected"]["state"] = "off"
        services, _, battery = self.plan(self.options)
        self.assertEqual(services, connected_services)
        self.assertFalse(battery["connected"])
        self.assertEqual(battery["available_from"], connected_battery["available_from"])
        self.assertEqual(battery["soc"], connected_battery["soc"])

    def test_a_retired_saved_option_does_not_read_an_entity_or_change_the_plan(self) -> None:
        expected = self.plan(self.options)
        # This entity deliberately does not exist. Retired settings cannot
        # reintroduce a deadline or block a fresh snapshot.
        actual = self.plan({**self.options, "ev_departure_entity": "sensor.removed"})
        self.assertEqual(actual, expected)

    def test_a_routed_connected_car_keeps_its_control_at_the_target_soc(self) -> None:
        self.states["sensor.soc"]["state"] = "80"
        services, _samples, _battery = self.plan(self.options)

        self.assertEqual(len(services), 1)
        self.assertEqual(services[0]["required_kwh"], 0)
        self.assertEqual(services[0]["control"], {
            "type": "discrete_current",
            "min_current_a": 6.0,
            "max_current_a": 16.0,
            "current_step_a": 1.0,
            "phase_count": 3,
            "voltage_v": 230.0,
        })

    def test_retired_phase_options_cannot_override_internal_ac_profile(self):
        services, _, _ = self.plan({**self.options, "ev_phase_count": 1, "ev_phase_voltage": 300})
        control = services[0]["control"]
        self.assertEqual(control["phase_count"] * control["voltage_v"], 690)
        self.assertEqual(control["current_step_a"], 1)
        self.assertEqual(control["max_current_a"], 16)

    def test_a_percentage_state_of_charge_is_read_by_its_unit(self) -> None:
        """A car at 1 % was read as full when the value alone set the scale."""
        self.states["sensor.soc"] = {"state": "1", "attributes": {"unit_of_measurement": "%"}}
        self.states["sensor.energy_remaining"]["state"] = "0.75"
        _services, _samples, battery = self.plan(self.options)
        self.assertEqual(battery["soc"], 0.01)
        self.assertEqual(battery["capacity_kwh"], 75)

    def test_an_empty_car_is_planned_with_its_known_battery_size(self) -> None:
        self.states["sensor.soc"]["state"] = "0"
        self.states["sensor.energy_remaining"]["state"] = "0"
        services, _samples, battery = build_services(
            self.options, HORIZON, [self.charger],
            read_entity=lambda entity_id: self.states[entity_id],
            local_tz=timezone.utc, ev_capacity_kwh=75.8,
        )
        self.assertEqual(battery["soc"], 0)
        self.assertEqual(battery["capacity_kwh"], 75.8)
        self.assertAlmostEqual(services[0]["required_kwh"], 0.8 * 75.8 / 0.92, places=3)
        # Without an earlier reading the builder leaves the car out first.
        with self.assertRaises(OptimisationInputError):
            self.plan(self.options)


class ServiceRoutingTests(unittest.TestCase):
    def test_a_pairing_no_service_owns_produces_no_service(self) -> None:
        # Room controls are planned as thermal zones, not as services, so a
        # snapshot of only room devices carries no service at all.
        heater = device("sensor.office_heater_energy", "heating", "switch_schedule")
        services, samples, battery = build_services(
            {"device_control_mappings": {
                "sensor.office_heater_energy": {
                    "control_type": "switch_schedule",
                    "actuator_entity_ids": ["switch.office_heater"],
                },
            }},
            HORIZON,
            [heater],
            read_entity=lambda entity_id: self.fail("no entity read is needed"),
            local_tz=timezone.utc,
        )
        self.assertEqual(services, [])
        self.assertEqual(samples, {})
        self.assertIsNone(battery)


def inventory_device(
    key: str,
    planning_role: str,
    control_type: str | None,
    *,
    category: str = "heating",
) -> dict[str, object]:
    return {
        "key": key,
        "name": key,
        "statistic_id": key,
        "category": category,
        "planning_role": planning_role,
        "control_type": control_type,
        "suggested_load_type": "duty_cycle",
        "active_power_w": None,
        "profile_sample_count": 0,
        "inference": {"source": "test"},
    }


def complete_history(*keys: str, kwh: float = 0.25) -> list[dict[str, object]]:
    """Fourteen days of every quarter, so weekday and weekend both learn."""
    start = datetime(2026, 8, 1, tzinfo=timezone.utc)
    return [
        {
            "start": (start + timedelta(days=day, minutes=15 * quarter)).isoformat(),
            "device_energy_kwh": {key: kwh for key in keys},
        }
        for day in range(14)
        for quarter in range(96)
    ]


class DeviceModelTests(unittest.TestCase):
    def test_pool_membership_is_exported_from_sensor_mapping_with_little_history(self):
        devices = [inventory_device(key, "controllable", kind) for key, kind in
                   [("pump", "switch_schedule"), ("heater", "setpoint"), ("floor", "setpoint")]]
        for item in devices:
            item["category"] = "pool_heating"
        mappings = {"heater": {"temperature_entity_id": "sensor.water"},
                    "floor": {"temperature_entity_id": "sensor.floor"}}
        actuals = [{"start": start.isoformat(), "device_energy_kwh":
                    {"pump": .191, "heater": .289, "floor": .2}} for start in HORIZON[:4]]
        models, degraded = build_device_models(
            devices, actuals, HORIZON, mappings, mapped_power_w=lambda _: None,
            pool_power_w={"pump":764, "heater":1156},
            local_tz=timezone.utc, pool_water_entity="sensor.water",
        )
        self.assertEqual(degraded, [])
        by_key = {model["key"]: model for model in models}
        self.assertEqual(by_key["pump"]["planning_service"], "pool")
        self.assertEqual(by_key["heater"]["planning_service"], "pool")
        self.assertNotIn("planning_service", by_key["floor"])
        self.assertEqual(by_key["pump"]["active_power_w"], 764)
        self.assertEqual(by_key["heater"]["active_power_w"], 1156)

    def build(self, devices, actuals, mappings=None, watts=None):
        models, _degraded = build_device_models(
            devices,
            actuals,
            HORIZON,
            mappings or {},
            mapped_power_w=lambda mapping: watts,
            pool_power_w={device["key"]:watts for device in devices},
            local_tz=timezone.utc,
        )
        return models

    def build_with_degraded(self, devices, actuals, mappings=None, watts=None, running_power=None):
        return build_device_models(
            devices,
            actuals,
            HORIZON,
            mappings or {},
            mapped_power_w=lambda mapping: watts,
            pool_power_w={device["key"]:watts for device in devices},
            local_tz=timezone.utc,
            boiler_running_power_w=running_power,
        )

    def test_every_devices_gap_is_reported_in_one_pass(self) -> None:
        # The whole point of the aggregation: fixing one problem must not be
        # the only way to discover the next one.
        devices = [
            inventory_device("sensor.a", "base_load", "setpoint"),
            inventory_device("sensor.b", "controllable", "nonsense"),
            inventory_device("sensor.c", "controllable", "setpoint"),
        ]
        with self.assertRaises(OptimisationInputError) as caught:
            self.build(devices, [])
        # Only the contract violations still raise. A device with no history is
        # a device to leave out, not a reason to refuse the whole home a plan.
        self.assertEqual(len(caught.exception.reasons), 2)
        self.assertIn("sensor.a has an invalid planning role", caught.exception.reasons[0])
        self.assertIn("sensor.b has an invalid planning role", caught.exception.reasons[1])

    def test_a_base_load_device_without_history_is_skipped_quietly(self) -> None:
        devices = [inventory_device("sensor.quiet", "base_load", None)]
        self.assertEqual(self.build(devices, []), [])  # base load, so nothing to report

    def test_a_controllable_device_becomes_a_model_for_every_slot(self) -> None:
        devices = [inventory_device("sensor.heater", "controllable", "setpoint")]
        models = self.build(devices, complete_history("sensor.heater"))
        self.assertEqual(len(models), 1)
        self.assertEqual(len(models[0]["forecast_w_by_slot"]), len(HORIZON))
        # 0.25 kWh in a quarter is a 1 kW draw.
        self.assertEqual(set(models[0]["forecast_w_by_slot"]), {1_000.0})
        self.assertEqual(models[0]["planning_role"], "controllable")
        self.assertEqual(models[0]["load_type"], "duty_cycle")

    def test_configured_state_based_devices_plan_without_any_history(self) -> None:
        for control_type, category in (("switch_schedule", "pool_heating"),
                                       ("setpoint", "heating"),
                                       ("variable_power", "ev_charging")):
            with self.subTest(category=category):
                devices = [inventory_device("sensor.new", "controllable",
                                            control_type, category=category)]
                models, degraded = self.build_with_degraded(devices, [], watts=772)
                self.assertEqual(len(models), 1)
                self.assertEqual(degraded, [])
                self.assertEqual(models[0]["active_power_w"], 772)
                self.assertEqual(models[0]["profile_sample_count"], 0)
                # Capacity does not become an invented 24-hour duty cycle.
                self.assertEqual(set(models[0]["forecast_w_by_slot"]), {0})

    def test_ev_current_contract_does_not_need_measured_watts(self) -> None:
        devices = [inventory_device("sensor.ev", "controllable", "variable_power",
                                    category="ev_charging")]
        models, degraded = self.build_with_degraded(devices, [])
        self.assertEqual(len(models), 1)
        self.assertEqual(degraded, [])

    def test_missing_pool_power_is_still_reported(self) -> None:
        devices = [inventory_device("sensor.pool", "controllable", "switch_schedule",
                                    category="pool_heating")]
        models, degraded = self.build_with_degraded(devices, [])
        self.assertEqual(len(models), 1)
        self.assertEqual(degraded, [])
        self.assertIsNone(models[0]["active_power_w"])
        self.assertIn("running_power_unknown", {w["code"] for w in models[0]["planning_warnings"]})

    def test_boiler_power_sensor_alone_does_not_supply_hot_water_energy_history(self) -> None:
        devices = [inventory_device("sensor.boiler", "controllable", "permit_inhibit",
                                    category="hot_water")]
        models, degraded = self.build_with_degraded(devices, [], mappings={"sensor.boiler": {"power": "sensor.boiler_power"}}, watts=3000)
        self.assertEqual(len(models), 1)
        self.assertEqual(degraded, [])
        self.assertEqual(models[0]["active_power_w"], 3000)
        self.assertEqual(models[0]["profile_sample_count"], 0)
        self.assertEqual(set(models[0]["forecast_w_by_slot"]), {0})
        self.assertIn("device_history_estimated", {w["code"] for w in models[0]["planning_warnings"]})

    def test_quarter_energy_supplies_expected_demand_not_pool_on_capability(self) -> None:
        devices = [inventory_device("sensor.new", "controllable", "switch_schedule",
                                    category="pool_heating")]
        history = complete_history("sensor.alive")
        for row in history[-44:]:
            row["device_energy_kwh"]["sensor.new"] = 0.193
        models, degraded = self.build_with_degraded(devices, history)
        self.assertEqual(len(models), 1)
        self.assertEqual(degraded, [])
        self.assertIsNone(models[0]["active_power_w"])
        self.assertEqual(max(models[0]["forecast_w_by_slot"]), 772)
        self.assertEqual(models[0]["profile_status"], "warming")

    def test_pool_on_bound_is_independent_of_live_power_and_quarter_average(self):
        key = "sensor.synthetic_pool"
        devices = [inventory_device(key, "controllable", "switch_schedule", category="pool_heating")]
        mappings = {key:{"control_type":"switch_schedule", "power":"sensor.synthetic_pool_power"}}
        models, _ = build_device_models(devices, complete_history(key, kwh=.3), HORIZON, mappings,
            mapped_power_w=lambda _:1200, pool_power_w={key:3600}, local_tz=timezone.utc)
        self.assertEqual(models[0]["active_power_w"], 3600)
        self.assertEqual(set(models[0]["forecast_w_by_slot"]), {1200})
        # Neither a previous profile estimate nor a lower current sample is a bound.
        models, _ = build_device_models(devices, complete_history(key, kwh=.3), HORIZON, mappings,
            mapped_power_w=lambda _:1200, pool_power_w={key:None}, local_tz=timezone.utc)
        self.assertIsNone(models[0]["active_power_w"])
        self.assertEqual(set(models[0]["forecast_w_by_slot"]), {1200})
        self.assertIn("running_power_unknown", {warning["code"] for warning in models[0]["planning_warnings"]})

    def test_missing_device_quarters_preserve_the_household_history(self) -> None:
        history = complete_history("sensor.alive")
        for row in history[-44:]:
            row["device_energy_kwh"]["sensor.new"] = 0.25
        actuals = [{"start": row["start"], "total_load_kwh": 0.75}
                   for row in history]
        model = build_base_load_model(
            actuals, "UTC", device_slots=history,
            modelled_device_keys=("sensor.alive", "sensor.new"), minimum_samples=2,
        )
        self.assertEqual(model["sample_count"], len(history))
        self.assertEqual(model["estimated_sample_count"], len(history) - 44)
        # The mature device is subtracted throughout. The new device is only
        # subtracted where measured; its unknown past stays conservatively in
        # the residual instead of being reconstructed from its recent profile.
        self.assertGreaterEqual(
            min(row["median_w"] for row in model["by_weekday"][0]), 1900,
        )

    def test_new_pool_heating_does_not_erase_older_household_consumption(self) -> None:
        # Regression for the September 9 replay: 142 metered quarters out of
        # 959, with heater power greater than the historical household base.
        # These are constructed readings, not raw data from the replay export.
        start = START - timedelta(minutes=959 * 15)
        history = []
        actuals = []
        for q in range(959):
            when = (start + timedelta(minutes=q * 15)).isoformat()
            measured = {"sensor.mature": 0.05}
            if q >= 959 - 142:
                measured["sensor.pool"] = 0.28  # 1120 W heat pump
            history.append({"start": when, "device_energy_kwh": measured})
            actuals.append({
                "start": when,
                "total_load_kwh": 0.15 + sum(measured.values()),  # 600 W base
            })
        devices = [inventory_device("sensor.pool", "controllable", "switch_schedule",
                                    category="pool_heating")]
        models, degraded = self.build_with_degraded(devices, history, watts=1120)
        self.assertEqual(len(models), 1)
        self.assertEqual(degraded, [])
        self.assertEqual(models[0]["profile_sample_count"], 142)
        model = build_base_load_model(
            actuals, "UTC", device_slots=history,
            modelled_device_keys=("sensor.mature", "sensor.pool"),
            minimum_samples=2,
        )
        self.assertEqual(model["sample_count"], 959)
        self.assertEqual(model["estimated_sample_count"], 817)
        for profile in model["by_weekday"].values():
            self.assertEqual({row["median_w"] for row in profile}, {600})

    def test_unmetered_consumption_is_not_silently_removed_from_the_baseline(self) -> None:
        history = complete_history("sensor.pool", kwh=0.28)
        # It may have been running before metering started. Without readings
        # there is no evidence to separate that consumption from the house.
        for row in history[:-142]:
            del row["device_energy_kwh"]["sensor.pool"]
        actuals = [{"start": row["start"], "total_load_kwh": 0.43}
                   for row in history]
        model = build_base_load_model(
            actuals, "UTC", device_slots=history, modelled_device_keys=("sensor.pool",),
        )
        self.assertGreater(
            min(row["median_w"] for row in model["by_weekday"][0]), 1500,
        )

    def test_measured_zero_household_consumption_is_not_given_an_artificial_floor(self) -> None:
        history = complete_history("sensor.pool", kwh=0)
        model = build_base_load_model(
            [{"start": row["start"], "total_load_kwh": 0} for row in history],
            "UTC", device_slots=history, modelled_device_keys=("sensor.pool",),
        )
        for profile in model["by_weekday"].values():
            self.assertEqual({row["median_w"] for row in profile}, {0})

    def test_no_device_evidence_keeps_a_conservative_household_baseline(self) -> None:
        history = complete_history("sensor.other")
        model = build_base_load_model(
            [{"start": row["start"], "total_load_kwh": 0.5} for row in history],
            "UTC", device_slots=history, modelled_device_keys=("sensor.new",),
        )
        self.assertEqual(model["estimated_sample_count"], len(history))
        self.assertEqual({row["median_w"] for row in model["by_weekday"][0]}, {2000})

    def test_a_measured_zero_is_not_replaced_by_estimated_consumption(self) -> None:
        history = complete_history("sensor.pool", kwh=0.25)
        # The quarter at midnight is explicitly off every day.
        for index in range(0, len(history), 96):
            history[index]["device_energy_kwh"]["sensor.pool"] = 0.0
        model = build_base_load_model(
            [{"start": row["start"], "total_load_kwh": 0.5} for row in history],
            "UTC", device_slots=history, modelled_device_keys=("sensor.pool",),
        )
        self.assertEqual(model["estimated_sample_count"], 0)
        self.assertEqual(model["by_weekday"][0][0]["median_w"], 2000)
        self.assertEqual(model["by_weekday"][0][1]["median_w"], 1000)

    def test_new_pool_model_reaches_service_construction_with_no_readings(self) -> None:
        devices = [inventory_device("sensor.pool", "controllable", "switch_schedule",
                                    category="pool_heating")]
        mappings = {"sensor.pool": {"control_type": "switch_schedule", "power": 772}}
        models, degraded = self.build_with_degraded(devices, [], mappings, watts=772)
        services, _samples, _ev = build_services(
            {"device_control_mappings": mappings}, HORIZON, models,
            read_entity=lambda _key: self.fail("no historical state is needed"),
            local_tz=timezone.utc,
        )
        self.assertEqual(degraded, [])
        self.assertEqual(services[0]["control"], {"type": "fixed_power", "power_w": 772})

    def test_old_device_history_does_not_disable_a_configured_control(self) -> None:
        history = complete_history("sensor.pool")[:96 * 4]
        devices = [inventory_device("sensor.pool", "controllable", "switch_schedule",
                                    category="pool_heating")]
        models, degraded = self.build_with_degraded(devices, history, watts=772)
        self.assertEqual(len(models), 1)
        self.assertEqual(degraded, [])
        self.assertEqual(models[0]["active_power_w"], 772)

    def test_a_device_reporting_throughout_is_still_modelled(self) -> None:
        """The bar must not evict healthy devices."""
        devices = [inventory_device("sensor.heater", "controllable", "setpoint")]
        models, degraded = self.build_with_degraded(
            devices, complete_history("sensor.heater")
        )
        self.assertEqual(len(models), 1)
        self.assertEqual(degraded, [])
        self.assertEqual(devices[0]["profile_status"], "ready")

    def test_a_base_load_device_is_measured_but_never_modelled(self) -> None:
        devices = [inventory_device("sensor.fridge", "base_load", None)]
        models = self.build(devices, complete_history("sensor.fridge"))
        self.assertEqual(models, [])
        self.assertIsNotNone(devices[0]["active_power_w"])

    def test_learned_measurements_are_written_back_for_the_next_run(self) -> None:
        # The caller uploads this same list and persists these fields, so the
        # in-place update is part of the contract rather than a side effect.
        devices = [inventory_device("sensor.heater", "controllable", "setpoint")]
        self.build(devices, complete_history("sensor.heater"))
        self.assertEqual(devices[0]["active_power_w"], 1_000.0)
        self.assertGreater(devices[0]["profile_sample_count"], 0)
        self.assertEqual(
            devices[0]["inference"]["profile"], "pooled_shape_weekday_level_v1"
        )
        self.assertEqual(devices[0]["inference"]["source"], "test")

    def test_boiler_running_power_uses_sensor_peaks_instead_of_quarter_means_or_idle_watts(self):
        key = "sensor.boiler_energy"
        for live_w in (0, 200, 1500, 3000):
            with self.subTest(live_w=live_w):
                devices = [inventory_device(key, "controllable", "permit_inhibit", category="hot_water")]
                models, degraded = self.build_with_degraded(devices, complete_history(key),
                    mappings={key: {"control_type": "permit_inhibit", "power": "sensor.boiler_power"}},
                    watts=live_w or None, running_power={key: 3063})
                self.assertEqual(degraded, [])
                self.assertEqual(models[0]["active_power_w"], 3063)
                self.assertEqual(devices[0]["inference"]["running_power_w"], 3063)
        devices = [inventory_device(key, "controllable", "permit_inhibit", category="hot_water")]
        models, degraded = self.build_with_degraded(devices, complete_history(key),
            mappings={key: {"power": "sensor.boiler_power"}}, watts=1274)
        self.assertEqual(models[0]["active_power_w"], 1274)
        self.assertEqual(degraded, [])

    def test_an_observed_boiler_rating_survives_idle_refreshes(self):
        key = "sensor.boiler_energy"
        devices = [inventory_device(key, "controllable", "permit_inhibit", category="hot_water")]
        mapping = {key: {"control_type": "permit_inhibit", "power": "sensor.boiler_power"}}
        self.build_with_degraded(devices, complete_history(key), mappings=mapping,
                                 running_power={key: 3070})
        models, _ = self.build_with_degraded(devices, complete_history(key), mappings=mapping, watts=200)
        self.assertEqual(models[0]["active_power_w"], 3070)

    def test_boiler_daily_energy_uses_day_totals_without_trimming_away_short_heating_bursts(self):
        key = "boiler"
        devices = [inventory_device(key, "controllable", "permit_inhibit", category="hot_water")]
        actuals = []
        for day in range(14):
            when = START - timedelta(days=day + 1)
            kwh = 7 if when.weekday() in (5, 6) else 3
            for quarter in range(96):
                # Short 3 kW runs shift clock position from day to day.
                active = (quarter + day * 7) % 96 < round(kwh / .75)
                actuals.append({"start": (when + timedelta(minutes=15 * quarter)).isoformat(),
                                "device_energy_kwh": {key: kwh / round(kwh / .75) if active else 0}})
        models, _ = self.build_with_degraded(devices, actuals,
            mappings={key: {"power": "sensor.boiler_power", "control_type": "permit_inhibit"}},
            running_power={key: 3000})
        daily_kwh = models[0]["inference"]["daily_energy_kwh_by_weekday"]
        self.assertGreater(daily_kwh[5], daily_kwh[0])
        self.assertGreater(min(daily_kwh.values()), 2.9)
        self.assertLess(max(daily_kwh.values()), 7.1)

    def test_reviewed_watts_win_over_the_learned_average(self) -> None:
        devices = [inventory_device("sensor.heater", "controllable", "setpoint")]
        self.build(
            devices,
            complete_history("sensor.heater"),
            mappings={"sensor.heater": {"power": 4_321}},
            watts=4_321.0,
        )
        self.assertEqual(devices[0]["active_power_w"], 4_321.0)

    def test_a_whole_home_of_devices_is_modelled_not_just_the_first(self) -> None:
        # Every earlier test here used one device, so a bug that only appears
        # on the second one shipped: the power reader was rebound to its own
        # result, which is callable exactly once.
        keys = [f"sensor.heater_{index}" for index in range(5)]
        devices = [
            inventory_device(key, "controllable", "setpoint") for key in keys
        ]
        models = self.build(devices, complete_history(*keys))
        self.assertEqual([model["key"] for model in models], keys)
        for device in devices:
            self.assertEqual(device["active_power_w"], 1_000.0)

    def test_the_power_reader_is_consulted_once_per_device(self) -> None:
        keys = [f"sensor.heater_{index}" for index in range(3)]
        devices = [
            inventory_device(key, "controllable", "setpoint") for key in keys
        ]
        seen: list[dict[str, object]] = []

        def reader(mapping: dict[str, object]) -> float | None:
            seen.append(mapping)
            return None

        build_device_models(
            devices,
            complete_history(*keys),
            HORIZON,
            {},
            mapped_power_w=reader,
            pool_power_w={},
            local_tz=timezone.utc,
        )
        self.assertEqual(len(seen), len(keys))


class UnplannedServiceTests(unittest.TestCase):
    """The gap that hid a connected car from the objective for two days.

    Telemetry is configured per service, in Home Assistant; the control route
    is configured per meter, on the website. Nothing compared the two, so a
    vehicle whose charging meter sat in base load produced `capabilities.ev`
    false, no `ev_battery` in the snapshot, no store, no bid and no diagnostic
    row — while the plan reported "ready" with no errors and no missing inputs.
    """

    EV_OPTIONS = {
        "ev_connected_entity": "binary_sensor.charge_cable",
        "ev_soc_entity": "sensor.car_soc",
        "ev_target_soc_entity": "number.charge_limit",
        "ev_energy_remaining_entity": "sensor.car_energy_remaining",
    }

    METERS = {
        "sensor.car_charging_lifetime_energy": {
            "name": "Car charging energy",
            "category": "ev_charging",
        },
    }

    def test_configured_vehicle_without_a_route_is_reported(self) -> None:
        reports = unplanned_services(
            dict(self.EV_OPTIONS), {"pool", "boiler"}, self.METERS
        )
        self.assertEqual(len(reports), 1)
        self.assertIn("A vehicle is configured here", reports[0])

    def test_the_report_names_what_the_customer_can_see(self) -> None:
        """Option keys name nothing a person has ever been shown.

        The first version of this warning listed `ev_connected_entity` and
        friends, sent the customer looking for settings that do not appear in
        any interface, and told them to change "the EV charging meter" without
        saying which meter or to what.
        """
        report = unplanned_services(
            dict(self.EV_OPTIONS), set(), self.METERS
        )[0]
        # The panel's own field labels and the entity ids they chose.
        self.assertIn("Vehicle connected state", report)
        self.assertIn("binary_sensor.charge_cable", report)
        # The meter to change, by the name the website shows, and the setting.
        self.assertIn("Car charging energy", report)
        self.assertIn("Variable power", report)
        # And none of the internal keys.
        for key in self.EV_OPTIONS:
            self.assertNotIn(key, report)

    def test_a_home_with_no_candidate_meter_is_told_to_add_one(self) -> None:
        report = unplanned_services(dict(self.EV_OPTIONS), set(), {})[0]
        self.assertIn("no meter is classified as ev charging", report)

    def test_a_routed_vehicle_is_silent(self) -> None:
        self.assertEqual(
            unplanned_services(dict(self.EV_OPTIONS), {"ev"}, self.METERS),
            [],
        )

    def test_a_home_without_the_telemetry_is_silent(self) -> None:
        """No vehicle configured is a house without a car, not a gap."""
        self.assertEqual(unplanned_services({}, set(), {}), [])

    def test_blank_option_values_do_not_count_as_evidence(self) -> None:
        self.assertEqual(
            unplanned_services(
                {"ev_connected_entity": "   ", "ev_soc_entity": ""}, set(), {}
            ),
            [],
        )

    def test_pool_telemetry_without_a_route_is_reported(self) -> None:
        reports = unplanned_services(
            {"pool_water_temperature_entity": "sensor.pool_water"},
            {"ev"},
            {"sensor.pool_heater_energy": {
                "name": "Pool heater", "category": "pool_heating",
            }},
        )
        self.assertEqual(len(reports), 1)
        self.assertIn("A pool is configured here", reports[0])
        self.assertIn("Pool heater", reports[0])
        self.assertIn("Switch schedule", reports[0])

    def test_every_unrouted_service_is_reported_together(self) -> None:
        """One gap must not hide the next, as a first-failure raise would."""
        reports = unplanned_services(
            {**self.EV_OPTIONS, "pool_water_temperature_entity": "sensor.pool"},
            set(),
            self.METERS,
        )
        self.assertEqual(len(reports), 2)


class VehicleStateWithoutAControlRouteTests(unittest.TestCase):
    """A car the planner may not control is still a car it should report.

    Vehicle state and the charger's control route are configured on opposite
    sides of the boundary, and `build_services` used to read the first only
    when the second existed. An unrouted charging meter therefore produced no
    `ev_battery` at all, so the server could not so much as name the store it
    was declining to plan.
    """

    ENTITIES = {
        "binary_sensor.cable": {"state": "on", "attributes": {}},
        "sensor.car_soc": {"state": "67", "attributes": {}},
        "number.charge_limit": {"state": "80", "attributes": {}},
        "sensor.car_energy_remaining": {"state": "51.5", "attributes": {}},
    }

    OPTIONS = {
        "device_control_mappings": {},
        "ev_connected_entity": "binary_sensor.cable",
        "ev_soc_entity": "sensor.car_soc",
        "ev_target_soc_entity": "number.charge_limit",
        "ev_energy_remaining_entity": "sensor.car_energy_remaining",
    }

    def _build(self):
        return build_services(
            dict(self.OPTIONS),
            HORIZON,
            [],  # no device model routes to "ev"
            read_entity=lambda entity_id: self.ENTITIES[entity_id],
            local_tz=timezone.utc,
        )

    def test_an_unrouted_charger_still_reports_the_vehicle(self) -> None:
        services, _samples, ev_battery = self._build()
        self.assertIsNotNone(ev_battery)
        self.assertTrue(ev_battery["connected"])
        self.assertAlmostEqual(ev_battery["soc"], 0.67, places=4)
        self.assertAlmostEqual(ev_battery["charge_limit_soc"], 0.8, places=4)
        # 51.5 kWh remaining at 67% is a 76.9 kWh pack.
        self.assertAlmostEqual(ev_battery["capacity_kwh"], 76.866, places=2)

    def test_an_unrouted_charger_produces_no_service(self) -> None:
        """Reported, not planned: there is no actuator to hand a schedule to."""
        services, _samples, _ev = self._build()
        self.assertEqual([s for s in services if s["device"] == "ev"], [])

    def test_the_charge_current_source_is_null_without_a_route(self) -> None:
        _services, _samples, ev_battery = self._build()
        self.assertIsNone(ev_battery["source_entity_ids"]["charge_current"])

    def test_a_home_with_no_vehicle_configured_reports_none(self) -> None:
        _services, _samples, ev_battery = build_services(
            {"device_control_mappings": {}},
            HORIZON,
            [],
            read_entity=lambda entity_id: self.ENTITIES[entity_id],
            local_tz=timezone.utc,
        )
        self.assertIsNone(ev_battery)


if __name__ == "__main__":
    unittest.main()


class StoreEnabledTests(unittest.TestCase):
    """A store switched off is a home without the equipment.

    That is a different claim from "configured but not routed", which
    `unplanned_services` exists to report and which sends the customer to the
    website to fix a meter. Confusing the two would nag every home that
    deliberately took a store out of planning.
    """

    def test_an_absent_key_leaves_the_store_planned(self) -> None:
        """The upgrade path. `optimisation_defaults` supplies True, but an
        options dict written before these keys existed carries neither, and
        reading a missing key as off would silently unplan every such home."""
        self.assertEqual(disabled_store_paths({}), set())

    def test_only_an_explicit_false_switches_a_store_off(self) -> None:
        self.assertEqual(disabled_store_paths({"pool_enabled": True}), set())
        self.assertEqual(disabled_store_paths({"pool_enabled": False}), {"pool"})
        self.assertEqual(
            disabled_store_paths({"pool_enabled": False, "ev_enabled": False}),
            {"pool", "ev"},
        )

    def test_a_switched_off_pool_builds_no_service(self) -> None:
        services, _samples, _battery = build_services(
            {
                "device_control_mappings": PoolServiceTests.mappings,
                "pool_enabled": False,
            },
            HORIZON,
            [PoolServiceTests.pool_switch],
            read_entity=lambda entity_id: self.fail(
                f"a switched-off pool must not read {entity_id}"
            ),
            local_tz=timezone.utc,
        )
        self.assertEqual([s for s in services if s["device"] == "pool"], [])

    def test_a_switched_off_vehicle_is_neither_planned_nor_published(self) -> None:
        """Both halves. The control route and the car's own state are read in
        separate branches — an unrouted charger still publishes the vehicle so
        it can be reported as undispatched — so switching the store off has to
        silence both or the snapshot carries a store nothing will bid for."""
        services, _samples, battery = build_services(
            {**EvServiceTests.options, "ev_enabled": False},
            HORIZON,
            [EvServiceTests.charger],
            read_entity=lambda entity_id: self.fail(
                f"a switched-off vehicle must not read {entity_id}"
            ),
            local_tz=timezone.utc,
        )
        self.assertEqual([s for s in services if s["device"] == "ev"], [])
        self.assertIsNone(battery)

    def test_a_switched_off_store_is_not_reported_as_unrouted(self) -> None:
        configured = {
            "pool_water_temperature_entity": "sensor.pool_temperature",
        }
        self.assertEqual(len(unplanned_services(configured, set(), {})), 1)
        self.assertEqual(
            unplanned_services({**configured, "pool_enabled": False}, set(), {}),
            [],
        )


class GridImportLimitTests(unittest.TestCase):
    """The plan's import limit is the main fuse answered on the website.

    Automatic setup used to fill it from the inverter's power rating, so a home
    fused for 17.2 kW was planned to 13.2 with no setting on screen to correct.
    """

    def test_only_the_resolved_website_house_limit_gives_the_limit(self):
        from household_fixture import Rig
        from shs_core.configuration_schema import OPTION_KEYS, resolve_configuration
        h = Rig(options={"grid_import_limit_w": 13200}).household
        h.tariff_catalog = {"grid_import_limit_w": 15000, "configuration": {"fuse_a": 25}}
        self.assertEqual(h.house_import_limit_w(), 15000)
        self.assertNotIn("grid_import_limit_w", OPTION_KEYS)
        self.assertNotIn("grid_import_limit_w", resolve_configuration({"grid_import_limit_w": 13200}))
        for missing in (None, {}, {"configuration": {"fuse_a": 25}}):
            h.tariff_catalog = missing
            self.assertIsNone(h.house_import_limit_w())

    def test_the_inverter_rating_is_an_export_limit_and_never_an_import_limit(self):
        from types import SimpleNamespace
        from shs_core.discovery import DiscoveryCatalog, discover_configuration
        rating = SimpleNamespace(entity_id="sensor.sigen_plant_max_active_power", state="13.2",
                                 attributes={"friendly_name": "Sigen Plant Max Active Power", "unit_of_measurement": "kW"})
        result = discover_configuration(DiscoveryCatalog({rating.entity_id: rating}, {"energy_sources": []}, 59, 18), {})
        self.assertEqual(result["configuration"]["grid_export_limit_w"], 13_200)
        self.assertFalse(result["configuration"].get("grid_import_limit_w"))
        self.assertNotIn("grid_import_limit_w", result["evidence"])


class PoolHeaterSettingsTests(unittest.TestCase):
    """The heat pump's own start and stop temperatures travel with every snapshot.

    They were typed into the server once per environment, so production had
    none and refused every plan, and the test copy went stale the day the
    heat pump was adjusted.
    """
    options = {
        "pool_start_temperature_entity": "number.pool_start",
        "pool_stop_temperature_entity": "number.pool_stop",
    }
    states = {"number.pool_start": {"state": "33.5"}, "number.pool_stop": {"state": "34.0"}}

    def test_current_settings_are_published_with_their_sources(self):
        self.assertEqual(pool_heater_settings(self.options, self.states.__getitem__), {
            "start_c": 33.5, "stop_c": 34.0, "control": "external_enable",
            "source_entity_ids": {"start": "number.pool_start", "stop": "number.pool_stop"},
        })

    def test_every_unselected_setting_is_named_for_correction(self):
        for options, missing in (
            ({}, ["pool_start_temperature_entity", "pool_stop_temperature_entity"]),
            ({"pool_start_temperature_entity": "number.pool_start"}, ["pool_stop_temperature_entity"]),
            ({**self.options, "pool_start_temperature_entity": " "}, ["pool_start_temperature_entity"]),
        ):
            with self.subTest(missing=missing), self.assertRaises(OptimisationInputError) as raised:
                pool_heater_settings(options, lambda entity: self.fail(f"read {entity} before setup was complete"))
            self.assertEqual(raised.exception.fix, {"kind": "fields", "fields": [{"key": key} for key in missing]})

    def test_a_setting_that_is_not_a_number_is_refused_rather_than_guessed(self):
        states = {**self.states, "number.pool_stop": {"state": "auto"}}
        with self.assertRaises(OptimisationInputError):
            pool_heater_settings(self.options, states.__getitem__)


class PoolRunningStateTests(unittest.TestCase):
    def test_pool_continuation_uses_switches_and_companions_not_power_or_floor_heat(self):
        models = [PoolServiceTests.pool_switch, PoolServiceTests.pool_pump, PoolServiceTests.pool_room]
        mappings = {key: dict(value) for key, value in PoolServiceTests.mappings.items()}
        mappings["sensor.pool_heater_energy"]["companion_actuator_entity_ids"] = ["switch.flow"]
        states = {"switch.pool_heater": "on", "switch.pool_pump": "on", "switch.flow": "on"}
        self.assertIs(pool_heating_running(models, mappings, states.get), True)
        states["switch.flow"] = "off"
        self.assertIs(pool_heating_running(models, mappings, states.get), False)
        states["switch.flow"] = "unavailable"
        self.assertIsNone(pool_heating_running(models, mappings, states.get))
        states["switch.pool_heater"] = "off"
        self.assertIs(pool_heating_running(models, mappings, states.get), False)

    def test_unknown_or_unmapped_actuators_never_claim_a_running_heater(self):
        model = PoolServiceTests.pool_switch
        self.assertIsNone(pool_heating_running([model], PoolServiceTests.mappings, lambda _: None))
        self.assertIsNone(pool_heating_running([model], {}, lambda _: "on"))
        self.assertIsNone(pool_heating_running([], {}, lambda _: "on"))


class PoolHeaterRuntimeTests(unittest.TestCase):
    def test_recorder_reconnect_preserves_confirmed_run_start(self):
        from shs_core.planning import pool_heating_runtime
        rows = [(START, 'off', None), (START + timedelta(minutes=1), 'on', None),
                (START + timedelta(minutes=20), 'unavailable', None),
                (START + timedelta(minutes=21), 'on', None)]
        runtime = pool_heating_runtime({'switch.heat'}, {'switch.heat': rows}, START + timedelta(minutes=30))
        self.assertEqual(runtime, {'heating_elapsed_seconds': 29 * 60, 'heating_elapsed_basis': 'confirmed_transition'})

    def test_true_off_then_on_starts_a_new_run(self):
        from shs_core.planning import pool_heating_runtime
        rows = [(START, 'on', None), (START + timedelta(minutes=20), 'off', None),
                (START + timedelta(minutes=25), 'on', None)]
        runtime = pool_heating_runtime({'switch.heat'}, {'switch.heat': rows}, START + timedelta(minutes=30))
        self.assertEqual(runtime['heating_elapsed_seconds'], 5 * 60)
        self.assertEqual(runtime['heating_elapsed_basis'], 'confirmed_transition')

    def test_first_on_without_transition_is_labeled_as_age_lower_bound(self):
        from shs_core.planning import pool_heating_runtime
        rows = [(START, 'unavailable', None), (START + timedelta(minutes=25), 'on', None)]
        runtime = pool_heating_runtime({'switch.heat'}, {'switch.heat': rows}, START + timedelta(minutes=30))
        self.assertEqual(runtime, {'heating_elapsed_seconds': 5 * 60, 'heating_elapsed_basis': 'observed_on_lower_bound'})

    def heating_history(self, rows, captured_minutes=14, entities=None):
        from shs_core.planning import pool_heating_history
        return pool_heating_history(entities or {'switch.heat'}, rows,
                                    START, START + timedelta(minutes=captured_minutes))

    def test_completed_partial_runs_and_multiple_cycles_survive_an_off_capture(self):
        rows = [(START - timedelta(minutes=10), 'off', None),
                (START + timedelta(minutes=1), 'on', None),
                (START + timedelta(minutes=4), 'off', None),
                (START + timedelta(minutes=7), 'on', None),
                (START + timedelta(minutes=12), 'off', None)]
        result = self.heating_history({'switch.heat': rows})
        self.assertTrue(result['complete'])
        self.assertEqual(result['start'], START.isoformat())
        self.assertEqual(result['end'], (START + timedelta(minutes=14)).isoformat())
        self.assertEqual(result['intervals'], [
            {'start': (START + timedelta(minutes=left)).isoformat(),
             'end': (START + timedelta(minutes=right)).isoformat(),
             'elapsed_seconds_at_start': 0, 'elapsed_basis': 'confirmed_transition'}
            for left, right in [(1, 4), (7, 12)]])

    def test_interval_clipping_preserves_startup_age_before_the_current_quarter(self):
        rows = [(START - timedelta(minutes=20), 'off', None),
                (START - timedelta(minutes=5), 'on', None),
                (START + timedelta(minutes=2), 'on', None),
                (START + timedelta(minutes=8), 'off', None)]
        result = self.heating_history({'switch.heat': rows})
        self.assertTrue(result['complete'])
        self.assertEqual(result['intervals'], [{
            'start': START.isoformat(), 'end': (START + timedelta(minutes=8)).isoformat(),
            'elapsed_seconds_at_start': 300, 'elapsed_basis': 'confirmed_transition'}])

    def test_initial_on_observation_preserves_a_labeled_elapsed_lower_bound(self):
        result = self.heating_history({'switch.heat': [(START - timedelta(minutes=5), 'on', None)]})
        self.assertTrue(result['complete'])
        self.assertEqual(result['intervals'][0]['elapsed_seconds_at_start'], 300)
        self.assertEqual(result['intervals'][0]['elapsed_basis'], 'observed_on_lower_bound')

    def test_companion_must_be_on_and_its_transition_sets_the_combined_run_age(self):
        rows = {'switch.heat': [(START - timedelta(minutes=20), 'off', None),
                                (START - timedelta(minutes=10), 'on', None)],
                'switch.flow': [(START - timedelta(minutes=20), 'off', None),
                                (START - timedelta(minutes=2), 'on', None),
                                (START + timedelta(minutes=8), 'off', None)]}
        result = self.heating_history(rows, entities=set(rows))
        self.assertTrue(result['complete'])
        self.assertEqual(result['intervals'], [{
            'start': START.isoformat(), 'end': (START + timedelta(minutes=8)).isoformat(),
            'elapsed_seconds_at_start': 120, 'elapsed_basis': 'confirmed_transition'}])

    def test_unknown_history_retains_known_intervals_without_claiming_complete_coverage(self):
        rows = {'switch.heat': [(START + timedelta(minutes=1), 'on', None),
                                (START + timedelta(minutes=4), 'unavailable', None),
                                (START + timedelta(minutes=7), 'on', None),
                                (START + timedelta(minutes=12), 'off', None)]}
        result = self.heating_history(rows)
        self.assertFalse(result['complete'])
        self.assertEqual([(row['start'], row['end']) for row in result['intervals']], [
            ((START + timedelta(minutes=left)).isoformat(), (START + timedelta(minutes=right)).isoformat())
            for left, right in [(1, 4), (7, 12)]])
        self.assertTrue(all(row['elapsed_basis'] == 'observed_on_lower_bound' for row in result['intervals']))
        self.assertFalse(self.heating_history({})['complete'])
        self.assertTrue(self.heating_history({'switch.heat': [(START - timedelta(minutes=1), 'off', None)]})['complete'])
        self.assertEqual(self.heating_history({'switch.heat': [(START - timedelta(minutes=1), 'off', None)]})['intervals'], [])


    def test_transient_command_controls_even_when_expected_draw_is_zero(self):
        from shs_core.device_port import device_intent
        plan = {'pool': {'heater_response': {'kind': 'bergvarme'}, 'stop_temperature_c': 32}, 'device_models': []}
        slot = {'start': START.isoformat(), 'pool_w': 0, 'pool_command_w': 3000}
        intent = device_intent('pool', 'apply', plan, slot, configuration_revision=1, policy_revision=1)
        self.assertEqual(intent['parameters']['heating_w'], 3000)
        with self.assertRaises(KeyError):
            device_intent('pool', 'apply', plan, {'start': START.isoformat(), 'pool_w': 0}, configuration_revision=1, policy_revision=1)

class ThermostatFactsTests(unittest.TestCase):
    def setUp(self):
        from types import SimpleNamespace
        self.mapping = dict(control_type='setpoint', actuator_entity_ids=['climate.bathroom'])
        self.state = SimpleNamespace(state='heat', attributes=dict(min_temp=5, max_temp=90,
            target_temp_step=1, current_temperature=21, temperature=21, hvac_action='idle'))

    def facts(self):
        from shs_core.planning import thermostat_facts
        return thermostat_facts(self.mapping, lambda entity: self.state)

    def test_own_temperature_target_and_hvac_latch_travel_with_native_steps(self):
        self.assertEqual(self.facts(), dict(entity_id='climate.bathroom', minimum_c=5, maximum_c=90,
            step_c=1, current_temperature_c=21, target_temperature_c=21, hvac_action='idle'))
        self.state.attributes['hvac_action'] = 'heating'
        self.assertEqual(self.facts()['hvac_action'], 'heating')
        self.assertNotIn('thermostat', self.mapping)

    def test_unknown_evidence_is_not_an_ideal_thermostat(self):
        for key in ('current_temperature', 'temperature', 'hvac_action', 'target_temp_step'):
            saved = self.state.attributes.pop(key)
            self.assertIsNone(self.facts())
            self.state.attributes[key] = saved
        self.state.state = 'unavailable'
        self.assertIsNone(self.facts())

    def test_number_targets_cannot_invent_their_own_sensor_or_hvac_action(self):
        self.mapping['actuator_entity_ids'] = ['number.target']
        self.assertIsNone(self.facts())

class PoolElectricalEvidenceTests(unittest.TestCase):
    def test_counter_deltas_survive_switch_gaps_but_not_missing_endpoints_or_resets(self):
        from shs_core.planning import pool_electrical_history
        model={"key":"heater","statistic_id":"sensor.energy","planning_service":"pool"}
        attrs={"unit_of_measurement":"kWh","state_class":"total_increasing"}
        def capture(values):
            rows=[(START+timedelta(seconds=i*20),v,attrs) for i,v in enumerate(values)]
            return pool_electrical_history([model],{"sensor.energy":rows},START,START+timedelta(minutes=2))["counters"]
        self.assertEqual(capture(["100","unavailable","100"]),
                         [{"device_key":"heater","start_kwh":100.,"end_kwh":100.}])
        self.assertEqual(capture(["100","100.1"])[0]["end_kwh"],100.1)
        for values in (["unavailable","100"],["100","unavailable"],["100","0","101"],["100","nan"],["100","99"]):
            with self.subTest(values=values): self.assertEqual(capture(values),[])
        for unit,factor in (("Wh",.001),("MWh",1000),("W",None)):
            attrs["unit_of_measurement"]=unit
            result=capture(["100","101"])
            if factor is None: self.assertEqual(result,[])
            else: self.assertAlmostEqual(result[0]["end_kwh"]-result[0]["start_kwh"],factor)
