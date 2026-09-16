from dataclasses import replace
from pathlib import Path
import sys
import unittest
sys.path.insert(0, str(Path(__file__).parents[1] / 'custom_components/shs_energy'))
from battery_supply import SupplyScope, PowerReading, measured_supply, proportional_supply, observe_supply
from operating_modes import reconcile_admissions, planning_devices
from configuration_schema import prepare_options


class BatterySupplyTests(unittest.TestCase):
    def setUp(self):
        self.house = PowerReading(3000, 'house', 1000, 5000, 'house-ac')
        self.pv = PowerReading(1000, 'pv', 1000, 5000, 'house-ac')
        self.devices = {'ev': PowerReading(1200, 'ev', 1000, 5000, 'house-ac'),
                        'pool': PowerReading(800, 'pool', 1000, 5000, 'house-ac')}

    def measure(self, scope, **changes):
        args = dict(scope=scope, house=self.house, pv=self.pv, planned_readings=self.devices,
                    planned_keys=('ev','pool'), now_ms=2000, max_alignment_ms=100,
                    membership_revision='roles-1', expected_membership_revision='roles-1')
        args.update(changes)
        return measured_supply(**args)

    def test_five_scopes_share_solar_once(self):
        for scope, expected in [(SupplyScope('none'),0), (SupplyScope('whole_house'),2000),
                (SupplyScope('selected',True),2000/3), (SupplyScope('selected',False,('ev',)),800),
                (SupplyScope('selected',True,('ev',)),4400/3)]:
            with self.subTest(scope=scope):
                self.assertAlmostEqual(self.measure(scope).house_supply_bound_w, expected)
        components = [proportional_supply(3000,1000,w) for w in (1000,1200,800)]
        self.assertAlmostEqual(sum(c.attributed_pv_w for c in components),1000)
        self.assertAlmostEqual(sum(c.house_supply_bound_w for c in components),2000)

    def test_surplus_and_zero_house_are_defined_physical_cases(self):
        self.assertEqual(proportional_supply(3000,4000,1000).house_supply_bound_w,0)
        self.assertEqual(proportional_supply(0,1000,0).house_supply_bound_w,0)

    def test_decimal_surplus_cannot_create_negative_supply(self):
        for house in (1000.067, 1100.123456, 1279.009, 3340.99):
            for eligible in (house, house / 3):
                for pv in (house, house + 500):
                    self.assertEqual(proportional_supply(house, pv, eligible).house_supply_bound_w, 0)

    def test_unavailable_inputs_never_substitute_forecasts_or_ratings(self):
        scope=SupplyScope('selected',True)
        for changes in [dict(house=None),dict(pv=None),dict(planned_readings={}),dict(now_ms=5000),
                dict(pv=replace(self.pv,at_ms=500)),dict(pv=replace(self.pv,boundary='dc')),
                dict(house=replace(self.house,basis='interval_average_ac')),
                dict(membership_revision='roles-2'),dict(house=replace(self.house,watts=100)),
                dict(planned_readings={**self.devices,'ev':replace(self.devices['ev'],source='house')})]:
            with self.subTest(changes=changes), self.assertRaises(ValueError):self.measure(scope,**changes)
        with self.assertRaises(ValueError): self.measure(SupplyScope('selected',False,('excluded',)))
        # Whole-house permission does not require individual decomposition.
        self.assertEqual(self.measure(SupplyScope('whole_house'),planned_readings={}).house_supply_bound_w,2000)

    def test_live_sensor_bindings_require_instant_power_and_acknowledged_roles(self):
        options = {'house_consumption_power_entity': 'sensor.house', 'solar_production_power_entity': 'sensor.pv',
                   'device_control_mappings': {'ev': {'power': 'sensor.ev'}}}
        values = {key: {'state': str(w), 'attributes': {'unit_of_measurement': 'kW', 'state_class': 'measurement'},
                       'last_reported': '1970-01-01T00:00:01+00:00'}
                  for key, w in [('sensor.house', 3), ('sensor.pv', 1), ('sensor.ev', 1.2)]}
        kwargs = dict(scope=SupplyScope('selected', False, ('ev',)), options=options,
                      devices=[{'key': 'ev', 'planning_role': 'controllable'}], read_entity=values.get,
                      at_ms=2000, max_age_ms=4000, max_alignment_ms=100, boundary='house-ac',
                      membership_revision='roles-1', expected_membership_revision='roles-1')
        result, evidence = observe_supply(**kwargs)
        self.assertAlmostEqual(result.house_supply_bound_w, 800)
        self.assertEqual({r.source for r in evidence}, set(values))
        options['device_control_mappings']['ev']['power'] = 1200
        with self.assertRaisesRegex(ValueError, 'sensor is not configured'):
            observe_supply(**kwargs)

    def test_invalid_partition_and_scope_keys_fail_closed(self):
        for values in [(1,0,2),(-1,0,0),(1,float('nan'),1)]:
            with self.assertRaises(ValueError): proportional_supply(*values)
        for scope in [dict(kind='all'),dict(kind='whole_house',include_base=True),
                dict(kind='selected',include_base=False,planned_device_keys=['ev','ev'])]:
            with self.assertRaises(ValueError):SupplyScope.read(scope)
        self.assertEqual(SupplyScope.read(dict(kind='selected',include_base=False,planned_device_keys=[])),SupplyScope('none'))


class ParticipationTests(unittest.TestCase):
    def setUp(self):
        self.device=dict(key='heater',category='heating',control_type='switch_schedule',
                         planning_role='controllable',planning_choice_at='role-1')

    def test_admission_defaults_verification_and_does_not_revive_control(self):
        options=reconcile_admissions({},[self.device],{})
        self.assertEqual(options['device_modes'],{'heater':'control_verification'})
        options['device_modes']['heater']='controlling'
        self.assertEqual(reconcile_admissions(options,[self.device],{}),options)
        demoted=reconcile_admissions(options,[{**self.device,'planning_role':'base_load'}],{})
        self.assertEqual(demoted['device_modes'],{})
        self.assertEqual(reconcile_admissions(demoted,[self.device],{})['device_modes']['heater'],'control_verification')
        revised=reconcile_admissions(options,[{**self.device,'planning_choice_at':'role-2'}],{})
        self.assertEqual(revised['device_modes']['heater'],'control_verification')

    def test_exclusion_removes_inventory_and_queued_authority_immediately(self):
        options=reconcile_admissions({},[self.device],{})
        options['device_modes']['heater']='controlling'
        excluded=prepare_options(options,{'excluded_device_readings':['heater']},lambda _:None)
        self.assertEqual(excluded['device_modes'],{})
        self.assertEqual(excluded['planning_admissions'],{})
        self.assertEqual(planning_devices([self.device],excluded),[])
        readded=prepare_options(excluded,{'excluded_device_readings':[]},lambda _:None)
        self.assertEqual(reconcile_admissions(readded,[self.device],{})['device_modes']['heater'],'control_verification')
