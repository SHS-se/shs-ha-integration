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
from unittest.mock import AsyncMock
import sys

ROOT = Path(__file__).parents[1] / 'custom_components/shs_energy'
sys.path.append(str(ROOT))
from refresh import refresh_in_progress
from presentation import operational_status


def coordinator_methods(namespace):
    tree = ast.parse((ROOT / 'coordinator.py').read_text())
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'ShsStatusCoordinator')
    names = {'async_restore_plan', 'async_report_runtime', 'async_replan_poll', 'async_replan_after_mode_change', 'async_answer_replan'}
    methods = [n for n in cls.body if isinstance(n, ast.AsyncFunctionDef) and n.name in names]
    exec(compile(ast.Module(body=methods, type_ignores=[]), 'coordinator.py', 'exec'), namespace)
    return type('RecoveryCoordinator', (), {name: namespace[name] for name in names})


class RecoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 9, 10, tzinfo=timezone.utc)
        namespace = dict(refresh_in_progress=refresh_in_progress, monotonic=monotonic, Any=Any, datetime=datetime, timedelta=timedelta,
            dt_util=SimpleNamespace(utcnow=lambda: self.now),
            resolved_options=lambda hass, options: options,
            OPT_PLANNING_MODE='planning_mode', PLANNING_MODE_LIVE='live',
            validate_server_contract=lambda status: None,
            ShsApiError=ValueError, ShsAuthError=PermissionError, ApiContractError=TypeError,
            _LOGGER=SimpleNamespace(debug=lambda *args: None, info=lambda *args: None))
        self.c = coordinator_methods(namespace)()
        self.c.entry = SimpleNamespace(options={'planning_mode': 'live'})
        self.c.hass = SimpleNamespace(data={})
        self.c.entry.entry_id = "entry"
        self.c.entry.runtime_data = self.c
        self.c._runtime_lock = asyncio.Lock()
        self.c._push_lock = asyncio.Lock()
        self.c._replan_lock = asyncio.Lock()
        self.c._recovering = False
        self.c._answered_replan_request_id = None
        self.c.last_optimisation_error = None
        self.c.client = SimpleNamespace(report_runtime=AsyncMock(return_value={}))
        self.c.async_update_listeners = lambda: None
        self.c.async_optimisation_push = AsyncMock()
        self.c.async_request_refresh = AsyncMock()
        self.c._report_replan_failure = AsyncMock()
        self.set_status(False)

    def set_status(self, ready):
        self.c.operational_status = {'now': self.now.isoformat(), 'plan_id': None,
            'state': 'ready' if ready else 'unavailable', 'reason': 'test',
            'binding_until': None, 'valid_until': None, 'actionable': ready, 'retry_at': None}

    async def test_mode_change_reports_wait_before_exchange_and_failure_afterwards(self):
        self.c.optimisation_plan = {'plan_id': 'retained'}
        async def failed_exchange(**kwargs):
            self.assertEqual(self.c.client.report_runtime.await_count, 1)
            self.c.last_optimisation_error = 'invalid pool configuration report'
            raise RuntimeError('exchange failed')
        self.c.async_optimisation_push.side_effect = failed_exchange
        with self.assertRaisesRegex(RuntimeError, 'exchange failed'):
            await self.c.async_replan_after_mode_change()
        reports = [call.args[0] for call in self.c.client.report_runtime.await_args_list]
        self.assertEqual([r['state'] for r in reports], ['unavailable', 'unavailable'])
        self.assertEqual(reports[-1]['last_error'], 'invalid pool configuration report')
        self.assertEqual(self.c.optimisation_plan['plan_id'], 'retained')

    async def test_mode_change_reports_replacement_ready_after_success(self):
        async def replacement(**kwargs):
            self.assertEqual(self.c.client.report_runtime.await_args.args[0]['state'], 'unavailable')
            self.set_status(True)
        self.c.async_optimisation_push.side_effect = replacement
        await self.c.async_replan_after_mode_change()
        self.c.async_optimisation_push.assert_awaited_once_with(force_plan=True)
        self.assertEqual(self.c.client.report_runtime.await_args.args[0]['state'], 'ready')

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
        self.c.client.report_runtime.side_effect = ValueError('offline')
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
        self.c.client.report_runtime.return_value = {'pending_replan_request_id': 'request'}
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

    async def test_disabled_planning_reports_request_failure_but_still_exchanges_measurements(self):
        self.c.entry.options['planning_mode'] = 'disabled'
        self.c.client.report_runtime.return_value = {'pending_replan_request_id': 'request'}
        await self.c.async_replan_poll()
        self.c._report_replan_failure.assert_awaited_once()
        self.c.async_optimisation_push.assert_awaited_once_with(force_plan=False, replan_request_id=None)

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
        tree = ast.parse((ROOT / 'api.py').read_text())
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'ShsApiClient')
        method = next(n for n in cls.body if isinstance(n, ast.AsyncFunctionDef) and n.name == 'report_runtime')
        namespace = {'Any': Any, 'API_VERSION': 1, 'ShsApiError': ValueError}
        exec(compile(ast.Module(body=[method], type_ignores=[]), 'api.py', 'exec'), namespace)
        client = SimpleNamespace(_request=AsyncMock())
        for response in ({}, {'runtime_received': False}, {'runtime_received': None}):
            client._request.return_value = response
            with self.assertRaisesRegex(ValueError, 'did not confirm'):
                await namespace['report_runtime'](client, {'state': 'unavailable'})
        client._request.return_value = {'runtime_received': True}
        self.assertEqual(await namespace['report_runtime'](client, {'state': 'unavailable'}), {'runtime_received': True})
