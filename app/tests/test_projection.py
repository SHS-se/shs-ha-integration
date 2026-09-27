import unittest
from shs_app.projection import display_plan

class ProjectionTests(unittest.TestCase):
    def test_all_scenario_facts_remain_and_app_diagnostics_are_not_mutated(self):
        slot = {'start':'time','battery_command':{'mode':'charge'},'decision':{'why':'large diagnostic tree'}}
        plan = {'plans':{key:{'slots':[slot]} for key in ('baseline','priority','cost')},
                'execution_plan':{'plans':{'priority':{'slots':[slot]}}},'battery_execution':{'contract':'preserved'}}
        result = display_plan(plan)
        self.assertIn('decision',plan['plans']['priority']['slots'][0])
        self.assertEqual(result['battery_execution'],plan['battery_execution'])
        for scenario in result['plans'].values():
            self.assertEqual(scenario['slots'],[{'start':'time','battery_command':{'mode':'charge'}}])
        self.assertNotIn('decision',result['execution_plan']['plans']['priority']['slots'][0])
