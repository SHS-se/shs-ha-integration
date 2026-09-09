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

sys.path.insert(0, str(Path(__file__).parents[1] / "custom_components" / "shs_energy"))

from optimisation import (  # noqa: E402
    OptimisationInputError,
    build_base_load_model,
)
from planning import (  # noqa: E402
    build_device_models,
    build_services,
    disabled_store_paths,
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
            self.assertEqual(
                service["control"]["max_consecutive_inhibit_slots"], 20
            )

    def test_expected_power_above_the_reviewed_rating_is_refused(self) -> None:
        boiler = device(
            "sensor.hot_water_energy", "hot_water", "permit_inhibit",
            watts=300, forecast_w=350,
        )
        with self.assertRaises(OptimisationInputError) as caught:
            build_services(
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
        self.assertIn("exceeds its reviewed rating", str(caught.exception))


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

    def test_a_connected_car_without_a_departure_charges_across_the_horizon(
        self,
    ) -> None:
        services, _samples, battery = self.plan(self.options)
        self.assertIsNone(battery["departure"])
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

    def test_a_stated_departure_becomes_the_deadline(self) -> None:
        departure = HORIZON[40]
        self.states["sensor.departure"] = {
            "state": departure.isoformat(), "attributes": {}
        }
        services, _samples, battery = self.plan({
            **self.options,
            "ev_departure_entity": "sensor.departure",
        })
        self.assertEqual(datetime.fromisoformat(battery["departure"]), departure)
        self.assertEqual(
            datetime.fromisoformat(services[0]["deadline"]), departure
        )

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

    def test_a_departure_outside_the_horizon_is_refused(self) -> None:
        self.states["sensor.departure"] = {
            "state": (HORIZON[-1] + timedelta(days=2)).isoformat(),
            "attributes": {},
        }
        with self.assertRaises(OptimisationInputError) as caught:
            self.plan({**self.options, "ev_departure_entity": "sensor.departure"})
        self.assertIn("inside the 72-hour horizon", str(caught.exception))


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
    def build(self, devices, actuals, mappings=None, watts=None):
        models, _degraded = build_device_models(
            devices,
            actuals,
            HORIZON,
            mappings or {},
            mapped_power_w=lambda mapping: watts,
            local_tz=timezone.utc,
        )
        return models

    def build_with_degraded(self, devices, actuals, mappings=None, watts=None):
        return build_device_models(
            devices,
            actuals,
            HORIZON,
            mappings or {},
            mapped_power_w=lambda mapping: watts,
            local_tz=timezone.utc,
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
        self.assertEqual(models, [])
        self.assertIn("configured running power", degraded[0]["reason"])

    def test_boiler_rating_alone_does_not_supply_hot_water_demand(self) -> None:
        devices = [inventory_device("sensor.boiler", "controllable", "permit_inhibit",
                                    category="hot_water")]
        models, degraded = self.build_with_degraded(devices, [], watts=3000)
        self.assertEqual(models, [])
        self.assertIn("hot-water demand", degraded[0]["reason"])

    def test_eleven_hours_of_history_can_supply_running_power(self) -> None:
        devices = [inventory_device("sensor.new", "controllable", "switch_schedule",
                                    category="pool_heating")]
        history = complete_history("sensor.alive")
        for row in history[-44:]:
            row["device_energy_kwh"]["sensor.new"] = 0.193
        models, degraded = self.build_with_degraded(devices, history)
        self.assertEqual(len(models), 1)
        self.assertEqual(degraded, [])
        self.assertEqual(models[0]["active_power_w"], 772)
        self.assertEqual(models[0]["profile_status"], "warming")

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
        # Each device consumes 1 kW; do not leave the new one in base load
        # and then count its future schedule on top of it.
        self.assertEqual({row["median_w"] for row in model["by_weekday"][0]}, {1000})

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
        self.assertAlmostEqual(ev_battery["departure_target_soc"], 0.8, places=4)
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
