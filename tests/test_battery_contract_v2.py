"""Planner-stated supply responses and demand partition (contract v2).

Capture, 17 October 2026: the plan discharged 864 W so that import stayed under
15 kW while the car charged at 16 A. HA followed all eligible demand (7 kW).
"""
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import sys
import unittest

sys.path.append(str(Path(__file__).parents[1] / "custom_components" / "shs_energy"))
from shs_core.battery_conversion import Conversion, Curve
from shs_core.battery_mode import promised_credit_w, read_cover_offer, supply_request_ac_w
from shs_core.plan_execution import (Account, LiveState, StateObservation, admit_plan, assess_execution,
    contract_wire, explain_execution, read_contract, record_meter, upgrade_persisted_contracts)
from shs_core.runtime_json import decode_value, encode_value
from shs_core.plan_execution import ExecutionContract

QUARTER = 900_000
POOL = ["sensor.pool_heater_energy", "sensor.pool_pump_energy"]
DEMAND = {
    "loads": [
        {"key": "sensor.car_charging_total_energy", "name": "Car charging total", "category": "ev_charging"},
        {"key": "sensor.pool_heater_energy", "name": "Pool heater", "category": "pool_heating"},
        {"key": "sensor.pool_pump_energy", "name": "Pool pump", "category": "pool_heating"},
        {"key": "sensor.water_boiler_energy", "name": "Water boiler", "category": "hot_water"},
    ],
    "profiles": [{"id": "base+pool", "include_base": True, "eligible_keys": POOL}],
}


def supply(response, *, discharge_w=864, load_w=16112):
    discharge, load = discharge_w * QUARTER // 3_600_000, load_w * QUARTER // 3_600_000
    return {"start_ms": 0, "end_ms": QUARTER, "stored_start_mwh": 9_000_000,
            "stored_end_mwh": 9_000_000 - round(discharge / 0.95), "target_kind": "demand_following",
            "instruction": {"kind": "supply", "profile_id": "base+pool", "charge_ac_limit_w": 8800,
                            "discharge_ac_limit_w": 9600, "response": response},
            "charge_ac_mwh": 0, "discharge_ac_mwh": discharge, "pv_mwh": 0, "load_mwh": load,
            "import_mwh": load - discharge, "export_mwh": 0, "curtailed_mwh": 0, "unserved_mwh": 0,
            "rounding_mwh": 0}


def wire(response, **kwargs):
    return {"schema": "battery-plan-execution-v2", "id": "plan:battery:1", "plan_id": "plan", "generation": 0,
            "mode": "controlling", "scope_revision": "whole-house", "model_revision": "rules",
            "energy_basis": "stored_energy_mwh", "capacity_mwh": 18_080_000, "minimum_mwh": 904_000,
            "maximum_mwh": 18_080_000, "export_reserve_mwh": 904_000, "valid_until_ms": QUARTER,
            "source_receipt": 0, "previous_contract_id": None, "intervals": [supply(response, **kwargs)],
            "objectives": [{"id": "follow", "kind": "demand_following", "start_ms": 0, "deadline_ms": QUARTER,
                            "target_mwh": 8_772_632, "reason": "Supply the house"}],
            "recovery": [], "dispositions": [], "demand": deepcopy(DEMAND)}


def account(contract):
    value = Account()
    for direction in ("charge", "discharge"):
        value = record_meter(value, event_id=f"{direction}:0", stream=direction, direction=direction,
                             boundary="battery_dc", epoch="m", source_at_ms=0, total_mwh=0)
    return admit_plan(value, contract, 0, StateObservation(0, 9_000_000, "SOC"))


FIXED = {"mode": "fixed", "discharge_ac_w": 864, "import_target_w": 15000}
TARGET = {"mode": "import_target", "import_target_w": 15000}
FOLLOW = {"mode": "follow_demand"}


class SupplyLawTests(unittest.TestCase):
    def law(self, mode, house_w, *, fixed=864, target=15000, bound=5527):
        return supply_request_ac_w(mode, eligible_bound_w=bound, discharge_ac_w=fixed, import_target_w=target,
                                   house_w=house_w, pv_w=0, pending_import_w=0)

    def test_each_stated_response_on_the_capture(self):
        # Gross demand ran 16.5 kW against the plan's 15.6 kW.
        self.assertEqual(self.law("fixed", 16527), 1527)
        self.assertEqual(self.law("fixed", 16527, target=None), 864)
        self.assertEqual(self.law("import_target", 16527), 1527)
        self.assertEqual(self.law("follow_demand", 16527), 5527)
        # Demand as planned: the fixed figure stands, the target needs less.
        self.assertEqual(self.law("fixed", 15600), 864)
        self.assertEqual(self.law("import_target", 15600), 600)
        self.assertEqual(self.law("import_target", 14000), 0)

    def test_no_response_supplies_demand_outside_the_profile(self):
        self.assertEqual(self.law("import_target", 30000, bound=2000), 2000)
        self.assertEqual(self.law("fixed", 30000, bound=500), 500)

    def test_promised_credit_has_no_solar_credit_and_respects_every_bound(self):
        credit = lambda after, bound=5527, ceiling=9600: promised_credit_w(
            import_after_w=after, import_target_w=15000, eligible_bound_w=bound, discharge_ceiling_ac_w=ceiling)
        self.assertEqual(credit(16112), 1112)
        self.assertEqual(credit(16112, bound=800), 800)
        self.assertEqual(credit(16112, ceiling=0), 0)
        self.assertEqual(credit(14000), 0)

    def test_cover_offer_is_strict(self):
        offer = {"import_target_w": 15000, "discharge_ac_max_w": 9600, "include_base": True,
                 "meters": ["sensor.car_power"], "valid_until_ms": QUARTER}
        self.assertEqual(read_cover_offer(offer)["meters"], ("sensor.car_power",))
        self.assertIsNone(read_cover_offer(None))
        for bad in ({**offer, "extra": 1}, {**offer, "meters": ["switch.car"]}, {**offer, "import_target_w": 1.5}):
            with self.assertRaises(ValueError):
                read_cover_offer(bad)


class ContractV2Tests(unittest.TestCase):
    def setUp(self):
        self.model = Conversion("measured", Curve(.94, 35), Curve(.95, 0), Curve(.97, 140), 120)
        self.live = LiveState(60_000, 9_000_000, 16527, 0, 5527, 8800, 9600, 15000, 13200)

    def test_reader_keeps_the_stated_response_and_round_trips(self):
        contract = read_contract(wire(FIXED))
        row = contract.intervals[0]
        self.assertEqual((row.operation, row.supply_mode, row.discharge_ac_w, row.import_target_w, row.profile_id),
                         ("supply_house", "fixed", 864, 15000, "base+pool"))
        self.assertEqual(contract.supply_scope(row)[0].planned_device_keys, tuple(POOL))
        self.assertEqual(contract.supply_scope(row)[1], tuple(load["key"] for load in DEMAND["loads"]))
        self.assertEqual(contract_wire(contract), wire(FIXED))

    def test_reader_refuses_unstated_or_inconsistent_partitions(self):
        for change in (lambda w: w["intervals"][0]["instruction"].update(profile_id="unknown"),
                       lambda w: w["demand"]["profiles"][0].update(eligible_keys=["sensor.unlisted"]),
                       lambda w: w["intervals"][0]["instruction"]["response"].update(mode="automatic"),
                       lambda w: w["intervals"][0]["instruction"]["response"].pop("discharge_ac_w"),
                       lambda w: w.pop("demand")):
            value = wire(FIXED)
            change(value)
            with self.assertRaises(ValueError):
                read_contract(value)

    def test_assessment_executes_the_stated_response_against_live_demand(self):
        def discharge(response, live=self.live):
            result = assess_execution(account(read_contract(wire(response))), live, self.model)
            return result, self.model.discharge.output(result.discharge_dc_w)
        result, ac = discharge(FIXED)
        self.assertAlmostEqual(ac, 1527, delta=1)
        self.assertEqual(result.reason, "import_limit_support")
        self.assertAlmostEqual(discharge(TARGET)[1], 1527, delta=1)
        self.assertAlmostEqual(discharge(FOLLOW)[1], 5527, delta=1)
        result, ac = discharge(FIXED, replace(self.live, house_w=15600))
        self.assertAlmostEqual(ac, 864, delta=1)
        self.assertEqual(result.reason, "following_plan")

    def test_v1_rows_keep_their_meaning(self):
        fixture = json.loads((Path(__file__).parent / "fixtures/battery-plan-execution-v1.json").read_text())
        contract = read_contract(fixture["contract"])
        self.assertIsNone(contract.demand)
        for raw, row in zip(fixture["contract"]["intervals"], contract.intervals):
            expected = (("follow_demand" if raw["follows_demand"] else "fixed")
                        if raw["operation"] == "supply_house" else "none")
            self.assertEqual(row.supply_mode, expected)
            self.assertIsNone(row.import_target_w)
        self.assertEqual(contract_wire(contract), fixture["contract"])

    def test_persisted_v1_rows_upgrade_with_their_meaning(self):
        current = read_contract(wire(FOLLOW))
        old = encode_value(replace(current, demand=None, intervals=(replace(current.intervals[0], profile_id=None),)))
        row = old["intervals"][0]
        for key in ("supply_mode", "discharge_ac_w", "import_target_w", "profile_id"):
            row.pop(key)
        row["follows_demand"] = True
        old.pop("demand")
        upgraded = decode_value(upgrade_persisted_contracts(old), ExecutionContract)
        self.assertEqual(upgraded.intervals[0].supply_mode, "follow_demand")
        self.assertIsNone(upgraded.demand)
        self.assertEqual(upgrade_persisted_contracts(encode_value(upgraded)), encode_value(upgraded))

    def test_explanation_states_the_planned_response(self):
        for response, text in ((TARGET, "cover demand above 15.00 kW of grid import"),
                               (FIXED, "supply 0.86 kW from the battery, more if needed to keep grid import within 15.00 kW")):
            ledger = account(read_contract(wire(response)))
            assessment = assess_execution(ledger, self.live, self.model)
            self.assertIn(text, explain_execution(ledger, self.live, assessment, mode="controlling")["plan"])


if __name__ == "__main__":
    unittest.main()


class RuntimeAdmissionTests(unittest.TestCase):
    """The runtime's own house-limit check credits the stated promise."""

    def harness(self):
        from test_home_runtime_execution import Harness
        from shs_core.battery_supply import ExecutionDemand
        from shs_core.plan_execution import Objective, ReferenceInterval
        h = Harness()
        old = h.contract.intervals[0]
        discharge, load = 864 * QUARTER // 3_600_000, 16112 * QUARTER // 3_600_000
        row = ReferenceInterval(old.start_ms, old.end_ms, 5_000_000, 5_000_000 - round(discharge / 0.95),
            'supply_house', 'demand_following', False, False, 4000, 4000, 'fixed', 864, 15000, 'base+pool',
            0, discharge, 0, load, load - discharge, 0, 0, 0, 0)
        contract = replace(h.contract, intervals=(row,), demand=ExecutionDemand.read(DEMAND),
            objectives=(Objective('follow', 'demand_following', row.start_ms, row.end_ms, row.stored_end_mwh, 'supply'),))
        h.conditions(load_w=16112, eligible_load_w=5000)
        h.offer(contract)
        self.assertEqual(h.state.execution.status, 'active')
        return h

    def credit(self, h, import_after_w=16500):
        from shs_core.home_runtime import _discharge_credit
        return _discharge_credit(h.state, h.now, h.group, None, import_after_w)

    def observe(self, h, mode, discharge_w, measured_w=0):
        from shs_core.home_runtime import DISCHARGE_MEASUREMENT, Observation, Observed, Envelope
        controls = (('mode', mode), ('charge', 4000), ('discharge', discharge_w))
        h.event(Observed(h.group.spec.id, Observation(h.group.observation_revision + 1, h.now, h.now + 1800000,
            controls, (('ready', 1), (DISCHARGE_MEASUREMENT, measured_w)), Envelope(0, 0))))

    def test_promise_covers_import_above_the_target_before_discharge_is_measured(self):
        h = self.harness()
        self.observe(h, 'Maximum Self Consumption', 4000)
        self.assertEqual(self.credit(h), 1500)
        self.assertEqual(self.credit(h, 14000), 0)

    def test_promise_never_exceeds_the_lowest_setting_or_survives_a_stop(self):
        h = self.harness()
        self.observe(h, 'Maximum Self Consumption', 1000)
        self.assertEqual(self.credit(h), 1000)
        self.observe(h, 'Standby', 4000)
        self.assertEqual(self.credit(h), 0)

    def test_no_promise_at_the_storage_reserve(self):
        h = self.harness()
        self.observe(h, 'Maximum Self Consumption', 4000)
        h.conditions(load_w=16112, eligible_load_w=5000, energy=h.authority.plant.cutoff_kwh)
        self.assertEqual(self.credit(h), 0)


class PlannerFixtureTests(unittest.TestCase):
    """The real planner's schema-10 output (copied from its consumer fixtures)."""

    def test_schema_10_plan_is_admitted_and_its_contract_states_every_quarter(self):
        from datetime import datetime
        from shs_core.optimisation import validate_plan_contract
        fixture = json.loads((Path(__file__).parent / "fixtures/schema-10-mixed-mode-plan.json").read_text())
        plan = fixture["plan"]
        validate_plan_contract(plan, datetime.fromisoformat(fixture["validation_time"]))
        contract = read_contract(plan["battery_execution"])
        self.assertEqual(contract.demand.keys, tuple(sorted(m["key"] for m in plan["device_models"])))
        for slot, row in zip(plan["plans"]["priority"]["slots"], contract.intervals):
            instruction = slot["battery_instruction"]
            self.assertEqual(row.supply_mode, instruction.get("response", {}).get("mode", "none"))
        self.assertEqual(contract_wire(contract), plan["battery_execution"])
