"""Execution failures reach the global surface without invented age gates."""
import unittest
from shs_core.operational_attention import execution_attention, merge_attention, pipeline_issue


class OperationalAttentionTests(unittest.TestCase):
    def test_blocked_pending_command_is_reported_but_normal_pending_and_off_are_quiet(self):
        reports={'pool':{'state':'pending','reason':'Waiting for battery charging to release grid capacity','execution_blocked':True},
                 'ev':{'state':'stopped','reason':'Plan requests charging off'}}
        battery={'state':'pending','reason':'Applying the planned battery settings',
                 'measurements':{'source_report_ages_ms':{'sensor.soc':999999999}}}
        items=execution_attention(reports,battery,[])
        self.assertEqual([item['key'] for item in items],['controller:pool'])
        self.assertIn('release grid capacity',items[0]['detail'])
        reports['pool']={'state':'scheduled','reason':'Heating is permitted'}
        self.assertEqual(execution_attention(reports,battery,[]),[])
        battery.update(command_state='physical_scope_blocked')
        self.assertIn('grid capacity',execution_attention(reports,battery,[])[0]['detail'])

    def test_only_controlling_or_still_held_devices_raise_execution_alarms(self):
        failures=({'state':'fault','reason':'x'},{'state':'limited','reason':'x'},{'state':'idle','reason':'x','execution_blocked':True},
                  {'state':'verified','reason':'x','plan_rejection':{'reason':'x'}},{'state':'pending','reason':'x','command_state':'attempt_limit'})
        for failure in failures:
            for mode in ('monitoring','control_verification'):
                with self.subTest(failure=failure,mode=mode):
                    self.assertEqual(execution_attention({'device:a':{**failure,'mode':mode,'holding':False}},{},[]),[])
                    self.assertEqual(execution_attention({},{**failure,'mode':mode,'holding':False},[]),[])
                    # A failed handover leaves SHS holding the device: that is still an alarm.
                    self.assertTrue(execution_attention({'device:a':{**failure,'mode':mode,'holding':True}},{},[]))
                    # Unknown ownership never hides a failure.
                    self.assertTrue(execution_attention({'device:a':{**failure,'mode':mode}},{},[]))
            with self.subTest(failure=failure,mode='controlling'):
                self.assertTrue(execution_attention({'device:a':{**failure,'mode':'controlling','holding':False}},{},[]))

    def test_a_rejected_battery_update_remains_visible_with_an_accepted_reference(self):
        battery={'state':'controlling','accepted_plan_id':'previous',
                 'plan_rejection':{'reason':'Execution contract did not match the request'}}
        items=execution_attention({},battery,[])
        self.assertEqual(items[0]['detail'],'Execution contract did not match the request')
        self.assertEqual(items[0]['severity'],'error')

    def test_configuration_corrections_suppress_only_their_exact_runtime_duplicate(self):
        fields=[{'key':'house_consumption_power_entity','scope':'configuration'}]
        setup={'key':'battery_control','severity':'warning','fix':{'kind':'fields','fields':fields}}
        duplicate={'key':'controller:battery','severity':'error','fix':{'kind':'fields','fields':fields}}
        unrelated=pipeline_issue('upload','Server returned HTTP 502')
        self.assertEqual({i['key'] for i in merge_attention([setup,duplicate,unrelated])},
                         {'battery_control','pipeline:upload'})
        self.assertEqual(next(i for i in merge_attention([setup,duplicate]) if i['key']=='battery_control')['severity'],'error')
        self.assertEqual(setup['severity'],'warning')
        duplicate['fix']['fields']=[{'key':'battery_power_measurement_entity','scope':'configuration'}]
        self.assertEqual(len(merge_attention([setup,duplicate,unrelated])),3)
