"""Independent synthetic power/activity observations, not household fixtures."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import unittest
import asyncio
from unittest.mock import AsyncMock
from types import SimpleNamespace
sys.path.append(str(Path(__file__).parents[1] / 'custom_components' / 'shs_energy'))
from shs_core.household_ports import HouseholdReadError, ConfigurationChanged
from shs_core.room_electrical import OnCondition, attributed_device_quarters, capture_room_on_power, room_on_power
from shs_core.device_draw import DrawBinding, learn_device_draw

START = datetime(2026, 1, 1, tzinfo=timezone.utc)
def at(minutes): return START+timedelta(minutes=minutes)
def row(minutes, action): return (at(minutes), 'heat', {'hvac_action': action})

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
