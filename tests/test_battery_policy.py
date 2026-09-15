"""The old exact-anchor reader is retained only for offline analytical scoring."""
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).parents[1] / "custom_components" / "shs_energy"))
from battery_policy import read_battery_policy, rank_policy

FIXTURES = Path(__file__).parent / "fixtures"


def load(name="fixed-tail"):
    return read_battery_policy((FIXTURES / f"battery-policy-{name}.json").read_bytes())


class BatteryPolicyTests(unittest.TestCase):
    def test_real_compiler_fixed_tail_reversal_selects_hold(self):
        policy = load()
        alternatives = {a.id: a for a in policy.summary.alternatives}
        self.assertEqual(alternatives["hold"].current[-1], .5)
        self.assertEqual(alternatives["discharge"].current[-1], 0)
        self.assertEqual(alternatives["discharge"].future_delta[-1], 2.5)
        self.assertEqual(rank_policy(policy)[0], "hold")

    def test_real_negative_price_fixture_score(self):
        policy = load("negative-price")
        self.assertEqual(rank_policy(policy)[0], "charge")
        selected = next(a for a in policy.summary.alternatives if a.id == "charge")
        self.assertAlmostEqual(selected.full[-1], .0413125)

    def test_closed_reader_rejects_cost_reference_ranking_and_scope_corruption(self):
        source = json.loads(load().source_json)
        for mutate in (
            lambda v: v.update(unknown=True),
            lambda v: v.update(reference_id="missing"),
            lambda v: v.update(ranking=list(reversed(v["ranking"]))),
            lambda v: v["alternatives"][0]["current"].update(total_sek=99),
            lambda v: v["alternatives"][1]["future_delta"].update(wear_sek=1, total_sek=3.5),
            lambda v: v["coverage"].update(current_interval_count=True),
            lambda v: v["coverage"].update(interpolation="linear"),
            lambda v: v["identity"].update(intent_revision="foreign"),
            lambda v: v["coverage"]["problem"]["plant"]["equipment"][0].update(kind="ev"),
            lambda v: v["coverage"]["problem"]["economics"].update(services=[{}]),
            lambda v: v["alternatives"][0]["candidate"]["actions"]["battery"][0].update(unknown=1),
            lambda v: v["alternatives"][1].update(id=v["alternatives"][0]["id"]),
            lambda v: v["coverage"].update(optimality="unproven_after_pruning"),
        ):
            value = json.loads(json.dumps(source)); mutate(value)
            with self.subTest(mutation=mutate), self.assertRaises(ValueError):
                read_battery_policy(json.dumps(value).encode())
        with self.assertRaises(ValueError):
            read_battery_policy(b'{"status":"compiled","status":"compiled"}')

    def test_current_operation_wins_only_inside_explicit_deadband(self):
        policy = load()
        self.assertEqual(rank_policy(policy, "discharge", 2.0)[0], "discharge")
        self.assertEqual(rank_policy(policy, "discharge", 1.99)[0], "hold")
        with self.assertRaises(ValueError):
            rank_policy(policy, deadband_sek=float("nan"))

    def test_impossible_current_responses_or_consistently_forged_current_costs_fail(self):
        for change in (
            {"charge_w": 500000, "discharge_w": 500000},
            {"solar_charge_w": 100},
            {"export_w": 100},
            {"charge_w": 100},  # Grid charging is forbidden in this fixture.
            {"discharge_w": 2000},
        ):
            value = json.loads(load().source_json)
            value["alternatives"][0]["candidate"]["actions"]["battery"][0].update(change)
            with self.assertRaises(ValueError):
                read_battery_policy(json.dumps(value).encode())
        value = json.loads(load().source_json)
        # Preserve all C/F algebra while inventing 0.1 SEK of extra current wear
        # for both alternatives. Independent current-cost evaluation must catch it.
        for alternative in value["alternatives"]:
            for component in ("current", "full"):
                alternative[component]["wear_sek"] += .1
                alternative[component]["total_sek"] += .1
        with self.assertRaises(ValueError):
            read_battery_policy(json.dumps(value).encode())

    def test_battery_identity_with_spaces_is_an_exact_dictionary_key(self):
        value = json.loads(load().source_json)
        value["coverage"]["problem"]["plant"]["equipment"][0]["id"] = "home battery"
        for alternative in value["alternatives"]:
            actions = alternative["candidate"]["actions"]
            actions["home battery"] = actions.pop("battery")
        self.assertEqual(read_battery_policy(json.dumps(value).encode()).summary.group_id, "home battery")


if __name__ == "__main__":
    unittest.main()
