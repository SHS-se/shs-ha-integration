import json
from pathlib import Path
import sys
import unittest
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "custom_components/shs_energy"))
from shs_core.app_projection import schedule, attention_links


class AppProjectionTests(unittest.TestCase):
    def test_forecast_scopes_remain_distinct_and_do_not_mutate_plan(self):
        plan = json.loads((Path(__file__).parent / "fixtures/schema-9-mixed-mode-plan.json").read_text())["plan"]
        before = json.dumps(plan)
        result = schedule(plan, {"state": "ready"})
        self.assertEqual(result["household"][0]["load_w"], plan["plans"]["priority"]["slots"][0]["load_w"])
        self.assertEqual(result["execution"][0]["load_w"], plan["execution_plan"]["plans"]["priority"]["slots"][0]["load_w"])
        result["household"][0]["device_commands"].clear()
        self.assertEqual(json.dumps(plan), before)
        self.assertIsNone(schedule(plan, {"state": "expired"}))
        self.assertIsNone(schedule(plan, {"state": "invalid"}))

    def test_missing_values_are_gaps_and_unbounded_predictions_preserved(self):
        plan = {"plans": {"priority": {"slots": [{"start": "time", "load_w": 9999999}]}}}
        result = schedule(plan, {"state": "ready"})
        self.assertIsNone(result["household"][0]["battery_soc"])
        self.assertEqual(result["household"][0]["load_w"], 9999999)

    def test_published_temperatures_keep_scope_gaps_and_owner_target(self):
        plan = {"pool": {"water_temperature_c": 25, "stop_temperature_c": 32},
                "resolved_value_stores": [{"key": "pool", "derivation": {"source": "comfort"}}],
                "plans": {"priority": {"slots": [{"pool_temperature_c": 29.4}, {},
                                                  {"pool_temperature_c": 31.2}]}},
                "execution_plan": {"plans": {"priority": {"slots": [{"pool_temperature_c": 28.1}]}}}}
        before = json.dumps(plan)
        result = schedule(plan, {"state": "ready"})
        self.assertEqual(result["temperatures"], [{"key": "pool", "name": "Pool", "target_c": 30}])
        self.assertEqual([slot["temperatures_c"]["pool"] for slot in result["household"]], [29.4, None, 31.2])
        self.assertEqual(result["execution"][0]["temperatures_c"], {"pool": 28.1})
        self.assertEqual(json.dumps(plan), before)
        plan["resolved_value_stores"] = []
        self.assertIsNone(schedule(plan, {"state": "ready"})["temperatures"][0]["target_c"])

    def test_no_temperature_reconstruction_from_initial_state_or_heating(self):
        plan = {"pool": {"water_temperature_c": 25, "stop_temperature_c": 32},
                "plans": {"priority": {"slots": [{"pool_w": 3000}, {}]}}}
        result = schedule(plan, {"state": "ready"})
        self.assertEqual([slot["temperatures_c"]["pool"] for slot in result["household"]], [None, None])
        del plan["pool"]
        self.assertEqual(schedule(plan, {"state": "ready"})["temperatures"], [])

    def test_all_cross_section_targets_keep_exact_editor_identity(self):
        fields = [{"key": key, "scope": "configuration", "message": "Required"} for key in
                  ("battery_power_measurement_entity", "house_consumption_power_entity", "solar_production_power_entity", "grid_power_entity")]
        source = [{"fix": {"fields": fields}}]
        result = attention_links(source, "entry with spaces")
        self.assertEqual(len(result[0]["fix"]["fields"]), 4)
        for field in result[0]["fix"]["fields"]:
            query = parse_qs(field["url"].split("?",1)[1])
            self.assertEqual(query["field"], [field["key"]])
            self.assertEqual(query["config_entry"], ["entry with spaces"])
        self.assertNotIn("url", source[0]["fix"]["fields"][0])
