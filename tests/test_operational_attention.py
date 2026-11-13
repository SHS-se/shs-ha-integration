"""Execution failures reach the global surface without invented age gates."""
import unittest
from shs_core.operational_attention import execution_attention, execution_issue, merge_attention, pipeline_issue


class OperationalAttentionTests(unittest.TestCase):
    def test_projection_only_displays_notices_issued_by_execution_owners(self):
        reports = {
            'pool': {'state':'pending', 'execution_blocked':True},
            'ev': {'state':'limited', 'reason':'Waiting for headroom'},
            'device:heater': {'state':'fault', 'reason':'A diagnostic status without a notice'},
        }
        battery = {'state':'pending', 'command_state':'physical_scope_blocked'}
        self.assertEqual(execution_attention(reports, battery, []), [])
        notice = execution_issue('device:heater', reports['device:heater'], verification=True)
        reports['device:heater']['attention'] = [notice]
        self.assertEqual(execution_attention(reports, battery, []), [])
        devices = [{'key':'heater', 'name':'Bathroom heater', 'permission':{'controller_id':'device:heater'}}]
        item, = execution_attention(reports, battery, devices, include_verification=True)
        self.assertEqual(item['device_key'], 'heater')
        self.assertEqual(item['title'], 'Bathroom heater: plan execution is blocked')

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
