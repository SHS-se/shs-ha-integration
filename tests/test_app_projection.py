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

    def test_all_cross_section_targets_keep_exact_editor_identity(self):
        fields = [{"key": key, "scope": "configuration", "message": "Required"} for key in
                  ("battery_power_measurement_entity", "house_consumption_power_entity", "solar_production_power_entity", "grid_power_entity")]
        source = [{"fix": {"fields": fields}}]
        result = attention_links(source, "entry with spaces")
        self.assertEqual(len(result[0]["fix"]["fields"]), 4)
        for field in result[0]["fix"]["fields"]:
            query = parse_qs(urlparse(field["url"]).query)
            self.assertEqual(query["field"], [field["key"]])
            self.assertEqual(query["config_entry"], ["entry with spaces"])
        self.assertNotIn("url", source[0]["fix"]["fields"][0])
