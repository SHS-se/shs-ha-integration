"""Exercise the coordinator's actual lifecycle methods at its HA boundary."""
import ast
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
import unittest
from time import monotonic
from unittest.mock import AsyncMock, patch
import sys

ROOT = Path(__file__).parents[1] / 'custom_components/shs_energy'
sys.path.append(str(ROOT))
from refresh import refresh_in_progress
from shs_core.presentation import operational_status
from household_fixture import Rig, cloud_status
from shs_core.api import ShsApiError, ShsConnectionError


class RecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_unexpected_replan_answer_failure_remains_until_that_request_succeeds(self):
        self.c.async_optimisation_push.side_effect=RuntimeError('Source admission crashed')
        with self.assertRaisesRegex(RuntimeError,'Source admission crashed'):
            await self.c.async_answer_replan('request')
        self.assertTrue(any(i['key']=='pipeline:replan_request' for i in self.c.attention_items))
        self.c._pipeline_error('notifications',None)
        self.assertTrue(any(i['key']=='pipeline:replan_request' for i in self.c.attention_items))
        self.assertIsNone(self.c._answered_replan_request_id)
        self.c.async_optimisation_push.side_effect=None
        await self.c.async_answer_replan('request')
        self.assertFalse(any(i['key']=='pipeline:replan_request' for i in self.c.attention_items))

    async def test_failed_runtime_report_is_visible_until_that_report_recovers(self):
        self.c.client.report_runtime.side_effect=ShsApiError('Controller status upload failed')
        await self.c.async_report_runtime()
        self.assertTrue(any(i['key']=='pipeline:runtime_report' for i in self.c.attention_items))
        self.c._pipeline_error('upload',None)
        self.assertTrue(any(i['key']=='pipeline:runtime_report' for i in self.c.attention_items))
        self.c.client.report_runtime.side_effect=None
        await self.c.async_report_runtime()
        self.assertFalse(any(i['key']=='pipeline:runtime_report' for i in self.c.attention_items))

    async def test_input_accuracy_is_reported_when_it_changes_at_most_every_five_minutes(self):
        from unittest.mock import PropertyMock
        def accuracy(percentage, minute):
            return {'version': 1, 'percentage': percentage, 'checks': [],
                    'observed_at': (self.now + timedelta(minutes=minute)).isoformat()}
        sent = self.c.client.report_runtime
        with patch.object(type(self.c), 'input_accuracy', new_callable=PropertyMock) as current:
            async def check(percentage, minute):
                current.return_value = accuracy(percentage, minute)
                self.rig.now = self.now + timedelta(minutes=minute)
                await self.c.async_report_input_accuracy()
                return sent.await_count
            self.assertEqual(await check(100, 0), 1)
            self.assertEqual(await check(100, 1), 1, 'a newer check time alone is no change')
            self.assertEqual(await check(88, 2), 1, 'a change waits until five minutes after the last report')
            self.assertEqual(await check(88, 5), 2)
            self.assertEqual(sent.await_args.kwargs['input_accuracy']['percentage'], 88)
            sent.side_effect = ShsApiError('Controller status upload failed')
            self.assertEqual(await check(75, 10), 3)
            self.assertEqual(await check(75, 11), 3, 'a failed report is not retried every minute')
            sent.side_effect = None
            self.assertEqual(await check(75, 15), 4)

    async def test_notification_failure_clears_only_after_the_long_poll_succeeds(self):
        from shs_core.replan_listener import listen_for_replans
        retry,release,connected=asyncio.Event(),asyncio.Event(),asyncio.Event()
        calls=0
        async def wait(_after):
            nonlocal calls
            calls+=1
            if calls==1:raise ShsConnectionError('Notification connection lost')
            if calls==2:
                retry.set();await release.wait();return None
            connected.set();await asyncio.Future()
        self.c.client.wait_for_replan=wait
        async def listen(wait,answer,report,**kwargs):
            await listen_for_replans(wait,answer,report,retry_seconds=0,**kwargs)
        with patch('shs_core.household.listen_for_replans',listen):
            task=asyncio.create_task(self.c.async_replan_listener())
            try:
                await asyncio.wait_for(retry.wait(),1)
                self.assertFalse(any(i['key']=='pipeline:notifications' for i in self.c.attention_items))
                self.c.connection_health.episodes['notifications']['observed_seconds']=3600
                self.assertTrue(any(i['key']=='pipeline:notifications' for i in self.c.attention_items))
                release.set();await asyncio.wait_for(connected.wait(),1)
                self.assertFalse(any(i['key']=='pipeline:notifications' for i in self.c.attention_items))
            finally:
                task.cancel();await asyncio.gather(task,return_exceptions=True)

    async def test_runtime_report_carries_actual_battery_admission_instead_of_cached_plan(self):
        feedback={'accepted':{'plan_id':'admitted','contract_id':'reference','scope_revision':'scope','generation':3},'rejection':None}
        self.c.battery_runtime=SimpleNamespace(plan_feedback=lambda:feedback)
        self.c.optimisation_plan={'plan_id':'only-cached'}
        await self.c.async_report_runtime()
        self.assertEqual(self.c.client.report_runtime.await_args.args[0]['battery_plan'],feedback)

    def setUp(self):
        self.now = datetime(2026, 9, 9, 10, tzinfo=timezone.utc)
        self.rig = Rig(now=self.now)
        self.c = self.rig.household
        self.c._runtime_lock = asyncio.Lock()
        self.c._push_lock = asyncio.Lock()
        self.c._replan_lock = asyncio.Lock()
        self.c._recovering = False
        self.c._answered_replan_request_id = None
        self.c.last_optimisation_error = None
        self.c.client = SimpleNamespace(report_runtime=AsyncMock(return_value=cloud_status()))
        self.c.async_update_listeners = lambda: None
        self.c.async_optimisation_push = AsyncMock()
        self.c.async_request_refresh = AsyncMock()
        self.c._report_replan_failure = AsyncMock()
        self.set_status(False)

    def set_status(self, ready):
        self.c.optimisation_plan = None
        if ready:
            plan = json.loads((Path(__file__).parent / 'fixtures/schema-7-device-plan.json').read_text())['plan']
            self.c.optimisation_plan = plan
            self.rig.now = datetime.fromisoformat(plan['issued_at']) + timedelta(minutes=1)

    async def test_each_interval_exchanges_even_with_a_healthy_cached_plan(self):
        self.set_status(True)
        await self.c.async_replan_poll()
        self.c.async_request_refresh.assert_awaited_once()
        self.c.async_optimisation_push.assert_awaited_once_with(force_plan=False, replan_request_id=None)
        self.assertFalse(self.c._recovering)
        self.assertFalse(self.c.client.report_runtime.await_args.args[0]['recovering'])

    async def test_outage_retains_the_plan_and_waits_for_the_next_interval(self):
        plan = {'plan_id': 'cached'}
        self.c.optimisation_plan = plan
        self.c.client.report_runtime.side_effect = ShsApiError('offline')
        self.c.async_optimisation_push.side_effect = RuntimeError('offline')
        with self.assertRaises(RuntimeError):
            await self.c.async_replan_poll()
        self.assertIs(self.c.optimisation_plan, plan)
        self.assertEqual(self.c.async_optimisation_push.await_count, 1)
        self.assertFalse(self.c._recovering)
        self.assertEqual(self.c.last_runtime_error, 'offline')

    async def test_inflight_exchange_does_not_queue_duplicate_requests(self):
        async with self.c._push_lock:
            await self.c.async_replan_poll()
        self.c._recovering = True
        await self.c.async_replan_poll()
        self.c.async_optimisation_push.assert_not_awaited()
        self.c.client.report_runtime.assert_not_awaited()

    async def test_explicit_request_is_attached_only_once(self):
        self.c.client.report_runtime.return_value = cloud_status(pending_replan_request_id='request')
        await self.c.async_replan_poll()
        self.c.async_optimisation_push.assert_awaited_once_with(force_plan=True, replan_request_id='request')
        await self.c.async_replan_poll()
        self.c.async_optimisation_push.assert_awaited_with(force_plan=False, replan_request_id=None)

    async def test_notification_and_poll_coalesce_the_same_request_while_busy(self):
        entered = asyncio.Event()
        release = asyncio.Event()
        async def push(**kwargs):
            entered.set()
            await release.wait()
        self.c.async_optimisation_push.side_effect = push
        first = asyncio.create_task(self.c.async_answer_replan('request'))
        await entered.wait()
        second = asyncio.create_task(self.c.async_answer_replan('request'))
        release.set()
        self.assertEqual(await asyncio.gather(first, second), [True, False])
        self.c.async_optimisation_push.assert_awaited_once_with(force_plan=True, replan_request_id='request')

    async def test_retired_planning_setting_does_not_override_current_configuration(self):
        self.rig.options['planning_mode'] = 'disabled'
        self.c.client.report_runtime.return_value = cloud_status(pending_replan_request_id='request')
        await self.c.async_replan_poll()
        self.c._report_replan_failure.assert_not_awaited()
        self.c.async_optimisation_push.assert_awaited_once_with(force_plan=True, replan_request_id="request")

    async def test_restart_restores_saved_plan_but_expired_or_invalid_never_execute(self):
        plan = json.loads((Path(__file__).parent / 'fixtures/schema-7-device-plan.json').read_text())['plan']
        self.c._store = SimpleNamespace(async_load=AsyncMock(return_value={'optimisation_plan': plan}))
        await self.c.async_restore_plan()
        issued = datetime.fromisoformat(plan['issued_at'])
        state = operational_status(self.c.optimisation_plan, 'live', [], issued + timedelta(minutes=1))
        self.assertTrue(state['actionable'])
        state = operational_status(self.c.optimisation_plan, 'live', [], datetime.fromisoformat(plan['valid_until']))
        self.assertEqual(state['state'], 'expired')
        self.assertFalse(state['actionable'])
        plan['schema_version'] = 999
        state = operational_status(self.c.optimisation_plan, 'live', [], issued)
        self.assertEqual(state['state'], 'invalid')
        self.assertFalse(state['actionable'])


class RuntimeDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_http_success_without_stored_report_is_not_successful_delivery(self):
        from shs_core.api import ShsApiClient
        client = ShsApiClient(None, 'https://example.invalid', 'test')
        client._request = AsyncMock()
        for response in ({}, {'runtime_received': False}, {'runtime_received': None}):
            client._request.return_value = response
            with self.assertRaisesRegex(ShsApiError, 'did not confirm'):
                await client.report_runtime({'state': 'unavailable'})
        client._request.return_value = {'runtime_received': True}
        self.assertEqual(await client.report_runtime({'state': 'unavailable'}), {'runtime_received': True})
