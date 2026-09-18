"""Accounting traces for the replacement; no controller or service mocks."""
from dataclasses import replace
from pathlib import Path
import sys
import unittest

sys.path.append(str(Path(__file__).parents[1] / "custom_components" / "shs_energy"))
from plan_execution import (Account, Bounds, Disposition, ExecutionContract, MeterReceipt,
    Objective, Recovery, ReferenceInterval, StateObservation, admit_plan, balance,
    contract_wire, feedback, measured, objective_history, observe_state, read_contract,
    record_meter, request_replan)
from plan_execution import LiveState, assess_execution, explain_execution, capture_replan
from battery_conversion import Conversion, Curve

QUARTER = 900_000


def contract(*, generation=0, opening=5_000_000, increment=500_000, previous=None, dispositions=(), recovery=()):
    interval = ReferenceInterval(0, QUARTER, opening, opening + increment,
        "grid_charge", "stored_energy", True, False, 2200, 0, False,
        550_000, 0, 0, 250_000, 800_000, 0, 0, 0, 0)
    return ExecutionContract(f"plan-{generation}", f"plan-{generation}", generation,
        "controlling", "whole-house", "constant-reference-0.95", "stored_energy_mwh",
        10_000_000, 1_000_000, 10_000_000, 1_000_000, QUARTER, 0, previous, (interval,),
        (Objective("cheap-window", "stored_energy", 0, QUARTER, opening + increment,
                   "Store cheap energy for later"),), recovery, dispositions)


def meter(account, direction, at, total, epoch="meter-1"):
    return record_meter(account, event_id=f"{direction}:{at}:{total}:{epoch}", stream=direction, direction=direction,
        boundary="battery_dc", epoch=epoch, source_at_ms=at, total_mwh=total)


def opening_account(plan=None, energy=5_000_000):
    account = Account()
    for direction in ("charge", "discharge"):
        account = meter(account, direction, 0, 0)
    return admit_plan(account, plan or contract(), 0, StateObservation(0, energy, "SOC"))


class ContractTests(unittest.TestCase):
    def test_real_server_fixture_is_read_without_field_translation(self):
        import json
        fixture = json.loads((Path(__file__).parent / "fixtures/battery-plan-execution-v1.json").read_text())
        plan = read_contract(fixture["contract"])
        self.assertEqual(plan.intervals[0].end_ms - plan.intervals[0].start_ms, 7 * 60_000)
        account = admit_plan(Account(requested_generation=plan.generation), plan, plan.intervals[0].start_ms,
            StateObservation(plan.intervals[0].start_ms, plan.intervals[0].stored_start_mwh, "fixture-opening"))
        self.assertEqual(balance(account, account.opening.at_ms).flow_debt_low_mwh, 0)
        self.assertEqual(contract_wire(plan), fixture["contract"])

    def test_wire_round_trip(self):
        self.assertEqual(read_contract(contract_wire(contract())), contract())

    def test_complete_balance_and_rounding_are_required(self):
        row = contract().intervals[0]
        with self.assertRaisesRegex(ValueError, "conserve"):
            replace(row, import_mwh=row.import_mwh + 10_000)
        with self.assertRaisesRegex(ValueError, "conserve"):
            replace(row, import_mwh=row.import_mwh + 10_000, rounding_mwh=10_000)
        self.assertEqual(replace(row, import_mwh=row.import_mwh + 1, rounding_mwh=1).rounding_mwh, 1)

    def test_partial_interval_cumulative_reference_does_not_round_every_tick(self):
        row = replace(contract().intervals[0], start_ms=100, end_ms=103,
                      stored_start_mwh=0, stored_end_mwh=2)
        self.assertEqual([row.stored_at(t) for t in range(100, 104)], [0, 1, 1, 2])
        falling = replace(row, stored_start_mwh=2, stored_end_mwh=0)
        self.assertEqual(falling.stored_at(101), 1)

    def test_capability_permission_and_target_have_distinct_meanings(self):
        row = contract().intervals[0]
        with self.assertRaisesRegex(ValueError, "grid permission"):
            replace(row, grid_charge_allowed=False)
        with self.assertRaisesRegex(ValueError, "capacity"):
            replace(contract(), maximum_mwh=5_100_000)
        with self.assertRaisesRegex(ValueError, "directional"):
            replace(row, operation="hold")

    def test_recovery_cannot_escape_deadline_or_duplicate_headroom(self):
        r = Recovery("cheap-window", 0, QUARTER, 500_000, 2000, 0, "household", "nominal_first_then_earliest", "Same cheap window")
        replace(contract(), recovery=(r,))
        with self.assertRaisesRegex(ValueError, "overlapping"):
            replace(contract(), recovery=(r, replace(r, order=1)))
        with self.assertRaisesRegex(ValueError, "deadline"):
            replace(contract(), recovery=(replace(r, end_ms=QUARTER + 1),))


class MeterEvidenceTests(unittest.TestCase):
    def test_gross_directions_not_netted(self):
        account = opening_account()
        account = meter(account, "charge", QUARTER, 1_000_000)
        account = meter(account, "discharge", QUARTER, 1_000_000)
        result = balance(account, QUARTER)
        self.assertEqual(result.charge, Bounds(1_000_000, 1_000_000))
        self.assertEqual(result.discharge, result.charge)
        self.assertEqual(result.flow_debt_low_mwh, 500_000)

    def test_missing_tail_is_unknown_and_unchanged_counter_is_zero(self):
        account = opening_account()
        self.assertEqual(balance(account, QUARTER).charge, Bounds(0, None))
        account = meter(account, "charge", QUARTER, 0)
        self.assertEqual(balance(account, QUARTER).charge, Bounds(0, 0))

    def test_cross_boundary_energy_is_not_prorated(self):
        account = opening_account()
        account = meter(account, "charge", QUARTER * 2, 1_000_000)
        self.assertEqual(measured(account.meters, "charge", 0, QUARTER), Bounds(0, 1_000_000))
        self.assertEqual(measured(account.meters, "charge", QUARTER, QUARTER * 2), Bounds(0, 1_000_000))
        self.assertEqual(measured(account.meters, "charge", 0, QUARTER * 2), Bounds(1_000_000, 1_000_000))

    def test_later_receipt_can_correct_same_or_earlier_source_time(self):
        account = opening_account()
        account = meter(account, "charge", QUARTER, 1_000_000)
        account = meter(account, "charge", QUARTER // 2, 250_000)
        self.assertEqual(measured(account.meters, "charge", 0, QUARTER // 2), Bounds(250_000, 250_000))
        account = meter(account, "charge", QUARTER // 2, 300_000)
        self.assertEqual(measured(account.meters, "charge", 0, QUARTER // 2), Bounds(300_000, 300_000))
        self.assertEqual(measured(account.meters, "charge", 0, QUARTER), Bounds(1_000_000, 1_000_000))

    def test_reset_and_unexplained_decrease_remain_unknown(self):
        for epoch in ("meter-1", "explicit-reset"):
            rows = (MeterReceipt("a", "c", "charge", "battery_dc", "meter-1", 0, 1000, 1),
                    MeterReceipt("b", "c", "charge", "battery_dc", epoch, QUARTER, 500, 2))
            self.assertEqual(measured(rows, "charge", 0, QUARTER), Bounds(0, None))

    def test_exact_duplicate_and_physical_double_mapping(self):
        account = opening_account()
        self.assertEqual(meter(account, "charge", 0, 0), account)
        with self.assertRaisesRegex(ValueError, "counted twice"):
            record_meter(account, event_id="different", stream="other", direction="charge", boundary="battery_dc",
                         epoch="x", source_at_ms=0, total_mwh=0)


class AccountTests(unittest.TestCase):
    def test_nonzero_opening_debt_and_credit(self):
        for observed, delivered, expected in ((4_500_000, 1_000_000, 0), (5_500_000, 0, 0)):
            account = opening_account(energy=observed)
            account = meter(account, "charge", QUARTER, delivered)
            account = meter(account, "discharge", QUARTER, 0)
            self.assertEqual(balance(account, QUARTER).flow_debt_low_mwh, expected)

    def test_soc_residual_is_not_metered_recovery(self):
        account = opening_account()
        account = meter(account, "charge", QUARTER, 400_000)
        account = meter(account, "discharge", QUARTER, 0)
        account = observe_state(account, StateObservation(QUARTER, 5_450_000, "SOC"))
        result = balance(account, QUARTER)
        self.assertEqual((result.flow_debt_low_mwh, result.state_debt_mwh, result.state_residual_low_mwh), (100_000, 50_000, 50_000))

    def test_historical_state_residual_and_target_evidence_are_retained(self):
        account = opening_account()
        account = meter(account, "charge", QUARTER, 500_000)
        account = meter(account, "discharge", QUARTER, 0)
        account = observe_state(account, StateObservation(QUARTER, 5_300_000, "SOC"))
        account = observe_state(account, StateObservation(QUARTER + 1, 5_400_000, "SOC"))
        self.assertEqual(balance(account, QUARTER).state_residual_low_mwh, -200_000)
        row = objective_history(account, QUARTER + 1)[0]
        self.assertEqual((row["outcome"], row["shortfall_mwh"], row["flow_shortfall_mwh"]), ("missed", 200_000, 0))
        self.assertEqual(row["fulfilment_basis"], "observed_stored_energy")

    def test_reference_amendment_does_not_invent_delivery_or_double_debt(self):
        account = opening_account(energy=4_500_000)
        account = meter(account, "charge", QUARTER // 2, 0)
        account = meter(account, "discharge", QUARTER // 2, 0)
        old = balance(account, QUARTER // 2)
        account = request_replan(account)
        new = contract(generation=1, opening=4_250_000, previous="plan-0",
            dispositions=(Disposition("cheap-window", "retained", None, "Revised from measured state"),))
        new = replace(new, source_receipt=account.requests[-1].source_receipt)
        account = admit_plan(account, new, QUARTER // 2, StateObservation(QUARTER // 2, 4_500_000, "SOC"))
        result = balance(account, QUARTER // 2)
        self.assertEqual(result.flow_debt_low_mwh, 0)
        self.assertEqual(result.charge, old.charge)
        self.assertEqual(account.admissions[-1].reference_adjustment_mwh, -750_000)
        self.assertEqual(admit_plan(account, new, QUARTER // 2, account.observed), account)

    def test_superseded_request_cannot_restore_old_strategy(self):
        account = request_replan(request_replan(opening_account()))
        with self.assertRaisesRegex(ValueError, "superseded"):
            admit_plan(account, contract(generation=1, previous="plan-0"), 100, StateObservation(100, 5_000_000, "SOC"))

    def test_replan_payload_keeps_its_captured_prefix_when_new_evidence_arrives(self):
        account, payload = capture_replan(opening_account(), 0)
        prefix = payload["source_receipt"]
        account = meter(account, "charge", 100, 500)
        self.assertLess(prefix, account.receipt)
        new = replace(contract(generation=payload["generation"], previous=payload["previous_contract_id"]), source_receipt=prefix)
        admitted = admit_plan(account, new, 100, StateObservation(100, 5_000_500, "SOC"))
        self.assertEqual(admitted.contract.source_receipt, prefix)
        self.assertEqual(payload["source_receipt"], prefix)

    def test_deadline_and_reference_revision_are_immutable(self):
        account = opening_account()
        with self.assertRaisesRegex(ValueError, "immutable"):
            admit_plan(account, contract(increment=100_000), 0, account.observed)
        account = request_replan(account)
        new = contract(generation=1, previous="plan-0")
        new = replace(new, source_receipt=account.requests[-1].source_receipt)
        new = replace(new, objectives=(replace(new.objectives[0], deadline_ms=QUARTER-1),))
        with self.assertRaisesRegex(ValueError, "original deadline"):
            admit_plan(account, new, 0, account.observed)

    def test_missed_deadline_survives_disposition_and_late_evidence_corrects_once(self):
        account = opening_account()
        account = meter(account, "charge", QUARTER, 400_000)
        account = meter(account, "discharge", QUARTER, 0)
        self.assertEqual(objective_history(account, QUARTER)[0]["outcome"], "missed")
        account = meter(account, "charge", QUARTER, 500_000)
        self.assertEqual(objective_history(account, QUARTER)[0]["outcome"], "fulfilled")
        again = meter(account, "charge", QUARTER, 500_000)
        self.assertEqual(again, account)

    def test_old_event_replay_cannot_overwrite_later_correction(self):
        account = opening_account()
        account = meter(account, "charge", QUARTER, 500_000)
        account = meter(account, "charge", QUARTER, 400_000)
        replay = meter(account, "charge", QUARTER, 500_000)
        self.assertEqual(replay, account)
        self.assertEqual(measured(replay.meters, "charge", 0, QUARTER), Bounds(400_000, 400_000))

    def test_amended_target_is_versioned_without_a_false_miss(self):
        account = request_replan(opening_account())
        new = contract(generation=1, increment=300_000, previous="plan-0",
            dispositions=(Disposition("cheap-window", "retained", None, "Requirement reduced"),))
        new = replace(new, source_receipt=account.requests[-1].source_receipt)
        account = admit_plan(account, new, 100, StateObservation(100, 5_000_000, "SOC"))
        account = meter(account, "charge", QUARTER, 300_000)
        account = meter(account, "discharge", QUARTER, 0)
        row = objective_history(account, QUARTER)[0]
        self.assertEqual(row["outcome"], "fulfilled")
        self.assertEqual([v["objective"]["target_mwh"] for v in row["versions"]], [5_500_000, 5_300_000])

    def test_missing_disposition_retains_prior_outcome(self):
        account = request_replan(opening_account())
        new = contract(generation=1, previous="plan-0")
        new = replace(new, source_receipt=account.requests[-1].source_receipt)
        new = replace(new, objectives=(replace(new.objectives[0], id="replacement"),))
        account = admit_plan(account, new, 100, StateObservation(100, 5_000_000, "SOC"))
        rows = objective_history(account, 100)
        self.assertEqual({r["objective"]["id"] for r in rows}, {"cheap-window", "replacement"})
        self.assertEqual(rows[0]["responsibility"], "outstanding")

    def test_actual_miss_survives_replan_and_intervening_delivery_is_counted_once(self):
        account = opening_account()
        account = meter(account, "charge", QUARTER, 400_000)
        account = meter(account, "discharge", QUARTER, 0)
        account = request_replan(account)
        new = contract(generation=1, opening=5_400_000, previous="plan-0")
        new = replace(new, source_receipt=account.requests[-1].source_receipt)
        row = replace(new.intervals[0], start_ms=QUARTER, end_ms=QUARTER*2)
        objective = replace(new.objectives[0], id="next-window", start_ms=QUARTER, deadline_ms=QUARTER*2)
        new = replace(new, intervals=(row,), objectives=(objective,), valid_until_ms=QUARTER*2,
            dispositions=(Disposition("cheap-window", "incorporated", "next-window", "Replanned from actual state"),))
        # Delivery between snapshot and arrival is present before activation.
        account = meter(account, "charge", QUARTER + 100, 410_000)
        account = meter(account, "discharge", QUARTER + 100, 0)
        account = admit_plan(account, new, QUARTER + 100, StateObservation(QUARTER + 100, 5_410_000, "SOC"))
        history = objective_history(account, QUARTER + 100)[0]
        self.assertEqual((history["outcome"], history["responsibility"], history["shortfall_mwh"]), ("missed", "incorporated", 100_000))
        result = balance(account, QUARTER + 100)
        self.assertEqual(result.charge, Bounds(410_000, 410_000))
        self.assertEqual(result.flow_debt_low_mwh, row.stored_at(QUARTER + 100) - 5_410_000)
        # A delayed source correction fixes history and the current balance once.
        account = meter(account, "charge", QUARTER, 405_000)
        self.assertEqual(objective_history(account, QUARTER + 100)[0]["shortfall_mwh"], 95_000)
        self.assertEqual(balance(account, QUARTER + 100), result)

    def test_retired_future_objective_does_not_become_a_later_miss(self):
        account = request_replan(opening_account())
        new = contract(generation=1, previous="plan-0")
        new = replace(new, source_receipt=account.requests[-1].source_receipt)
        new = replace(new, objectives=(), dispositions=(Disposition("cheap-window", "retired", None, "Objective cancelled"),))
        account = admit_plan(account, new, 100, StateObservation(100, 5_000_000, "SOC"))
        self.assertEqual(objective_history(account, QUARTER)[0]["outcome"], "changed_before_deadline")

    def test_checkpoint_roundtrip_preserves_evidence_and_unknowns(self):
        from runtime_json import encode_value, decode_value
        import json
        account = opening_account()
        account = meter(account, "charge", QUARTER * 2, 1_000_000)
        restored = decode_value(json.loads(json.dumps(encode_value(account))), Account)
        self.assertEqual(restored, account)
        self.assertEqual(feedback(restored, QUARTER), feedback(account, QUARTER))


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        self.model = Conversion("measured-installation", Curve(.94, 35), Curve(.95, 0), Curve(.97, 140), 120)
        self.live = LiveState(0, 5_000_000, 1000, 0, 1000, 10000, 10000, 16000, 5000)

    def test_nominal_charge_survives_ordinary_forecast_error(self):
        account = opening_account()
        baseline = assess_execution(account, self.live, self.model)
        noisy = assess_execution(account, replace(self.live, house_w=7000, eligible_load_w=7000), self.model)
        self.assertEqual(baseline.operation, "grid_charge")
        self.assertEqual(baseline.charge_dc_w, int(.94 * 2200 - 35))
        self.assertEqual(noisy.charge_dc_w, baseline.charge_dc_w)
        self.assertIsNone(noisy.replan_reason)

    def test_real_limit_reduces_charge_and_counts_pending_load(self):
        account = opening_account()
        live = replace(self.live, import_limit_w=3000, pending_import_w=1500)
        result = assess_execution(account, live, self.model)
        self.assertLess(result.charge_dc_w, result.nominal_charge_dc_w)
        self.assertEqual(result.reason, "import_headroom_reduced")
        self.assertLessEqual(self.model.net_grid(result.charge_dc_w, 0, 0, 1000) + 1500, 3000)

    def test_grid_permission_is_never_inferred_from_shortfall(self):
        result = assess_execution(opening_account(), replace(self.live, grid_charge_allowed=False), self.model)
        self.assertEqual(result.charge_dc_w, 0)
        self.assertEqual(result.reason, "grid_source_restricted")

    def test_full_battery_and_credit_never_force_discharge(self):
        account = opening_account(energy=10_000_000)
        result = assess_execution(account, replace(self.live, stored_mwh=10_000_000), self.model)
        self.assertEqual((result.operation, result.charge_dc_w, result.discharge_dc_w), ("hold", 0, 0))
        self.assertEqual(result.reason, "battery_full")

    def test_achieved_target_stops_charging_without_emptying_credit(self):
        result = assess_execution(opening_account(), replace(self.live, stored_mwh=5_600_000), self.model)
        self.assertEqual((result.charge_dc_w, result.discharge_dc_w), (0, 0))
        self.assertEqual(result.reason, "stored_target_reached")

    def test_storage_operating_limits_do_not_reject_real_state(self):
        plan = replace(contract(), minimum_mwh=2_000_000, maximum_mwh=8_000_000,
                       export_reserve_mwh=2_000_000)
        low = opening_account(plan, energy=1_000_000)
        result = assess_execution(low, replace(self.live, stored_mwh=1_000_000), self.model)
        self.assertGreater(result.charge_dc_w, 0)
        high = opening_account(plan, energy=9_000_000)
        result = assess_execution(high, replace(self.live, stored_mwh=9_000_000), self.model)
        self.assertEqual(result.charge_dc_w, 0)
        self.assertEqual(result.reason, "battery_full")

    def test_spent_recovery_cap_is_not_advertised_again(self):
        r = Recovery("cheap-window", 0, QUARTER, 100_000, 4000, 0, "household", "nominal_first_then_earliest", "Cheap window")
        account = opening_account(replace(contract(), recovery=(r,)), energy=4_850_000)
        account = meter(account, "charge", QUARTER // 2, 350_000)
        account = meter(account, "discharge", QUARTER // 2, 0)
        result = assess_execution(account, replace(self.live, at_ms=QUARTER // 2, stored_mwh=5_200_000), self.model)
        self.assertEqual(result.recovery_remaining_mwh, 50_000)
        self.assertEqual(result.recovery_dc_w, 0)
        self.assertEqual(result.replan_reason, "recovery_cannot_meet_deadline")

    def recovery_account(self, correction=500_000):
        r = Recovery("cheap-window", 0, QUARTER, correction, 4000, 0, "household", "nominal_first_then_earliest", "Cheap window")
        account = opening_account(replace(contract(), recovery=(r,)), energy=4_500_000)
        account = meter(account, "charge", QUARTER // 2, 250_000)
        account = meter(account, "discharge", QUARTER // 2, 0)
        return account

    def test_measured_shortfall_uses_authorised_earliest_power(self):
        account = self.recovery_account()
        live = replace(self.live, at_ms=QUARTER // 2, stored_mwh=4_750_000)
        result = assess_execution(account, live, self.model)
        self.assertEqual(result.recovery_dc_w, 4000)
        self.assertEqual(result.recovery_state, "executing")
        self.assertLessEqual(result.valid_until_ms, live.at_ms + 500_000 * 3600 // 4000)

    def test_command_or_soc_alone_cannot_settle_or_fabricate_recovery(self):
        account = opening_account(energy=4_500_000)
        live = replace(self.live, at_ms=QUARTER // 2, stored_mwh=4_750_000)
        result = assess_execution(account, live, self.model)
        self.assertEqual(result.recovery_state, "insufficient_capacity")
        self.assertEqual(result.recovery_dc_w, 0)

    def test_insufficient_authority_requests_replan_before_deadline(self):
        live = replace(self.live, at_ms=QUARTER // 2, stored_mwh=4_750_000)
        result = assess_execution(self.recovery_account(100_000), live, self.model)
        self.assertEqual(result.recovery_state, "insufficient_capacity")
        self.assertEqual(result.replan_reason, "recovery_cannot_meet_deadline")
        self.assertEqual(result.operation, "grid_charge")

    def test_scope_following_is_not_forecast_capped(self):
        row = replace(contract().intervals[0], operation="supply_house", target_kind="demand_following",
                      charge_ac_mwh=0, import_mwh=0, discharge_ac_mwh=250_000,
                      charge_ac_limit_w=0, discharge_ac_limit_w=10000, follows_demand=True,
                      stored_end_mwh=4_750_000)
        plan = replace(contract(), intervals=(row,), objectives=())
        account = opening_account(plan)
        live = replace(self.live, house_w=4000, eligible_load_w=4000)
        result = assess_execution(account, live, self.model)
        self.assertGreater(result.discharge_dc_w, 4000)
        self.assertLessEqual(self.model.discharge.output(result.discharge_dc_w), 4000)
        scoped = assess_execution(account, replace(live, eligible_load_w=1000, pv_w=2000), self.model)
        self.assertLessEqual(self.model.discharge.output(scoped.discharge_dc_w), 500)

    def test_expired_plan_has_no_recovery_authority(self):
        result = assess_execution(opening_account(), replace(self.live, at_ms=QUARTER), self.model)
        self.assertEqual(result.operation, "hold")
        self.assertEqual(result.replan_reason, "plan_required")

    def test_soc_discrepancy_requests_replan_without_fabricating_meter_debt(self):
        account = opening_account()
        account = meter(account, "charge", QUARTER // 2, 250_000)
        account = meter(account, "discharge", QUARTER // 2, 0)
        live = replace(self.live, at_ms=QUARTER // 2, stored_mwh=4_000_000)
        result = assess_execution(account, live, self.model)
        self.assertEqual(balance(account, live.at_ms).flow_debt_low_mwh, 0)
        self.assertEqual(result.recovery_dc_w, 0)
        self.assertEqual(result.replan_reason, "observed_state_cannot_meet_target")

    def test_export_reserve_is_distinct_from_household_minimum(self):
        row = replace(contract().intervals[0], operation="export", target_kind="permission",
                      charge_ac_mwh=0, import_mwh=0, discharge_ac_mwh=300_000, export_mwh=50_000,
                      charge_ac_limit_w=0, discharge_ac_limit_w=2000, export_allowed=True,
                      stored_end_mwh=4_700_000)
        plan = replace(contract(), intervals=(row,), objectives=(), export_reserve_mwh=5_000_000)
        result = assess_execution(opening_account(plan), replace(self.live, export_allowed=True), self.model)
        self.assertEqual(result.discharge_dc_w, 0)
        self.assertEqual(result.reason, "battery_reserve")

    def test_card_copy_does_not_call_a_request_measured_delivery(self):
        account = self.recovery_account()
        live = replace(self.live, at_ms=QUARTER // 2, stored_mwh=4_750_000)
        result = assess_execution(account, live, self.model)
        card = explain_execution(account, live, result, measured_battery_dc_w=0)
        self.assertIn("neither charging", card["now"])
        self.assertIn("being requested", card["next"])
        self.assertIn("0.50 kWh less", card["difference"])
        self.assertNotIn("policy", " ".join(str(v) for v in card.values()))

    def test_verification_card_does_not_claim_control(self):
        account = opening_account(replace(contract(), mode="control_verification"))
        card = explain_execution(account, self.live, assess_execution(account, self.live, self.model))
        self.assertIn("settings are not being changed", card["status"])
        self.assertIn("Waiting for a battery power reading", card["now"])


if __name__ == "__main__":
    unittest.main()

class BasisReconciliationTests(unittest.TestCase):
    def test_capacity_calibration_is_explicit_state_adjustment_not_charge(self):
        account=opening_account()
        account=meter(account,'charge',1000,0)
        account=meter(account,'discharge',1000,0)
        account=request_replan(account)
        new=replace(contract(generation=1,previous=account.contract.id),capacity_mwh=12000000,maximum_mwh=12000000,
            source_receipt=account.requests[-1].source_receipt)
        amended=admit_plan(account,new,1000,StateObservation(1000,6000000,'capacity_recalibration',False))
        self.assertEqual(amended.meters,account.meters)
        self.assertEqual(len(amended.reconciliations),1)
        record=amended.reconciliations[0]
        self.assertEqual((record.old_capacity_mwh,record.new_capacity_mwh),(10000000,12000000))
        self.assertIn('not delivered charge',record.reason)
        self.assertEqual(balance(amended,1000).charge.low,0)
        self.assertEqual(balance(amended,0),balance(account,0))

    def test_live_reserve_increase_is_authoritative_without_changing_plan(self):
        plan=contract(increment=-250000)
        row=replace(plan.intervals[0],operation='supply_house',target_kind='demand_following',
            charge_ac_mwh=0,discharge_ac_mwh=250000,import_mwh=0,charge_ac_limit_w=0,discharge_ac_limit_w=1000,follows_demand=True)
        plan=replace(plan,intervals=(row,),objectives=(replace(plan.objectives[0],kind='demand_following'),))
        account=opening_account(plan,energy=5000000)
        live=LiveState(0,5000000,1000,0,1000,4000,4000,10000,10000,minimum_mwh=6000000)
        model=Conversion('lossless',Curve(1,0),Curve(1,0),Curve(1,0),0)
        decision=assess_execution(account,live,model)
        self.assertEqual(decision.discharge_dc_w,0)
        self.assertEqual(decision.replan_reason,'local_storage_reserve_changed')

    def test_planner_feedback_omits_repeated_history_but_acknowledges_its_revision(self):
        import json
        from plan_execution import Admission, planner_feedback
        base=contract()
        admissions=tuple(Admission(replace(base,id=f'c{i}',generation=i,
            previous_contract_id=f'c{i-1}' if i else None),0,0,i+1) for i in range(1000))
        account=Account(requested_generation=999,receipt=1000,admissions=admissions,
            opening=StateObservation(0,5000000,'soc'))
        full=feedback(account,0);compact=planner_feedback(account,0)
        self.assertGreater(len(json.dumps(full)),100000)
        self.assertLess(len(json.dumps(compact)),5000)
        self.assertNotIn('versions',compact['objectives'][0])
        self.assertEqual(compact['settled_history']['through_receipt'],1000)
