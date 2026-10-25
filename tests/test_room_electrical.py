"""Independent synthetic power/activity observations, not household fixtures."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import unittest
from unittest.mock import AsyncMock
from types import SimpleNamespace
sys.path.append(str(Path(__file__).parents[1] / 'custom_components' / 'shs_energy'))
from shs_core.room_electrical import OnCondition, capture_room_on_power, room_on_power

START = datetime(2026, 1, 1, tzinfo=timezone.utc)
def at(minutes): return START+timedelta(minutes=minutes)
def row(minutes, action): return (at(minutes), 'heat', {'hvac_action': action})

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

    async def test_history_failure_is_not_fabricated_model_evidence(self):
        h=self.history();h.statistics.side_effect=RuntimeError('recorder failed')
        with self.assertRaisesRegex(RuntimeError,'recorder failed'):
            await capture_room_on_power(h,[self.device()],{'room':self.mapping()},START,at(15))
