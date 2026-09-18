"""Plain-language details on every controller sensor and scheduled mode select."""
from copy import deepcopy
from types import SimpleNamespace
import unittest
from pathlib import Path
import sys
sys.path.append(str(Path(__file__).parents[1]/'custom_components'/'shs_energy'))

from presentation import battery_status_text, controller_explanation
from operating_modes import device_mode
from test_price_sensor_forecasts import method
from test_execution_mode_select import Rig


class ControllerDetailsTests(unittest.TestCase):
    def setUp(self):
        self.runtime = {'reason':'Testing the plan; battery settings are not being changed',
            'mode':'control_verification','measurements':{'response_matches_direction':False},
            'loss_evidence':{'discharge':{'model_source':'measured'}},
            'explanation':{'status':'Testing the plan; battery settings are not being changed',
                'now':'Your home is using 2.67 kW. Solar is providing 0.00 kW. The battery is neither charging nor supplying power.',
                'plan':'The plan is to use the battery to help power your home.',
                'difference':'The battery has 0.08 kWh more stored than the plan expected.',
                'next':'Continue with the current plan.','deadline_ms':None}}
        self.options={'device_modes':{'$battery':'control_verification','$pool':'controlling',
            '$ev':'control_verification','heater':'control_verification','relay':'controlling'}}
        self.slot={'pool_w':1000,'ev_target_current_a':0,'device_commands':{
            'heater':{'type':'setpoint','target_c':21},'relay':{'type':'permit_inhibit','permitted':False}}}
        self.status={'battery':{'state':'verified'},'pool':{'state':'scheduled','water_temperature_c':28.8},
            'ev':{'state':'verified'},'device:heater':{'state':'verified'},'device:relay':{'state':'commanded'}}
        self.coordinator=SimpleNamespace(controller=SimpleNamespace(status=self.status,options=lambda:self.options),
            binding_plan_for=lambda *args:({},self.slot),battery_runtime=SimpleNamespace(snapshot=lambda:deepcopy(self.runtime)))

    def attributes(self, device):
        getter=method('sensor.py','ShsControllerSensor','extra_state_attributes',
            device_mode=device_mode,controller_explanation=controller_explanation)
        return getter(SimpleNamespace(coordinator=self.coordinator,device=device))

    def test_battery_sensor_contains_card_text_and_does_not_claim_verification_response(self):
        attributes=self.attributes('battery');text=attributes['explanation'];live=battery_status_text(self.runtime)
        for line in [live['status'],live['now'],live['loss'],*list(self.runtime['explanation'].values())[1:-1]]:
            self.assertIn(line,text)
        self.assertNotIn('not yet responded',text)
        self.assertIn('0.08 kWh more',text)

    def test_battery_rejection_is_visible_even_before_controller_status_refresh(self):
        rejection={'at_ms':123,'contract_id':'rejected','generation':2,'reason':'changed target'}
        self.runtime.update(state='fault',reason='A new battery plan could not be accepted.',
            plan_status='rejected',plan_rejection=rejection,accepted_reference_id='previous')
        attributes=self.attributes('battery')
        self.assertEqual(attributes['plan_rejection'],rejection)
        self.assertEqual(attributes['plan_status'],'rejected')
        self.assertEqual(attributes['accepted_reference_id'],'previous')
        self.assertIn('Waiting for a corrected plan',attributes['explanation'])
        getter=method('sensor.py','ShsControllerSensor','native_value')
        self.assertEqual(getter(SimpleNamespace(coordinator=self.coordinator,device='battery')),'fault')
        self.runtime.update(plan_status='accepted',plan_rejection=None,state='verified')
        self.assertIsNone(self.attributes('battery')['plan_rejection'])
        self.assertEqual(getter(SimpleNamespace(coordinator=self.coordinator,device='battery')),'verified')

    def test_pool_ev_and_each_other_device_have_specific_plain_language(self):
        pool=self.attributes('pool')['explanation']
        self.assertIn('28.8 °C',pool);self.assertIn('requests pool heating',pool)
        ev=self.attributes('ev')['explanation']
        self.assertIn('Testing the plan',ev);self.assertIn('no car charging',ev)
        devices=self.attributes('devices')
        self.assertIn('21 °C',devices['devices']['heater']['explanation'])
        self.assertIn('requests a pause',devices['devices']['relay']['explanation'])
        self.assertIn('21 °C',devices['explanation'])
        self.assertEqual(devices['devices']['relay']['state'],'commanded')

    def test_missing_plan_and_measurements_remain_explicit_without_fabricated_values(self):
        self.coordinator.binding_plan_for=lambda *args:({},None)
        self.assertIn('Waiting for a current plan',self.attributes('ev')['explanation'])
        self.runtime.pop('explanation');self.runtime['measurements']=None
        self.runtime['loss_evidence']=None;self.runtime['loss_model']=None
        self.assertIn('Waiting for current household',self.attributes('battery')['explanation'])
        self.assertNotIn('2.67',self.attributes('battery')['explanation'])

    def test_fault_correction_and_pending_delivery_remain_visible(self):
        self.status['pool'].update(state='fault',reason='Water temperature is unavailable',next_step='Check the water temperature sensor.')
        text=self.attributes('pool')['explanation']
        self.assertIn('Water temperature is unavailable',text)
        self.assertIn('Check the water temperature sensor.',text)
        self.runtime.update(mode='controlling',reason='Applying the planned settings',pending_writes=1)
        text=self.attributes('battery')['explanation']
        self.assertIn('not yet responded as requested',text)
        self.assertIn('confirm its settings',text)


class SelectDetailsTests(unittest.IsolatedAsyncioTestCase):
    async def test_each_mode_select_exposes_its_own_controller_explanation(self):
        r=Rig();await r.manager.refresh()
        r.entry.runtime_data.controller.status={'battery':{'state':'verified'},'device:sensor.heater':{'state':'verified'}}
        r.entry.runtime_data.controller.options=lambda:r.entry.options
        r.entry.runtime_data.battery_runtime=SimpleNamespace(snapshot=lambda:{'reason':'Testing the plan','explanation':{'plan':'Store spare solar energy.'}})
        r.entry.runtime_data.binding_plan_for=lambda *args:({}, {'device_commands':{'sensor.heater':{'type':'switch_schedule','on_seconds':900}}})
        getter=r.adapter.ExecutionModeSelect.extra_state_attributes.fget
        getter.__globals__['controller_explanation']=controller_explanation
        self.assertIn('Store spare solar',r.manager.entities['$battery'].extra_state_attributes['explanation'])
        self.assertIn('requests this device to be on',r.manager.entities['sensor.heater'].extra_state_attributes['explanation'])

    async def test_rejection_is_machine_readable_on_mode_select(self):
        r=Rig();await r.manager.refresh()
        rejection={'at_ms':123,'contract_id':'rejected','generation':2,'reason':'changed target'}
        r.entry.runtime_data.controller.status={'battery':{'state':'verified'}}
        r.entry.runtime_data.controller.options=lambda:r.entry.options
        r.entry.runtime_data.battery_runtime=SimpleNamespace(snapshot=lambda:{'state':'fault',
            'plan_status':'rejected','plan_rejection':rejection,'accepted_reference_id':'previous'})
        r.entry.runtime_data.binding_plan_for=lambda *args:({},None)
        getter=r.adapter.ExecutionModeSelect.extra_state_attributes.fget
        getter.__globals__['controller_explanation']=controller_explanation
        attributes=r.manager.entities['$battery'].extra_state_attributes
        self.assertEqual(attributes['plan_rejection'],rejection)
        self.assertEqual(attributes['accepted_reference_id'],'previous')
        self.assertIn('Waiting for a corrected plan',attributes['explanation'])
