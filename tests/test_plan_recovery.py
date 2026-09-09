"""Exercise the coordinator's actual lifecycle methods at its HA boundary."""
import ast
import asyncio
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
import unittest
from unittest.mock import AsyncMock
import sys

ROOT = Path(__file__).parents[1] / 'custom_components/shs_energy'
sys.path.insert(0, str(ROOT))
from presentation import operational_status


def coordinator_methods(namespace):
    tree = ast.parse((ROOT / 'coordinator.py').read_text())
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'ShsStatusCoordinator')
    names = {'async_restore_plan', 'async_report_runtime', 'async_replan_poll'}
    methods = [n for n in cls.body if isinstance(n, ast.AsyncFunctionDef) and n.name in names]
    exec(compile(ast.Module(body=methods, type_ignores=[]), 'coordinator.py', 'exec'), namespace)
    return type('RecoveryCoordinator', (), {name: namespace[name] for name in names})


class RecoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 9, 10, tzinfo=timezone.utc)
        namespace = dict(Any=Any, datetime=datetime, timedelta=timedelta,
            dt_util=SimpleNamespace(utcnow=lambda: self.now),
            resolved_options=lambda hass, options: options,
            OPT_PLANNING_MODE='planning_mode', PLANNING_MODE_LIVE='live',
            validate_server_contract=lambda status: None,
            ShsApiError=ValueError, ShsAuthError=PermissionError, ApiContractError=TypeError,
            _LOGGER=SimpleNamespace(debug=lambda *args: None))
        self.c = coordinator_methods(namespace)()
        self.c.entry = SimpleNamespace(options={'planning_mode': 'live'})
        self.c.hass = None
        self.c._runtime_lock = asyncio.Lock()
        self.c._push_lock = asyncio.Lock()
        self.c._recovery_retry_at = None
        self.c._recovery_attempts = 0
        self.c._recovering = False
        self.c._answered_replan_request_id = None
        self.c.last_optimisation_error = None
        self.c.client = SimpleNamespace(report_runtime=AsyncMock(return_value={}))
        self.c.async_update_listeners = lambda: None
        self.c.async_optimisation_push = AsyncMock()
        self.c._report_replan_failure = AsyncMock()
        self.set_status(False)

    def set_status(self, ready):
        self.c.operational_status = {'now': self.now.isoformat(), 'plan_id': None,
            'state': 'ready' if ready else 'unavailable', 'reason': 'test',
            'binding_until': None, 'valid_until': None, 'actionable': ready, 'retry_at': None}

    async def test_missing_plan_recovers_without_website_request(self):
        async def recover(**kwargs): self.set_status(True)
        self.c.async_optimisation_push.side_effect = recover
        await self.c.async_replan_poll()
        self.c.async_optimisation_push.assert_awaited_once_with(force_plan=True, replan_request_id=None)
        reports = [c.args[0] for c in self.c.client.report_runtime.await_args_list]
        self.assertEqual([r['state'] for r in reports], ['unavailable', 'unavailable', 'ready'])
        self.assertEqual([r['recovering'] for r in reports], [False, True, False])
        self.assertIsNone(self.c._recovery_retry_at)

    async def test_failed_recovery_backs_off_and_eventually_succeeds(self):
        for delay in (1, 2, 4, 5, 5):
            await self.c.async_replan_poll()
            self.assertEqual(self.c._recovery_retry_at, self.now + timedelta(minutes=delay))
            count = self.c.async_optimisation_push.await_count
            await self.c.async_replan_poll()
            self.assertEqual(self.c.async_optimisation_push.await_count, count)
            self.now = self.c._recovery_retry_at
        self.c.async_optimisation_push.side_effect = lambda **kw: self.set_status(True)
        await self.c.async_replan_poll()
        self.assertEqual(self.c._recovery_attempts, 0)
        self.assertIsNone(self.c._recovery_retry_at)

    async def test_report_outage_does_not_prevent_recovery(self):
        self.c.client.report_runtime.side_effect = ValueError('offline')
        await self.c.async_replan_poll()
        self.c.async_optimisation_push.assert_awaited_once()
        self.assertEqual(self.c.last_runtime_error, 'offline')

    async def test_healthy_and_disabled_planning_do_not_replan(self):
        self.set_status(True)
        await self.c.async_replan_poll()
        self.set_status(False)
        self.c.entry.options['planning_mode'] = 'disabled'
        await self.c.async_replan_poll()
        self.c.async_optimisation_push.assert_not_awaited()
        self.assertEqual(self.c.client.report_runtime.await_count, 2)

    async def test_inflight_exchange_does_not_queue_duplicate_recovery(self):
        async with self.c._push_lock:
            await self.c.async_replan_poll()
        self.c.async_optimisation_push.assert_not_awaited()
        self.c._recovering = True
        await self.c.async_replan_poll()
        self.c.async_optimisation_push.assert_not_awaited()

    async def test_new_user_request_bypasses_backoff_and_is_answered_once(self):
        self.c._recovery_retry_at = self.now + timedelta(minutes=5)
        self.c.client.report_runtime.return_value = {'pending_replan_request_id': 'request'}
        await self.c.async_replan_poll()
        self.c.async_optimisation_push.assert_awaited_once_with(force_plan=True, replan_request_id='request')
        await self.c.async_replan_poll()
        self.assertEqual(self.c.async_optimisation_push.await_count, 1)

    async def test_exception_still_reports_retry_and_releases_recovery(self):
        self.c.async_optimisation_push.side_effect = RuntimeError('unexpected')
        with self.assertRaises(RuntimeError): await self.c.async_replan_poll()
        self.assertFalse(self.c._recovering)
        self.assertIsNotNone(self.c._recovery_retry_at)
        self.assertEqual(self.c.client.report_runtime.await_count, 3)

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
