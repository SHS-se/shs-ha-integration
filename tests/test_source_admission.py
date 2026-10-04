"""Consumer unions and exact pool obligations govern early event admission."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from pathlib import Path
import sys
import unittest

sys.path.append(str(Path(__file__).parents[1]/'custom_components/shs_energy'))

from shs_core.source_admission import (source_bindings, validate_bindings, ordered,
    pool_paused, native_pool_idle, temperature_available, validate_pool_pause)


class AdmissionTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime.now(timezone.utc)
        self.off = SimpleNamespace(state='off')
        self.owner = SimpleNamespace(records={})
        self.context = dict(configuration_revision=4,policy_revision=7)
        self.pause = dict(**self.context,start=self.now.isoformat(),entity='switch.pool')

    def test_consumer_union_keeps_battery_and_counter_facts_when_pool_is_idle(self):
        options = dict(pool_water_temperature_entity='sensor.shared',battery_soc_entity='sensor.shared',
            weather_entity='weather.home',entities_grid_import=['sensor.energy'])
        uses = validate_bindings(source_bindings(options,[]))
        self.assertTrue(ordered(uses['sensor.shared'],pool_idle=True))
        self.assertTrue(ordered(uses['sensor.energy']))
        self.assertFalse(ordered(uses['weather.home']))
        self.assertFalse(ordered({'pool_temperature','reference'},pool_idle=True))
        self.assertTrue(ordered({'pool_temperature','reference'},pool_idle=False))

    def test_observed_on_restoration_and_slot_end_require_attention(self):
        idle = lambda: native_pool_idle(self.pause,None,self.context,lambda _:self.off,self.owner,self.now)
        self.assertTrue(idle())
        self.off.state='on';self.assertFalse(idle());self.off.state='off'
        self.owner.records['pool']={'restoration_pending':True};self.assertFalse(idle())
        self.owner.records.clear()
        self.pause['start']=(self.now-timedelta(minutes=15)).isoformat();self.assertFalse(idle())

    def test_verification_attention_follows_simulated_heating_and_current_slot(self):
        slot=dict(start=self.now.isoformat(),pool_w=0)
        status=dict(state='verified',slot_start=slot['start'],plan_id='plan',
            requested_power_w=0,requested_switch_state='off',control_entity='switch.pool')
        paused=lambda:pool_paused(status,slot,{'plan_id':'plan'},lambda _:self.off,self.owner)
        self.assertTrue(paused())
        slot['pool_w']=2000;self.assertFalse(paused())
        slot['pool_w']=0;status['requested_power_w']=2000;self.assertFalse(paused())
        status['requested_power_w']=0;status['state']='pending';self.assertFalse(paused())

    def test_invalid_sensor_and_unknown_interest_fail_explicitly(self):
        for value in (None,SimpleNamespace(state='unavailable',attributes={'unit_of_measurement':'°C'}),
                      SimpleNamespace(state='30',attributes={'unit_of_measurement':'W'})):
            self.assertFalse(temperature_available(value))
        for value in ({'sensor.a':['wrong']},{'sensor.a':['capture','capture']}):
            with self.assertRaises(ValueError):validate_bindings(value)
        with self.assertRaises(ValueError):validate_pool_pause({'entity':'switch.pool','until':'2026-10-03T10:00:00'})
