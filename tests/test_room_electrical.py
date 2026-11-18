"""Independent synthetic power/activity observations, not household fixtures."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import unittest
import asyncio
from unittest.mock import AsyncMock, patch
from types import SimpleNamespace
import json
from copy import deepcopy
sys.path.append(str(Path(__file__).parents[1] / 'custom_components' / 'shs_energy'))
from shs_core.household_ports import HouseholdReadError, ConfigurationChanged
from shs_core.room_electrical import OnCondition, attributed_device_quarters, capture_room_on_power, room_on_power
from shs_core.room_electrical import heating_quarters, weather_heating_bindings
from shs_core.device_draw import DrawBinding, learn_device_draw
from shs_core.room_electrical import OnPowerEvidence, retain_running_power_model

START = datetime(2026, 1, 1, tzinfo=timezone.utc)
def at(minutes): return START+timedelta(minutes=minutes)
def row(minutes, action): return (at(minutes), 'heat', {'hvac_action': action})


class WeatherHeatingTests(unittest.TestCase):
    def test_only_complete_heating_quarters_are_qualified(self):
        states = [row(-5, 'heating'), row(6, 'idle'), row(12, 'heating'),
                  (at(31), 'fan_only', {'hvac_action': 'heating'}), row(45, 'heating'),
                  (at(60), 'unavailable', {}), row(76, 'heating')]
        self.assertEqual(heating_quarters('climate.room', states, START, at(95)),
                         {at(15).isoformat(), at(45).isoformat()})

    def test_weather_learning_uses_a_dedicated_reversible_thermostat_mapping(self):
        device = {'key': 'meter', 'control_type': 'setpoint', 'category': 'cooling',
                  'load_type': 'inverter', 'statistic_id': 'sensor.energy'}
        mapping = {'actuator_entity_ids': ['climate.room'], 'power': 'sensor.power'}
        read = lambda _entity: SimpleNamespace(attributes={'hvac_modes': ['off', 'heat', 'cool']})
        self.assertEqual(weather_heating_bindings([device], {'meter': mapping}, read),
                         {'meter': ('climate.room', 'climate.room|sensor.power|sensor.energy')})
        self.assertEqual(weather_heating_bindings([device], {'meter': mapping, 'shared': mapping}, read), {})
        self.assertEqual(weather_heating_bindings([{**device, 'control_type': 'switch_schedule'}],
                                                   {'meter': mapping}, read), {})

class AttributedDrawTests(unittest.TestCase):
    def test_change_only_meter_can_measure_constant_load_cycles(self):
        activity, power = [], []
        for day in range(3):
            start = START + timedelta(days=day, hours=12)
            activity.extend((when,'heat',{'hvac_action':action}) for when,action in (
                (start-timedelta(hours=1),'idle'), (start,'heating'),
                (start+timedelta(minutes=10),'idle')))
            if not power:
                power.append((start-timedelta(hours=1),'3000',{'unit_of_measurement':'W'}))
            power.extend((when,value,{'unit_of_measurement':'W'}) for when,value in (
                (start,'4000'),(start+timedelta(minutes=10),'3000')))
        draw = learn_device_draw(DrawBinding('climate.room','sensor.circuit'),activity,power)
        self.assertEqual(draw.startup_w,1000)
        self.assertEqual(draw.running_w,1000)

    def test_repeated_cycles_separate_outlet_load_and_startup_from_running_power(self):
        activity, power = [], []
        # The first three days have the same heater on different outlet loads.
        # On the fourth a lone faulty high reading coincides with startup.
        for day, (outlets, peak) in enumerate(((2,986), (3100,969), (800,913), (2,4852))):
            start = START + timedelta(days=day, minutes=15)
            activity.extend((when, 'heat', {'hvac_action': action}) for when,action in (
                (start-timedelta(minutes=3),'idle'), (start,'heating'),
                (start+timedelta(seconds=145),'idle')))
            power.extend((start+timedelta(seconds=offset),str((outlets+heater)/1000),
                          {'unit_of_measurement':'kW'}) for offset,heater in (
                (-120,0),(-60,0),(-4,peak),(2,695),(14,604),(30,480),(116,405),(139,0),(160,0)))
        binding = DrawBinding('climate.room','sensor.circuit')
        self.assertIsNone(learn_device_draw(binding,activity[:6],power[:18]))
        draw = learn_device_draw(binding,activity,power)
        self.assertEqual(draw.cycles,3)
        self.assertAlmostEqual(draw.startup_w,986)
        self.assertGreater(draw.running_w,400)
        self.assertLess(draw.running_w,600)
        self.assertAlmostEqual(sum(draw.quarter_kwh.values()),draw.running_w*draw.on_seconds/3_600_000)
        # Keep unrelated metered energy and remove the misleading circuit total.
        rows = [{"start": at(15).isoformat(), "device_energy_kwh":{"room":1.0,"other":0.3}},
                {"start": at(30).isoformat(), "device_energy_kwh":{"room":0.8}}]
        attributed = attributed_device_quarters(rows,{"room":{"quarter_kwh":draw.quarter_kwh}},START,at(45))
        self.assertEqual(len(attributed),2)  # The measured cycle straddles the quarter.
        self.assertEqual(attributed[1]["device_energy_kwh"]["other"],0.3)
        self.assertLess(attributed[1]["device_energy_kwh"]["room"],0.1)

class OnPowerTests(unittest.TestCase):
    def learn(self, rows, means=None, condition=None, extras=None):
        return room_on_power(conditions={'room':condition or OnCondition('sensor.power',('climate.room',),())},
            power_means={'sensor.power':means if means is not None else [(at(0),1100),(at(5),1300),(at(10),2)]},
            actuator_rows={'climate.room':rows, **(extras or {})},start=START,end=at(15))['room']

    def test_historical_on_mean_is_not_idle_draw_or_peak(self):
        found=self.learn([row(0,'heating'),row(10,'idle')])
        self.assertAlmostEqual(found.expected_w,1200)
        self.assertAlmostEqual(found.on_hours,1/6)
        self.assertEqual(found.intervals,2)
        self.assertEqual(found.source,'recorded_on_intervals')

    def test_typical_on_draw_resists_an_isolated_meter_spike(self):
        found = self.learn([row(0, 'heating')], [(at(0), 1100), (at(5), 4900), (at(10), 1200)])
        self.assertEqual(found.expected_w, 1200)
        self.assertEqual(found.intervals, 3)
        self.assertEqual(found.on_hours, 0.25)

    def test_mixed_and_unknown_buckets_are_not_prorated(self):
        found=self.learn([row(0,'heating'),row(2,'idle'),(at(5),'unavailable',{}),row(10,'heating')])
        self.assertEqual(found.expected_w,2)
        self.assertEqual(found.intervals,1)  # A small genuine ON observation is not thresholded.

    def test_climate_permission_without_action_is_unknown(self):
        self.assertIsNone(self.learn([(START,'heat',{})]).expected_w)
        self.assertIsNone(self.learn([row(0,'idle')]).expected_w)

    def test_switch_uses_explicit_on_and_duplicate_activity_does_not_split_support(self):
        found=self.learn([],condition=OnCondition('sensor.power',('switch.room',),()),extras={
            'switch.room':[(at(0),'on',{}),(at(2),'on',{}),(at(10),'off',{})]})
        self.assertEqual(found.expected_w,1200)

    def test_shared_meter_requires_every_other_actuator_explicitly_off(self):
        condition=OnCondition('sensor.power',('climate.room',),('climate.other',))
        for activity,expected in [('heating',None),('cooling',None),('idle',1200),(None,None)]:
            with self.subTest(activity=activity):
                found=self.learn([row(0,'heating'),row(10,'idle')],condition=condition,
                    extras={'climate.other':[(START,'heat',{'hvac_action':activity})]})
                self.assertEqual(found.expected_w,expected)

    def test_missing_start_partial_end_and_invalid_power_are_unknown(self):
        self.assertIsNone(self.learn([row(1,'heating')],[(at(0),1000),(at(12),1000)]).expected_w)
        for value in [None,float('nan'),float('inf'),-1,True]:
            self.assertIsNone(self.learn([row(0,'heating')],[(at(0),value)]).expected_w)
        self.assertEqual(self.learn([row(0,'heating')],[(at(0),0)]).expected_w,0)

class CaptureTests(unittest.IsolatedAsyncioTestCase):
    def device(self,key='room'):return {'key':key,'control_type':'setpoint','category':'heating'}
    def mapping(self,power='sensor.power'):return {'power':power,'actuator_entity_ids':['climate.room'],'control_type':'setpoint'}
    def history(self):return SimpleNamespace(statistics=AsyncMock(return_value={'sensor.power':[
        {'start':START.timestamp(),'mean':1180},{'start':at(5),'mean':1220},{'start':at(10),'mean':3}]}),
        states=AsyncMock(return_value={'climate.room':[row(0,'heating'),row(10,'idle')]}))

    async def test_batch_adapter_requests_normalized_watts_and_activity_attributes(self):
        h=self.history();found=await capture_room_on_power(h,[self.device()],{'room':self.mapping()},START,at(15))
        self.assertEqual(found['room'].expected_w,1200)
        h.statistics.assert_awaited_once_with(START,at(15),{'sensor.power'},'5minute',{'power':'W'},{'mean'})
        h.states.assert_awaited_once_with(START,at(15),['climate.room'],with_attributes=True)

    async def test_explicit_numeric_watts_do_not_read_history(self):
        h=self.history();found=await capture_room_on_power(h,[self.device()],{'room':self.mapping(1700)},START,at(15))
        self.assertEqual(found['room'].expected_w,1700)
        self.assertEqual(found['room'].source,'configured');h.states.assert_not_awaited();h.statistics.assert_not_awaited()

    async def test_attributed_running_watts_replace_raw_room_means(self):
        h = self.history()
        found = await capture_room_on_power(h, [self.device()], {'room':self.mapping()}, START, at(15),
            learned_draws={'room':{'startup_w':986,'running_w':455,'on_seconds':360,'cycles':3}})
        self.assertEqual(found['room'].expected_w,455)
        self.assertEqual(found['room'].as_inference()['method'],'room_attributed_cycle_energy_v1')
        h.statistics.assert_not_awaited()
        h.states.assert_not_awaited()

    async def test_remapping_cannot_reuse_old_sensor_or_prior_evidence(self):
        h=self.history()
        first=await capture_room_on_power(h,[self.device()],{'room':self.mapping()},START,at(15))
        second=await capture_room_on_power(h,[self.device()],{'room':self.mapping('sensor.replacement')},START,at(15))
        self.assertEqual(first['room'].expected_w,1200)
        self.assertIsNone(second['room'].expected_w)
        self.assertEqual(second['room'].condition.power,'sensor.replacement')

    async def test_shared_meter_without_observable_other_actuator_is_unknown(self):
        h=self.history();found=await capture_room_on_power(h,[self.device()],{
            'room':self.mapping(),'other':{'power':'sensor.power'}},START,at(15))
        self.assertIsNone(found['room'].expected_w);self.assertEqual(found['room'].source,'shared_meter_unknown')
        h.statistics.assert_not_awaited()

    async def test_unavailable_history_is_explicit_unknown_and_keeps_configured_power(self):
        for source in ('states', 'statistics'):
            with self.subTest(source=source):
                h=self.history();getattr(h,source).side_effect=HouseholdReadError('recorder unavailable')
                devices=[self.device(),dict(self.device(),key='configured')]
                with self.assertLogs('shs_core.room_electrical',level='WARNING') as logs:
                    found=await capture_room_on_power(h,devices,{'room':self.mapping(),
                        'configured':self.mapping(1700)},START,at(15))
                self.assertIsNone(found['room'].expected_w)
                self.assertEqual(found['room'].source,'source_unavailable')
                self.assertEqual(found['room'].intervals,0)
                self.assertEqual(found['room'].condition.power,'sensor.power')
                self.assertEqual(found['configured'].expected_w,1700)
                self.assertIn('recorder unavailable',logs.output[0])

    async def test_programming_configuration_and_cancellation_failures_propagate(self):
        for error in (RuntimeError('bug'),ValueError('invalid'),TypeError('invalid'),
                      ConfigurationChanged('remapped'),asyncio.CancelledError()):
            with self.subTest(error=type(error).__name__):
                h=self.history();h.states.side_effect=error
                with self.assertRaises(type(error)):
                    await capture_room_on_power(h,[self.device()],{'room':self.mapping()},START,at(15))


class InventoryEvidenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_capture_uploads_learned_power_without_copying_planning_preferences(self):
        """Synthetic idle/ON readings cross the actual snapshot/inventory copy boundary."""
        from household_fixture import Rig
        from shs_core.api import ShsApiClient
        from shs_core.operating_modes import planning_devices
        rig = Rig(now=at(15))
        mappings = {'room': {'control_type':'setpoint', 'power':'sensor.power',
                            'actuator_entity_ids':['climate.room']}}
        options = {'planning_mode':'live', 'device_control_mappings':mappings}
        inventory = [{'key':'room', 'statistic_id':'sensor.energy', 'name':'Synthetic room heater',
            'category':'heating', 'control_type':'setpoint', 'planning_role':'controllable',
            'suggested_load_type':'duty_cycle', 'load_type':'duty_cycle', 'mapping_status':'ready',
            'active_power_w':None, 'profile_sample_count':0, 'inference':{'method':'synthetic_discovery'}}]
        rig.history.statistics.return_value = {'sensor.power':[
            {'start':START.timestamp(),'mean':1100}, {'start':at(5).timestamp(),'mean':1300},
            {'start':at(10).timestamp(),'mean':2}]}
        rig.history.states.return_value = {'climate.room':[row(0,'heating'),row(10,'idle')]}
        rig.household._refresh_counter_bounds = AsyncMock()
        rig.household._planning_actual_quarters = AsyncMock(return_value=([],[
            {'start':START.isoformat(),'device_energy_kwh':{'room':.2}}]))
        rig.household._boiler_running_power_w = AsyncMock(return_value={})
        rig.household._supplier_price_catalog = lambda: {
            'configuration':{'price_area':'synthetic'},'issued_at':START.isoformat()}
        def scoped(devices, options):
            copied = planning_devices(devices, options)
            copied[0]['name'] = 'Planning-only name'
            copied[0]['load_type'] = 'fixed_full_load'
            return copied
        class Captured(Exception):
            pass
        # Stop after the real model capture, before unrelated house forecasts.
        with patch('shs_core.operating_modes.planning_devices', scoped), \
                patch('shs_core.household.supplier_price_forecast', return_value={}), \
                patch('shs_core.household.build_base_load_model', side_effect=Captured):
            with self.assertRaises(Captured):
                await rig.household._build_optimisation_snapshot(options,{},[],{},inventory)
        uploaded = ShsApiClient.prepare_optimisation_submission([],None,inventory)['devices'][0]
        self.assertEqual(uploaded['active_power_w'],1200)
        self.assertEqual(uploaded['profile_sample_count'],1)
        self.assertEqual(uploaded['inference']['on_power']['expected_w'],1200)
        self.assertEqual(uploaded['inference']['on_power']['intervals'],2)
        self.assertEqual(uploaded['inference']['method'],'synthetic_discovery')
        self.assertEqual(uploaded['name'],'Synthetic room heater')
        self.assertEqual(uploaded['load_type'],'duty_cycle')
        self.assertEqual(uploaded['planning_role'],'controllable')


class RetainedRunningPowerTests(unittest.TestCase):
    """Synthetic seasonal transitions use the same durable inventory as uploads."""
    def setUp(self):
        self.device = {'key':'heater','statistic_id':'sensor.energy','name':'Synthetic heater',
            'category':'heating','control_type':'setpoint','planning_role':'controllable',
            'suggested_load_type':'duty_cycle','load_type':'duty_cycle',
            'active_power_w':9000,'inference':{'method':'synthetic_discovery'}}
        self.mappings = {'heater':{'control_type':'setpoint','power':'sensor.power',
                                   'actuator_entity_ids':['climate.room']}}

    def observed(self, watts=1200):
        power = self.mappings['heater']['power']
        actuator = self.mappings['heater']['actuator_entity_ids'][0]
        return room_on_power(conditions={'heater':OnCondition(power,(actuator,),())},
            power_means={power:[(START,watts),(at(5),watts)]},
            actuator_rows={actuator:[row(0,'heating'),row(10,'idle')]},
            start=START,end=at(15))['heater']

    def build(self, evidence):
        from shs_core.planning import build_device_models
        # No energy profile can supply running watts; a current standby sensor
        # and an unrelated descriptive previous rating cannot either.
        models,_ = build_device_models([self.device],[],[at(15)],self.mappings,
            mapped_power_w=lambda _:2,pool_power_w={},room_on_power={'heater':evidence},local_tz=timezone.utc)
        return models[0]

    def test_summer_no_on_retains_fitted_source_and_training_provenance_after_restart(self):
        from shs_core.api import ShsApiClient
        fitted = self.build(self.observed())
        artifact = deepcopy(fitted['inference']['on_power'])
        snapshot = {'snapshot_id':'synthetic', 'device_models':[fitted]}
        body = ShsApiClient.prepare_optimisation_submission([],snapshot,[self.device])
        artifact_source = {'revision':artifact['revision'],'binding':artifact['binding']}
        self.assertEqual(body['snapshot']['device_models'][0]['running_power_model'],artifact_source)
        # This is the shape round-tripped by optimisation_device_metadata.
        self.device = json.loads(json.dumps(self.device))
        for source in ('no_on_coverage','source_unavailable'):
            model = self.build(OnPowerEvidence(None,0,0,source))
            selected = model['inference']['on_power']
            self.assertEqual(model['active_power_w'],1200)
            self.assertTrue(selected['retained'])
            self.assertEqual(selected['revision'],artifact['revision'])
            self.assertEqual(selected['observed_from'],START.isoformat())
            self.assertEqual(selected['observed_to'],at(10).isoformat())
            self.assertEqual(selected['latest_attempt']['source'],source)
            self.assertEqual(model['running_power_model'],
                {'revision':artifact['revision'],'binding':artifact['binding']})
            self.assertNotIn('running_power_unknown',{w['code'] for w in model['planning_warnings']})
        # A new identified measurement replaces this source's constant fit.
        updated = self.build(self.observed(1500))
        self.assertEqual(updated['active_power_w'],1500)
        self.assertFalse(updated['inference']['on_power']['retained'])
        self.assertNotEqual(updated['inference']['on_power']['revision'],artifact['revision'])
        self.assertEqual(len(updated['inference']['on_power_models']),1)

    def test_changed_meter_or_actuator_cannot_reuse_fit_and_preserves_old_source_archive(self):
        old = self.build(self.observed())['inference']['on_power']
        retained = deepcopy(self.device)
        replacements = [({'power':'sensor.new_power'},None),
                        ({'actuator_entity_ids':['climate.new_heater']},None),
                        ({},'sensor.new_energy')]
        for changes,statistic in replacements:
            with self.subTest(changes=changes,statistic=statistic):
                self.device = deepcopy(retained)
                self.mappings['heater'] = {'control_type':'setpoint','power':'sensor.power',
                    'actuator_entity_ids':['climate.room'],**changes}
                if statistic:
                    self.device['statistic_id'] = statistic
                result = self.build(OnPowerEvidence(None,0,0,'no_on_coverage'))
                self.assertIsNone(result['active_power_w'])
                self.assertNotIn('running_power_model',result)
                self.assertEqual(result['inference']['on_power_models'][old['binding']]['revision'],old['revision'])
                fresh = self.build(self.observed(1600))
                self.assertEqual(len(fresh['inference']['on_power_models']),2)

    def test_shared_meter_contract_change_and_unqualified_previous_rating_stay_unknown(self):
        self.device['running_power_model'] = {'revision':'stale','binding':'stale'}
        unknown = self.build(OnPowerEvidence(None,0,0,'no_on_coverage'))
        self.assertIsNone(unknown['active_power_w'])
        self.assertNotIn('running_power_model',unknown)
        old = self.build(self.observed())['inference']['on_power']
        self.mappings['other'] = {'control_type':'setpoint','power':'sensor.power',
                                 'actuator_entity_ids':['climate.other']}
        result = self.build(OnPowerEvidence(None,0,0,'shared_meter_unknown'))
        self.assertIsNone(result['active_power_w'])
        self.assertIn(old['binding'],result['inference']['on_power_models'])

    def test_identical_observations_keep_revision_and_numeric_configuration_is_not_archived(self):
        first = self.build(self.observed())['inference']['on_power']
        second = self.build(self.observed())['inference']['on_power']
        self.assertEqual(first['revision'],second['revision'])
        configured = OnPowerEvidence(1800,0,0,'configured')
        self.mappings['heater']['power'] = 1800
        watts,metadata = retain_running_power_model(self.device,self.mappings,configured)
        self.assertEqual(watts,1800)
        self.assertEqual(len(metadata['on_power_models']),1)
