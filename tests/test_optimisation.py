"""Pure tests for quarter-hour aggregation and forecast normalization."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).parents[1] / "custom_components" / "shs_energy"))

from optimisation import (  # noqa: E402
    OptimisationInputError,
    aggregate_category_changes,
    aggregate_device_changes,
    build_base_load_model,
    build_device_load_model,
    calibration_summary,
    discrete_current_control,
    extract_timestamped_forecast,
    optimisation_plan_due,
    require_fresh_source,
    service_daily_energy,
    service_energy_today,
    suggested_device_planning,
    suggested_load_type,
    utc_slots,
    validate_service_windows,
    validate_plan_contract,
)


class QuarterAggregationTests(unittest.TestCase):
    def test_load_characteristics_are_suggested_from_device_semantics(self) -> None:
        cases = (
            ("Workshop extractor", "household", "fixed_full_load"),
            ("Tesla charging", "ev_charging", "variable_full_load"),
            ("Water boiler", "hot_water", "duty_cycle"),
            ("Living room aircon", "cooling", "inverter"),
        )
        for name, category, expected in cases:
            with self.subTest(name=name):
                load_type, inference = suggested_load_type(name, category)
                self.assertEqual(load_type, expected)
                self.assertEqual(
                    inference["method"], "energy_dashboard_semantics_v1"
                )

    def test_new_devices_always_require_explicit_control_opt_in(self) -> None:
        cases = (
            ("hot_water", "duty_cycle", "base_load", None),
            ("pool_heating", "duty_cycle", "base_load", None),
            ("ev_charging", "variable_full_load", "base_load", None),
            ("cooling", "inverter", "base_load", None),
            ("household", "fixed_full_load", "base_load", None),
        )
        for category, load_type, role, control in cases:
            with self.subTest(category=category):
                suggested_role, suggested_control, inference = (
                    suggested_device_planning(category, load_type)
                )
                self.assertEqual(suggested_role, role)
                self.assertEqual(suggested_control, control)
                self.assertEqual(
                    inference["method"],
                    "energy_dashboard_planning_semantics_v1",
                )

    def test_three_five_minute_changes_become_one_quarter(self) -> None:
        start = datetime(2026, 8, 10, 8, 0, tzinfo=timezone.utc)
        rows = [(start + timedelta(minutes=5 * index), 0.1) for index in range(3)]
        result = aggregate_category_changes({
            "total_consumption": rows,
            "solar_production": [
                (start, 0.2),
                (start + timedelta(minutes=5), 0.1),
                (start + timedelta(minutes=10), 0.2),
            ],
            "battery_charge": [
                (start + timedelta(minutes=5 * index), 0.05)
                for index in range(3)
            ],
        })

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["total_load_kwh"], 0.3)
        self.assertEqual(result[0]["solar_production_kwh"], 0.5)
        self.assertEqual(result[0]["battery_charge_kwh"], 0.15)
        self.assertEqual(result[0]["quality"]["duration_seconds"], 900)

    def test_incomplete_five_minute_bucket_is_not_published(self) -> None:
        start = datetime(2026, 8, 10, 8, 0, tzinfo=timezone.utc)
        result = aggregate_category_changes({
            "total_consumption": [(start, 0.1), (start + timedelta(minutes=5), 0.1)],
        })

        self.assertEqual(result, [])

    def test_device_quarters_remain_separate_and_feed_expected_power(self) -> None:
        start = datetime(2026, 8, 3, tzinfo=timezone.utc)
        changes = {"sensor.water_boiler_energy": []}
        for day in range(7):
            for quarter in range(96):
                quarter_start = start + timedelta(days=day, minutes=quarter * 15)
                energy = 0.25 if quarter % 8 == 0 else 0.0
                changes["sensor.water_boiler_energy"].extend([
                    (quarter_start + timedelta(minutes=offset), energy / 3)
                    for offset in (0, 5, 10)
                ])
        slots = aggregate_device_changes(changes)
        model = build_device_load_model(
            slots,
            "sensor.water_boiler_energy",
            "UTC",
            minimum_samples=2,
        )
        profile = model["by_weekday"][0]

        self.assertEqual(len(slots), 7 * 96)
        self.assertEqual(profile[0], 1000)
        self.assertEqual(profile[1], 0)
        self.assertEqual(model["active_power_w"], 1000)

    def test_device_profile_keeps_an_intermittent_load_off_the_median(
        self,
    ) -> None:
        """A thermostat that runs one quarter in eight must not forecast zero.

        The centre statistic stays a trimmed mean for exactly this case: a
        median would report 0 W for seven quarters in eight and the planner
        would size the service from nothing.
        """
        start = datetime(2026, 6, 1, tzinfo=timezone.utc)
        changes = {"sensor.boiler_energy": []}
        for day in range(14):
            for quarter in range(96):
                quarter_start = start + timedelta(days=day, minutes=quarter * 15)
                energy = 0.25 if quarter % 8 == 0 else 0.0
                changes["sensor.boiler_energy"].extend([
                    (quarter_start + timedelta(minutes=offset), energy / 3)
                    for offset in (0, 5, 10)
                ])
        model = build_device_load_model(
            aggregate_device_changes(changes),
            "sensor.boiler_energy",
            "UTC",
            minimum_samples=2,
        )

        self.assertGreater(model["by_weekday"][0][0], 500)

    def test_device_profile_separates_days_of_the_week(self) -> None:
        """Hot water on a Sunday is not hot water on a Tuesday."""
        monday = datetime(2026, 6, 1, tzinfo=timezone.utc)
        changes = {"sensor.boiler_energy": []}
        for day in range(28):
            when_day = monday + timedelta(days=day)
            # Twice the hot water on Sundays, when the house is home all day.
            scale = 2.0 if when_day.weekday() == 6 else 1.0
            for quarter in range(96):
                quarter_start = when_day + timedelta(minutes=quarter * 15)
                energy = 0.1 * scale if quarter % 4 == 0 else 0.0
                changes["sensor.boiler_energy"].extend([
                    (quarter_start + timedelta(minutes=offset), energy / 3)
                    for offset in (0, 5, 10)
                ])
        model = build_device_load_model(
            aggregate_device_changes(changes),
            "sensor.boiler_energy",
            "UTC",
            minimum_samples=2,
        )

        tuesday = model["by_weekday"][1][0]
        sunday = model["by_weekday"][6][0]
        self.assertGreater(sunday, tuesday)
        # And two ordinary weekdays are no longer byte-identical by
        # construction — they may agree, but only because the evidence does.
        self.assertEqual(
            len(model["by_weekday"]), 7, "every weekday carries its own series"
        )

    def test_base_profile_subtracts_only_controllable_devices(self) -> None:
        start = datetime(2026, 8, 1, tzinfo=timezone.utc)
        rows = []
        device_rows = []
        for day in range(4):
            for quarter in range(96):
                # Three ordinary days have 1 kW baseload; one 20 kW outlier
                # must not become the forecast shape for every future day.
                base_kwh = 5.0 if day == 3 else 0.25
                when = start + timedelta(days=day, minutes=quarter * 15)
                rows.append({
                    "start": when.isoformat(),
                    "total_load_kwh": base_kwh + 0.625,
                })
                device_rows.append({
                    "start": when.isoformat(),
                    "device_energy_kwh": {
                        "pool-heater": 0.5,
                        "fridge": 0.125,
                    },
                })
        model = build_base_load_model(
            rows,
            "UTC",
            device_slots=device_rows,
            modelled_device_keys=("pool-heater",),
        )
        profile = model["by_weekday"][0]

        self.assertEqual(len(profile), 96)
        # The controllable pool heater is removed. The non-controllable fridge
        # remains inside the empirical base load learned from whole-home usage.
        # The 20 kW outlier day must not drag the shared shape with it.
        self.assertLess(profile[0]["median_w"], 3_000)
        self.assertGreater(profile[0]["p90_w"], profile[0]["median_w"])

    def test_base_model_separates_every_day_of_the_week(self) -> None:
        """Saturday and Sunday must not share one weekend expectation."""
        monday = datetime(2026, 6, 1, tzinfo=timezone.utc)
        rows = []
        for day in range(28):
            when_day = monday + timedelta(days=day)
            # A quiet Sunday, a busy Saturday, an ordinary week.
            if when_day.weekday() == 6:
                kwh = 0.125
            elif when_day.weekday() == 5:
                kwh = 0.5
            else:
                kwh = 0.25
            for quarter in range(96):
                rows.append({
                    "start": (
                        when_day + timedelta(minutes=quarter * 15)
                    ).isoformat(),
                    "total_load_kwh": kwh,
                })

        model = build_base_load_model(rows, "UTC", minimum_samples=2)
        saturday = model["by_weekday"][5][0]["median_w"]
        sunday = model["by_weekday"][6][0]["median_w"]
        weekday_w = model["by_weekday"][0][0]["median_w"]

        self.assertGreater(saturday, weekday_w)
        self.assertLess(sunday, weekday_w)
        # The old weekday/weekend split averaged these two into one number.
        self.assertGreater(saturday, sunday * 2)

    def test_base_model_shrinks_to_the_shared_shape_without_evidence(
        self,
    ) -> None:
        """One odd Saturday must not become every future Saturday."""
        monday = datetime(2026, 6, 1, tzinfo=timezone.utc)
        rows = []
        for day in range(7):
            when_day = monday + timedelta(days=day)
            kwh = 2.0 if when_day.weekday() == 5 else 0.25
            for quarter in range(96):
                rows.append({
                    "start": (
                        when_day + timedelta(minutes=quarter * 15)
                    ).isoformat(),
                    "total_load_kwh": kwh,
                })

        model = build_base_load_model(rows, "UTC", minimum_samples=1)
        saturday = model["by_weekday"][5][0]

        # A single sample earns part of the departure it claims, not all of
        # it: 8 kW measured must not become an 8 kW standing expectation.
        self.assertGreater(saturday["median_w"], 1_000)
        self.assertLess(saturday["median_w"], 5_000)
        # And the thin evidence has to be visible in the published band.
        self.assertGreater(
            saturday["p90_w"] - saturday["p10_w"],
            saturday["median_w"] * 0.25,
        )

    def test_base_model_weights_recent_days_more_heavily(self) -> None:
        """A routine that changed three weeks ago must not still dominate."""
        start = datetime(2026, 6, 1, tzinfo=timezone.utc)
        rows = []
        for day in range(42):
            # The household halved its standing load partway through.
            kwh = 0.5 if day < 21 else 0.25
            for quarter in range(96):
                rows.append({
                    "start": (
                        start + timedelta(days=day, minutes=quarter * 15)
                    ).isoformat(),
                    "total_load_kwh": kwh,
                })

        model = build_base_load_model(rows, "UTC", minimum_samples=2)
        expected = model["by_weekday"][0][0]["median_w"]

        # An unweighted median of the whole window would sit at 2000 W.
        self.assertLess(expected, 1_400)


class ForecastTests(unittest.TestCase):
    def test_service_window_validation(self) -> None:
        captured = datetime(2026, 8, 10, 17, 47, tzinfo=timezone.utc)
        horizon = utc_slots(captured, 72)
        earliest = horizon[0]
        deadline = datetime(2026, 8, 10, 22, 0, tzinfo=timezone.utc)
        self.assertEqual(earliest, horizon[0])
        validate_service_windows([{
            "id": "boiler:2026-08-10",
            "earliest_start": earliest.isoformat(),
            "deadline": deadline.isoformat(),
        }], horizon)
        with self.assertRaisesRegex(OptimisationInputError, "outside horizon"):
            validate_service_windows([{
                "id": "pool:bad",
                "earliest_start": (horizon[0] - timedelta(minutes=15)).isoformat(),
                "deadline": horizon[4].isoformat(),
            }], horizon)

    def test_a_plan_is_rebuilt_once_per_quarter(self) -> None:
        """The snapshot is only built when a plan is due, so this is also how
        stale the stored snapshot is allowed to get. The server refuses to plan
        against one older than fifteen minutes."""
        issued = datetime(2026, 8, 10, 18, 0, 51, tzinfo=timezone.utc)
        plan = {"status": "ready", "issued_at": issued.isoformat()}

        # Same quarter as the issue: nothing to do yet.
        self.assertFalse(optimisation_plan_due(
            plan, issued + timedelta(minutes=10)
        ))
        # The next quarter has begun, so a fresh snapshot is due.
        self.assertTrue(optimisation_plan_due(
            plan, datetime(2026, 8, 10, 18, 15, 22, tzinfo=timezone.utc)
        ))
        self.assertTrue(optimisation_plan_due(
            plan, issued + timedelta(minutes=2), retry_after_error=True
        ))

    def test_extracts_timestamped_watts_and_reports_provenance(self) -> None:
        entity = {
            "entity_id": "sensor.pv_forecast",
            "last_updated": "2026-08-10T08:01:00+00:00",
            "attributes": {
                "watts": {
                    "2026-08-10T08:00:00+00:00": 100,
                    "2026-08-10T08:15:00+00:00": 200,
                }
            },
        }
        values, used, issued = extract_timestamped_forecast(
            [entity], attribute_names=("watts",), value_keys=("watts", "value")
        )

        self.assertEqual(list(values.values()), [100, 200])
        self.assertEqual(used, ["sensor.pv_forecast"])
        self.assertEqual(issued, datetime(2026, 8, 10, 8, 1, tzinfo=timezone.utc))

    def test_rejects_positional_forecast_without_timestamps(self) -> None:
        with self.assertRaisesRegex(OptimisationInputError, "timestamped"):
            extract_timestamped_forecast(
                [{
                    "entity_id": "sensor.price",
                    "last_updated": "2026-08-10T08:01:00+00:00",
                    "attributes": {"today": [0.1, 0.2]},
                }],
                attribute_names=("today",),
                value_keys=("price", "value"),
            )

    def test_multiple_pv_arrays_are_summed_without_double_counting_attributes(self) -> None:
        entities = [
            {
                "entity_id": f"sensor.roof_{index}",
                "last_updated": "2026-08-10T08:01:00+00:00",
                "attributes": {
                    "watts": {"2026-08-10T08:00:00+00:00": watts},
                    "forecast": [
                        {"start": "2026-08-10T08:00:00+00:00", "watts": watts}
                    ],
                },
            }
            for index, watts in enumerate((100, 250))
        ]

        values, used, _ = extract_timestamped_forecast(
            entities,
            attribute_names=("watts", "forecast"),
            value_keys=("watts", "value"),
            combine="sum",
        )

        self.assertEqual(list(values.values()), [350])
        self.assertEqual(len(used), 2)

    def test_calibration_is_compact_and_separate_by_lead_day(self) -> None:
        result = calibration_summary([
            {"lead_day": 0, "predicted_kwh": 1.0, "actual_kwh": 0.8},
            {"lead_day": 0, "predicted_kwh": 1.0, "actual_kwh": 0.8},
            {"lead_day": 1, "predicted_kwh": 1.0, "actual_kwh": 0.5},
        ], minimum_samples=2)

        self.assertEqual(result["correction_factor_by_lead_day"][:2], [0.8, 1.0])
        self.assertEqual(result["sample_count_by_lead_day"][:2], [2, 1])

    def test_a_replan_is_not_skipped_by_seconds_within_a_quarter(self) -> None:
        """The observed failure: a plan issued at :51 past, pushed at :22.

        Comparing exact instants made the decision a coin flip on the seconds,
        so the quarter was skipped and the house ran on an hour-old plan
        through a replan it was due. The push clock only ever asks on quarter
        boundaries, so the comparison is made on the same clock.
        """
        issued = datetime(2026, 8, 17, 13, 30, 51, tzinfo=timezone.utc)
        plan = {"status": "ready", "issued_at": issued.isoformat()}

        self.assertTrue(optimisation_plan_due(
            plan, datetime(2026, 8, 17, 13, 45, 22, tzinfo=timezone.utc)
        ))
        # And a push inside the issuing quarter is still too early.
        self.assertFalse(optimisation_plan_due(
            plan, datetime(2026, 8, 17, 13, 30, 22, tzinfo=timezone.utc)
        ))

    def test_stale_forecast_is_rejected(self) -> None:
        captured = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)
        with self.assertRaisesRegex(OptimisationInputError, "stale"):
            require_fresh_source(
                captured - timedelta(hours=13),
                captured,
                max_age=timedelta(hours=12),
                label="PV forecast",
            )

    def test_discrete_current_capability_uses_number_entity_bounds(self) -> None:
        control = discrete_current_control(5, 16, 1, 3, 230)

        self.assertEqual(control, {
            "type": "discrete_current",
            "min_current_a": 5,
            "max_current_a": 16,
            "current_step_a": 1,
            "phase_count": 3,
            "voltage_v": 230,
        })
        with self.assertRaisesRegex(OptimisationInputError, "not aligned"):
            discrete_current_control(5, 16.5, 1, 3, 230)

    def test_cached_plan_accepts_and_verifies_ev_current_steps(self) -> None:
        now = datetime(2026, 8, 10, 8, 0, tzinfo=timezone.utc)
        currents = [5] * 7 + [6, 6]
        slots = [
            {
                "start": (now + timedelta(minutes=15 * index)).isoformat(),
                "binding": True,
                "pool_w": 0,
                "boiler_expected_w": 0,
                "boiler_permitted": True,
                "device_loads_w": {},
                "ev_w": current * 3 * 230,
                "ev_target_current_a": current,
                "ev_min_current_a": 0,
                "ev_max_current_a": 16,
                "battery_soc": 0.5,
            }
            for index, current in enumerate(currents)
        ]
        service = {
            "id": "ev:departure",
            "device": "ev",
            "earliest_start": now.isoformat(),
            "deadline": (now + timedelta(hours=2, minutes=15)).isoformat(),
            "required_kwh": 8,
            "control": {
                "type": "discrete_current",
                "min_current_a": 5,
                "max_current_a": 16,
                "current_step_a": 1,
                "phase_count": 3,
                "voltage_v": 230,
            },
            "min_run_slots": 2,
        }
        plan = {
            "schema_version": 5,
            "mode": "live",
            "capabilities": {
                "pv": True,
                "battery": True,
                "pool": False,
                "boiler": False,
                "ev": True,
            },
            "slot_minutes": 15,
            "model_version": "battery-export-planner-v6",
            "status": "ready",
            "issued_at": now.isoformat(),
            "valid_until": (now + timedelta(hours=2, minutes=30)).isoformat(),
            "binding_until": (now + timedelta(hours=2, minutes=15)).isoformat(),
            "services": [service],
            "device_models": [],
            "plans": {
                key: {
                    "status": "ready",
                    "slots": [dict(slot) for slot in slots],
                    "service_slots": {"ev:departure": list(range(9))},
                    "service_currents_a": {"ev:departure": list(currents)},
                    "service_inhibited_slots": {},
                }
                for key in ("baseline", "priority", "cost")
            },
        }
        validate_plan_contract(plan, now)

        plan["mode"] = "demo"
        with self.assertRaisesRegex(OptimisationInputError, "mode"):
            validate_plan_contract(plan, now)
        plan["mode"] = "live"

        plan["plans"]["priority"]["slots"][0]["ev_max_current_a"] = 17
        with self.assertRaisesRegex(OptimisationInputError, "charger capability"):
            validate_plan_contract(plan, now)
        plan["plans"]["priority"]["slots"][0]["ev_max_current_a"] = 16

        plan["plans"]["priority"]["service_currents_a"]["ev:departure"][0] = 5.5
        with self.assertRaisesRegex(OptimisationInputError, "current step"):
            validate_plan_contract(plan, now)

    def test_cached_plan_requires_matching_contiguous_scenarios(self) -> None:
        now = datetime(2026, 8, 10, 8, 0, tzinfo=timezone.utc)
        slots = [
            {
                "start": (now + timedelta(minutes=15 * index)).isoformat(),
                "binding": True,
                "pool_w": 0,
                "boiler_expected_w": 0,
                "boiler_permitted": True,
                "device_loads_w": {},
                "ev_w": 0,
                "ev_target_current_a": 0,
                "ev_min_current_a": 0,
                "ev_max_current_a": 0,
                "battery_soc": 0.5,
            }
            for index in range(4)
        ]
        plan = {
            "schema_version": 5,
            "mode": "live",
            "capabilities": {
                "pv": True,
                "battery": True,
                "pool": False,
                "boiler": False,
                "ev": False,
            },
            "slot_minutes": 15,
            "model_version": "battery-export-planner-v6",
            "status": "ready",
            "issued_at": now.isoformat(),
            "valid_until": (now + timedelta(minutes=75)).isoformat(),
            "binding_until": (now + timedelta(hours=1)).isoformat(),
            "services": [],
            "device_models": [],
            "plans": {
                key: {
                    "status": "ready",
                    "slots": [dict(slot) for slot in slots],
                    "service_slots": {},
                    "service_currents_a": {},
                    "service_inhibited_slots": {},
                }
                for key in ("baseline", "priority", "cost")
            },
        }
        validate_plan_contract(plan, now)

        plan["plans"]["cost"]["slots"][2]["start"] = (
            now + timedelta(minutes=60)
        ).isoformat()
        with self.assertRaisesRegex(OptimisationInputError, "not contiguous"):
            validate_plan_contract(plan, now)

    def test_cached_plan_binding_flags_must_match_the_boundary(self) -> None:
        now = datetime(2026, 8, 10, 8, 0, tzinfo=timezone.utc)
        slots = [
            {
                "start": (now + timedelta(minutes=15 * index)).isoformat(),
                "binding": index < 2,
                "pool_w": 0,
                "boiler_expected_w": 0,
                "boiler_permitted": True,
                "device_loads_w": {},
                "ev_w": 0,
                "ev_target_current_a": 0,
                "ev_min_current_a": 0,
                "ev_max_current_a": 0,
                "battery_soc": 0.5,
            }
            for index in range(4)
        ]
        plan = {
            "schema_version": 5,
            "mode": "live",
            "capabilities": {
                "pv": True,
                "battery": True,
                "pool": False,
                "boiler": False,
                "ev": False,
            },
            "slot_minutes": 15,
            "model_version": "battery-export-planner-v6",
            "status": "ready",
            "issued_at": now.isoformat(),
            "valid_until": (now + timedelta(minutes=75)).isoformat(),
            "binding_until": (now + timedelta(minutes=30)).isoformat(),
            "services": [],
            "device_models": [],
            "plans": {
                key: {
                    "status": "ready",
                    "slots": [dict(slot) for slot in slots],
                    "service_slots": {},
                    "service_currents_a": {},
                    "service_inhibited_slots": {},
                }
                for key in ("baseline", "priority", "cost")
            },
        }
        validate_plan_contract(plan, now)

        plan["plans"]["priority"]["slots"][2]["binding"] = True
        with self.assertRaisesRegex(OptimisationInputError, "different binding"):
            validate_plan_contract(plan, now)

    def test_cached_plan_rejects_expected_boiler_draw_while_inhibited(self) -> None:
        now = datetime(2026, 8, 10, 8, 0, tzinfo=timezone.utc)
        slots = [
            {
                "start": (now + timedelta(minutes=15 * index)).isoformat(),
                "binding": True,
                "pool_w": 0,
                "boiler_expected_w": 500,
                "boiler_permitted": True,
                "device_loads_w": {},
                "ev_w": 0,
                "ev_target_current_a": 0,
                "ev_min_current_a": 0,
                "ev_max_current_a": 0,
                "battery_soc": 0.5,
            }
            for index in range(4)
        ]
        plan = {
            "schema_version": 5,
            "mode": "live",
            "capabilities": {
                "pv": True,
                "battery": True,
                "pool": False,
                "boiler": True,
                "ev": False,
            },
            "slot_minutes": 15,
            "model_version": "battery-export-planner-v6",
            "status": "ready",
            "issued_at": now.isoformat(),
            "valid_until": (now + timedelta(minutes=75)).isoformat(),
            "binding_until": (now + timedelta(hours=1)).isoformat(),
            "services": [{
                "id": "boiler:today",
                "device": "boiler",
                "required_kwh": 0.5,
                "earliest_start": now.isoformat(),
                "deadline": (now + timedelta(hours=1)).isoformat(),
                "control": {
                    "type": "duty_cycle",
                    "rated_power_w": 3000,
                    "expected_power_w_by_slot": [500, 500, 500, 500],
                    "max_consecutive_inhibit_slots": 2,
                },
            }],
            "device_models": [],
            "plans": {
                key: {
                    "status": "ready",
                    "slots": [dict(slot) for slot in slots],
                    "service_slots": {"boiler:today": [0, 1, 2, 3]},
                    "service_currents_a": {},
                    "service_inhibited_slots": {"boiler:today": []},
                }
                for key in ("baseline", "priority", "cost")
            },
        }
        validate_plan_contract(plan, now)

        plan["plans"]["priority"]["slots"][0]["boiler_permitted"] = False
        plan["plans"]["priority"]["service_inhibited_slots"]["boiler:today"] = [0]
        with self.assertRaisesRegex(OptimisationInputError, "draws power while inhibited"):
            validate_plan_contract(plan, now)


if __name__ == "__main__":
    unittest.main()


class ModelVersionToleranceTests(unittest.TestCase):
    """The server must be able to change planner without stopping control.

    `model_version` names the algorithm; `schema_version` names the contract.
    Only the second decides whether this build can read a plan, so the server
    must be free to rename or replace its planner without an allowlist here
    that someone has to remember to widen. Forgetting exactly that on
    2026-08-14 stopped every plan being cached for days.
    """

    def _plan(self, **overrides: Any) -> dict[str, Any]:
        issued = datetime(2026, 8, 16, 12, 0, tzinfo=timezone.utc)
        slot = {
            "start": issued.isoformat(),
            "binding": True,
            "pool_w": 0,
            "boiler_expected_w": 0,
            "boiler_permitted": True,
            "ev_w": 0,
            "ev_target_current_a": 0,
            "ev_min_current_a": 0,
            "ev_max_current_a": 0,
            "device_loads_w": {},
            "battery_soc": 0.5,
        }
        return {
            "schema_version": 5,
            "mode": "live",
            "capabilities": {
                "pv": True,
                "battery": False,
                "pool": False,
                "boiler": False,
                "ev": False,
            },
            "slot_minutes": 15,
            "model_version": "thermal-room-planner-v8",
            "status": "ready",
            "issued_at": issued.isoformat(),
            "valid_until": (issued + timedelta(minutes=75)).isoformat(),
            "binding_until": (issued + timedelta(hours=1)).isoformat(),
            "services": [],
            "device_models": [],
            "plans": {
                key: {
                    "status": "ready",
                    "slots": [
                        {
                            **slot,
                            "start": (
                                issued + timedelta(minutes=15 * index)
                            ).isoformat(),
                        }
                        for index in range(4)
                    ],
                    "service_slots": {},
                    "service_currents_a": {},
                    "service_inhibited_slots": {},
                }
                for key in ("baseline", "priority", "cost")
            },
            **overrides,
        }

    def test_both_readable_plan_schemas_are_accepted(self) -> None:
        """A rollout must never leave an installation unable to read a plan.

        The server offers schema 6 only to snapshots that declared 6, so both
        versions are live at once while installations update.
        """
        now = datetime(2026, 8, 16, 12, 5, tzinfo=timezone.utc)
        for version in (5, 6):
            with self.subTest(schema_version=version):
                validate_plan_contract(self._plan(schema_version=version), now)

    def test_any_planner_name_is_executable_on_a_known_contract(self) -> None:
        now = datetime(2026, 8, 16, 12, 5, tzinfo=timezone.utc)
        for version in (
            "battery-export-planner-v6",
            "shadow-price-planner-v7",
            "thermal-room-planner-v8",
            "marginal-value-planner-v9",
            "a-planner-nobody-has-written-yet",
        ):
            with self.subTest(version=version):
                validate_plan_contract(
                    self._plan(model_version=version), now
                )

    def test_a_dispatched_device_is_not_judged_as_a_block(self) -> None:
        """A schema 6 plan sizes stores by state, not by required_kwh.

        This is the check that was missing when schema 6 shipped: the server
        stopped emitting per-service blocks, the integration went on demanding
        them, and every plan was refused with "workload is invalid" while the
        portal reported the format unsupported.
        """
        now = datetime(2026, 8, 16, 12, 5, tzinfo=timezone.utc)
        issued = datetime(2026, 8, 16, 12, 0, tzinfo=timezone.utc)
        plan = self._plan(
            schema_version=6,
            model_version="marginal-value-planner-v9",
            services=[{
                "id": "pool:2026-08-16",
                "device": "pool",
                "earliest_start": issued.isoformat(),
                "deadline": (issued + timedelta(hours=1)).isoformat(),
                "required_kwh": 2.0,
                "control": {"type": "fixed_power", "power_w": 3_500},
                "min_run_slots": 4,
                "priority": 2,
            }],
        )
        for scenario in plan["plans"].values():
            scenario["dispatched_devices"] = ["battery", "pool"]
            scenario["service_slots"] = {"pool:2026-08-16": []}
            # Power the block model would call unexplained, and a state-based
            # plan calls Tuesday afternoon.
            for offset, slot in enumerate(scenario["slots"]):
                slot["pool_w"] = 3_500 if offset < 2 else 0

        validate_plan_contract(plan, now)

    def test_a_dispatched_scenario_still_declares_its_devices(self) -> None:
        now = datetime(2026, 8, 16, 12, 5, tzinfo=timezone.utc)
        plan = self._plan(schema_version=6)
        for scenario in plan["plans"].values():
            scenario["dispatched_devices"] = "pool"

        with self.assertRaises(OptimisationInputError):
            validate_plan_contract(plan, now)

    def test_an_unreadable_contract_is_still_refused(self) -> None:
        now = datetime(2026, 8, 16, 12, 5, tzinfo=timezone.utc)
        for field, value in (
            # 7 is beyond this build; 5 and 6 are both readable.
            ("schema_version", 7),
            ("slot_minutes", 60),
            ("mode", "shadow"),
        ):
            with self.subTest(field=field):
                with self.assertRaises(OptimisationInputError):
                    validate_plan_contract(self._plan(**{field: value}), now)


class ServiceMeteringTests(unittest.TestCase):
    """A service is sized from its own meters, not from its meter category."""

    daily = {
        "2026-08-13": {
            "sensor.pool_heater_energy": 8.0,
            "sensor.pool_pump_energy": 2.0,
            "sensor.pool_room_floor_heater_energy": 5.0,
        },
        "2026-08-14": {
            "sensor.pool_heater_energy": 6.0,
            "sensor.pool_pump_energy": 2.0,
            "sensor.pool_room_floor_heater_energy": 4.0,
        },
    }

    def test_a_room_heater_sharing_the_category_is_not_charged_to_the_service(
        self,
    ) -> None:
        totals = service_daily_energy(
            self.daily,
            ["sensor.pool_heater_energy", "sensor.pool_pump_energy"],
        )
        self.assertEqual(totals, {"2026-08-13": 10.0, "2026-08-14": 8.0})

    def test_a_day_missing_one_of_the_meters_is_dropped_not_undercounted(self) -> None:
        daily = {**self.daily, "2026-08-15": {"sensor.pool_heater_energy": 7.0}}
        totals = service_daily_energy(
            daily,
            ["sensor.pool_heater_energy", "sensor.pool_pump_energy"],
        )
        self.assertNotIn("2026-08-15", totals)

    def test_energy_done_today_counts_only_this_service_s_devices(self) -> None:
        rows = [
            {
                "start": "2026-08-15T06:00:00+00:00",
                "device_energy_kwh": {
                    "sensor.pool_heater_energy": 3.0,
                    "sensor.pool_room_floor_heater_energy": 1.5,
                },
            },
            {
                "start": "2026-08-14T06:00:00+00:00",
                "device_energy_kwh": {"sensor.pool_heater_energy": 9.0},
            },
        ]
        done = service_energy_today(
            rows,
            {"sensor.pool_heater_energy", "sensor.pool_pump_energy"},
            datetime(2026, 8, 15, tzinfo=timezone.utc).date(),
            timezone.utc,
        )
        self.assertEqual(done, 3.0)
