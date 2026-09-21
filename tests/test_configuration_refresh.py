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


class RefreshTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.hass = SimpleNamespace(data={})
        self.entry = SimpleNamespace(entry_id='entry', state='loaded')
        self.coordinator = SimpleNamespace(_recovering=False, _push_lock=asyncio.Lock(),
            options_update_requires_reload=Mock(return_value=True),
            async_report_runtime=AsyncMock(), async_optimisation_push=AsyncMock())
        self.entry.runtime_data = self.coordinator
        self.hass.config_entries = SimpleNamespace(async_reload=AsyncMock(return_value=True),
            async_get_entry=lambda _: self.entry)
        self.ns = load_functions('__init__.py', {'_async_options_updated'}, {
            'set_reloading': set_reloading,
        })

    async def test_reload_and_replan_are_one_continuous_operation_across_coordinators(self):
        replacement = SimpleNamespace(_recovering=False, _push_lock=asyncio.Lock(),
            async_report_runtime=AsyncMock())
        async def reload(_):
            self.entry.state = 'not_loaded'
            self.assertTrue(refresh_in_progress(self.hass, self.entry))
            self.entry.runtime_data = replacement
            self.assertTrue(refresh_in_progress(self.hass, self.entry))
            self.entry.state = 'loaded'
            return True
        async def replan(**kwargs):
            self.assertTrue(refresh_in_progress(self.hass, self.entry))
        replacement.async_optimisation_push = AsyncMock(side_effect=replan)
        self.hass.config_entries.async_reload.side_effect = reload
        await self.ns['_async_options_updated'](self.hass, self.entry)
        self.assertFalse(refresh_in_progress(self.hass, self.entry))
        replacement.async_optimisation_push.assert_awaited_once_with(force_plan=True)
        self.coordinator.async_optimisation_push.assert_not_awaited()
        replacement.async_report_runtime.assert_awaited_once()

    async def test_failed_reload_and_failed_replan_always_end_progress(self):
        self.hass.config_entries.async_reload.return_value = False
        await self.ns['_async_options_updated'](self.hass, self.entry)
        self.assertFalse(refresh_in_progress(self.hass, self.entry))
        self.coordinator.async_optimisation_push.assert_not_awaited()
        self.assertIn('could not reload', self.coordinator.last_optimisation_error)
        self.hass.config_entries.async_reload.return_value = True
        self.coordinator.async_optimisation_push.side_effect = RuntimeError('Failed')
        with self.assertRaisesRegex(RuntimeError, 'Failed'):
            await self.ns['_async_options_updated'](self.hass, self.entry)
        self.assertFalse(refresh_in_progress(self.hass, self.entry))

    async def test_progress_responses_retain_clients_and_block_other_saves(self):
        names = {'websocket_get_configuration', 'websocket_get_status',
            'websocket_save_configuration', 'websocket_save_device_configuration',
            'websocket_control_permission'}
        ns = load_functions('config_panel.py', names, {
            '_entry_from_message': lambda *_: self.entry,
            '_entry_state': lambda entry: entry.state,
            'refresh_in_progress': refresh_in_progress,
        })
        set_reloading(self.hass, self.entry, True)
        self.entry.state = 'not_loaded'
        for name in names:
            with self.subTest(name=name):
                connection = SimpleNamespace(send_error=Mock(), send_result=Mock())
                await ns[name](self.hass, connection, {'id': 1, 'config_entry': 'entry'})
                if name.startswith('websocket_get'):
                    connection.send_result.assert_called_once_with(1, {'refreshing': True, 'entry_id': 'entry'})
                    connection.send_error.assert_not_called()
                else:
                    self.assertEqual(connection.send_error.call_args.args[1], 'refresh_in_progress')
        set_reloading(self.hass, self.entry, False)
        connection = SimpleNamespace(send_error=Mock(), send_result=Mock())
        await ns['websocket_get_configuration'](self.hass, connection, {'id': 1, 'config_entry': 'entry'})
        self.assertEqual(connection.send_error.call_args.args[1], 'not_loaded')

    async def test_exchange_reports_busy_then_idle_even_when_it_fails(self):
        ns = load_functions('coordinator.py', {'_planning_exchange'}, {'asynccontextmanager': asynccontextmanager})
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
