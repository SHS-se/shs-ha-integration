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

from optimisation import OptimisationInputError  # noqa: E402
from planning import (  # noqa: E402
    build_device_models,
    build_services,
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

    def plan(self, models: list[dict[str, object]], **kwargs: object):
        return build_services(
            {"device_control_mappings": self.mappings},
            kwargs.get("daily_changes", daily(
                **{
                    "sensor.pool_heater_energy": 8.0,
                    "sensor.pool_pump_energy": 2.0,
                    "sensor.pool_room_floor_heater_energy": 5.0,
                }
            )),
            kwargs.get("device_actuals", []),
            HORIZON,
            models,
            read_entity=lambda entity_id: self.fail(
                f"a pool plan must not read {entity_id}"
            ),
            local_tz=timezone.utc,
            today=TODAY,
        )

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
        # Its own meters measured 8 + 2 kWh a day, not 8 + 2 + 5.
        self.assertEqual(with_room[0]["required_kwh"], 10.0)

    def test_the_service_is_rated_from_its_own_devices(self) -> None:
        services, _samples, _ev = self.plan(
            [self.pool_switch, self.pool_pump, self.pool_room]
        )
        self.assertEqual(services[0]["control"]["power_w"], 6_000)

    def test_energy_already_delivered_today_reduces_only_today(self) -> None:
        actuals = [{
            "start": START.isoformat(),
            "device_energy_kwh": {
                "sensor.pool_heater_energy": 4.0,
                "sensor.pool_room_floor_heater_energy": 3.0,
            },
        }]
        services, _samples, _ev = self.plan(
            [self.pool_switch, self.pool_pump, self.pool_room],
            device_actuals=actuals,
        )
        today_service = next(
            service for service in services if service["id"].endswith(TODAY.isoformat())
        )
        # 10 required, 4 delivered by this service's own meters; the room
        # heater's 3 kWh belong to its room, not to the pool.
        self.assertEqual(today_service["required_kwh"], 6.0)

    def test_a_pool_with_too_few_measured_days_is_reported_not_hidden(self) -> None:
        sparse = {
            day: values
            for index, (day, values) in enumerate(
                daily(**{
                    "sensor.pool_heater_energy": 8.0,
                    "sensor.pool_pump_energy": 2.0,
                }).items()
            )
            if index < 3
        }
        with self.assertRaises(OptimisationInputError) as caught:
            self.plan([self.pool_switch, self.pool_pump], daily_changes=sparse)
        self.assertIn("measured active days", str(caught.exception))


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
            {},
            [],
            HORIZON,
            [boiler],
            read_entity=lambda entity_id: self.fail("no entity read is needed"),
            local_tz=timezone.utc,
            today=TODAY,
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
                {},
                [],
                HORIZON,
                [boiler],
                read_entity=lambda entity_id: self.fail("no entity read is needed"),
                local_tz=timezone.utc,
                today=TODAY,
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
            {},
            [],
            HORIZON,
            [self.charger],
            read_entity=lambda entity_id: self.states[entity_id],
            local_tz=timezone.utc,
            today=TODAY,
        )

    def test_a_connected_car_without_a_departure_charges_across_the_horizon(
        self,
    ) -> None:
        services, _samples, battery = self.plan(self.options)
        self.assertIsNone(battery["departure"])
        self.assertTrue(battery["connected"])
        deadline = datetime.fromisoformat(services[0]["deadline"])
        self.assertEqual(deadline, HORIZON[-1] + timedelta(minutes=15))

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
            {},
            [],
            HORIZON,
            [heater],
            read_entity=lambda entity_id: self.fail("no entity read is needed"),
            local_tz=timezone.utc,
            today=TODAY,
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
        self.assertEqual(len(caught.exception.reasons), 3)
        self.assertIn("sensor.a has an invalid planning role", caught.exception.reasons[0])
        self.assertIn("sensor.b has an invalid planning role", caught.exception.reasons[1])
        self.assertIn("sensor.c needs a complete empirical profile", caught.exception.reasons[2])

    def test_a_base_load_device_without_history_is_skipped_quietly(self) -> None:
        devices = [inventory_device("sensor.quiet", "base_load", None)]
        self.assertEqual(self.build(devices, []), [])

    def test_a_controllable_device_becomes_a_model_for_every_slot(self) -> None:
        devices = [inventory_device("sensor.heater", "controllable", "setpoint")]
        models = self.build(devices, complete_history("sensor.heater"))
        self.assertEqual(len(models), 1)
        self.assertEqual(len(models[0]["forecast_w_by_slot"]), len(HORIZON))
        # 0.25 kWh in a quarter is a 1 kW draw.
        self.assertEqual(set(models[0]["forecast_w_by_slot"]), {1_000.0})
        self.assertEqual(models[0]["planning_role"], "controllable")
        self.assertEqual(models[0]["load_type"], "duty_cycle")

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

    def test_configured_vehicle_without_a_route_is_reported(self) -> None:
        reports = unplanned_services(dict(self.EV_OPTIONS), {"pool", "boiler"})
        self.assertEqual(len(reports), 1)
        self.assertIn("a vehicle is configured", reports[0])
        self.assertIn("ev_connected_entity", reports[0])
        self.assertIn("variable-power", reports[0])

    def test_a_routed_vehicle_is_silent(self) -> None:
        self.assertEqual(
            unplanned_services(dict(self.EV_OPTIONS), {"ev"}),
            [],
        )

    def test_a_home_without_the_telemetry_is_silent(self) -> None:
        """No vehicle configured is a house without a car, not a gap."""
        self.assertEqual(unplanned_services({}, set()), [])

    def test_blank_option_values_do_not_count_as_evidence(self) -> None:
        self.assertEqual(
            unplanned_services(
                {"ev_connected_entity": "   ", "ev_soc_entity": ""}, set()
            ),
            [],
        )

    def test_pool_telemetry_without_a_route_is_reported(self) -> None:
        reports = unplanned_services(
            {"pool_water_temperature_entity": "sensor.pool_water"}, {"ev"}
        )
        self.assertEqual(len(reports), 1)
        self.assertIn("a pool is configured", reports[0])
        self.assertIn("switch-schedule", reports[0])

    def test_every_unrouted_service_is_reported_together(self) -> None:
        """One gap must not hide the next, as a first-failure raise would."""
        reports = unplanned_services(
            {**self.EV_OPTIONS, "pool_water_temperature_entity": "sensor.pool"},
            set(),
        )
        self.assertEqual(len(reports), 2)


if __name__ == "__main__":
    unittest.main()
