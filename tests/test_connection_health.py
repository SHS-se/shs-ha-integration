"""Connection policy changes neither plan failures nor device state."""
import asyncio
from copy import deepcopy
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock
sys.path.append(str(Path(__file__).parents[1]/'custom_components/shs_energy'))
from shs_core.connection_health import ConnectionHealth, RetryBackoff
from household_fixture import Rig, Store
from shs_core.api import ShsApiError, ShsConnectionError

class HealthTests(unittest.IsolatedAsyncioTestCase):
    async def test_hour_is_continuous_and_other_lanes_do_not_clear_it(self):
        now=[0.0];scheduled=[];publish=Mock();store=Store()
        health=ConnectionHealth(store,publish,clock=lambda:now[0],call_later=lambda delay,callback:scheduled.append((delay,callback)) or Mock())
        health.failed('notifications',ConnectionError('lost'))
        now[0]=3599;self.assertFalse(health.persistent('notifications'))
        health.verified('cloud_status');self.assertFalse(health.persistent('notifications'))
        now[0]=3600;scheduled[0][1]();self.assertTrue(health.persistent('notifications'));self.assertTrue(publish.called)
        health.verified('notifications');self.assertFalse(health.persistent('notifications'))
        health.failed('notifications',ConnectionError('again'));now[0]=7199
        self.assertFalse(health.persistent('notifications'))
        await health.close()

    async def test_restart_preserves_observed_time_and_excludes_downtime(self):
        now=[0.0];store=Store();timer=lambda *_:Mock()
        health=ConnectionHealth(store,lambda:None,clock=lambda:now[0],call_later=timer)
        health.failed('notifications',ConnectionError('lost'));now[0]=2400
        await health.close()
        now[0]=900000
        restored=ConnectionHealth(store,lambda:None,clock=lambda:now[0],call_later=timer)
        await restored.restore();self.assertFalse(restored.persistent('notifications'))
        restored.failed('notifications',ConnectionError('still lost'));now[0]+=1200
        self.assertTrue(restored.persistent('notifications'))
        restored.verified('notifications');self.assertEqual(store.saved,{})
        await restored.close()

    async def test_real_planning_error_stays_immediate_and_success_is_stage_specific(self):
        h=Rig().household
        h._pipeline_error('notifications',ShsConnectionError('temporarily down'))
        self.assertFalse(any(i['key']=='pipeline:notifications' for i in h.attention_items))
        h._pipeline_error('planning',ShsApiError('Plan could not be solved'))
        self.assertTrue(any(i['key']=='pipeline:planning' for i in h.attention_items))
        h.connection_health.episodes['notifications']['observed_seconds']=3600
        self.assertTrue(any(i['key']=='pipeline:notifications' for i in h.attention_items))
        h._pipeline_error('notifications',None)
        self.assertFalse(any(i['key']=='pipeline:notifications' for i in h.attention_items))
        self.assertTrue(any(i['key']=='pipeline:planning' for i in h.attention_items))
        await h.connection_health.close()

    def test_backoff_caps_and_resets(self):
        retries=RetryBackoff(jitter=lambda low,high:high)
        self.assertEqual([retries.next() for _ in range(7)],[5,10,20,40,60,60,60])
        retries.reset();self.assertEqual(retries.next(),5)
