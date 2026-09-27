"""Run the actual save/reload/status boundary without installing HA."""
import ast
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

ROOT = Path(__file__).parents[1] / 'custom_components/shs_energy'
sys.path.append(str(ROOT))
from refresh import refresh_in_progress, set_reloading


def load_functions(filename, names, namespace):
    tree = ast.parse((ROOT / filename).read_text())
    nodes = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in names]
    for node in nodes:
        if node.name != '_planning_exchange':
            node.decorator_list = []
    future = ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0)
    module = ast.fix_missing_locations(ast.Module(body=[future, *nodes], type_ignores=[]))
    exec(compile(module, filename, 'exec'), namespace)
    return namespace


class CoordinatorFixture(SimpleNamespace):
    @property
    def configuration_busy(self):
        return self._recovering or self._push_lock.locked()


class RefreshTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.hass = SimpleNamespace(data={})
        self.entry = SimpleNamespace(entry_id='entry', state='loaded')
        self.coordinator = CoordinatorFixture(_recovering=False, _push_lock=asyncio.Lock(),
            options_update_requires_reload=Mock(return_value=True),
            async_report_runtime=AsyncMock(), async_optimisation_push=AsyncMock())
        self.entry.runtime_data = self.coordinator
        self.coordinator.service = SimpleNamespace(source=SimpleNamespace(refresh_configuration=AsyncMock()))
        self.hass.config_entries = SimpleNamespace(async_reload=AsyncMock(return_value=True),
            async_get_entry=lambda _: self.entry)
        self.ns = load_functions('__init__.py', {'_async_options_updated'}, {
            'set_reloading': set_reloading,
        })

    async def test_configuration_change_reaches_gateway_without_reloading_entities(self):
        set_reloading(self.hass,self.entry,True)
        await self.ns['_async_options_updated'](self.hass,self.entry)
        self.coordinator.service.source.refresh_configuration.assert_awaited_once()
        self.hass.config_entries.async_reload.assert_not_awaited()
        self.assertIs(self.entry.runtime_data,self.coordinator)
        self.assertFalse(refresh_in_progress(self.hass,self.entry))

    async def test_failed_configuration_publication_ends_progress_and_exposes_failure(self):
        set_reloading(self.hass,self.entry,True)
        self.coordinator.service.source.refresh_configuration.side_effect = OSError('receipt disk failed')
        with self.assertRaisesRegex(OSError,'receipt disk failed'):
            await self.ns['_async_options_updated'](self.hass,self.entry)
        self.assertFalse(refresh_in_progress(self.hass,self.entry))
        self.hass.config_entries.async_reload.assert_not_awaited()

    async def test_exchange_reports_busy_then_idle_even_when_it_fails(self):
        ns = load_functions('shs_core/household.py', {'_planning_exchange'}, {'asynccontextmanager': asynccontextmanager})
        states = []
        async def report():
            states.append(refresh_in_progress(self.hass, self.entry))
        self.coordinator.async_report_runtime.side_effect = report
        with self.assertRaisesRegex(RuntimeError, 'Planner unavailable'):
            async with ns['_planning_exchange'](self.coordinator):
                self.assertTrue(refresh_in_progress(self.hass, self.entry))
                raise RuntimeError('Planner unavailable')
        self.assertEqual(states, [True, False])
        self.assertFalse(refresh_in_progress(self.hass, self.entry))

class ManualReplanTests(unittest.IsolatedAsyncioTestCase):
    async def test_client_queues_a_manual_request_and_requires_confirmation(self):
        ns = load_functions('shs_core/api.py', {'request_replan'}, {'API_VERSION': 1, 'ShsApiError': ValueError})
        client = SimpleNamespace(_request=AsyncMock(return_value={'replan_request_id': 'request'}))
        self.assertEqual(await ns['request_replan'](client), 'request')
        client._request.assert_awaited_once_with('POST', 'integration-status',
            json_body={'api_version': 1, 'request_replan': True})
        client._request.return_value = {}
        with self.assertRaisesRegex(ValueError, 'did not confirm'):
            await ns['request_replan'](client)
