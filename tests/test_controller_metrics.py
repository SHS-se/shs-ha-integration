"""Measurement distinguishes changed inputs, fresh reports and busy execution."""
import ast
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1] / 'custom_components' / 'shs_energy'))
from controller_metrics import ControllerMetrics


class MetricsTests(unittest.TestCase):
    def test_first_reads_fresh_reports_changed_attributes_and_context(self):
        meter = ControllerMetrics('test')
        state = SimpleNamespace(state='secret-value', attributes={'unit': 'W'}, last_reported='one')
        context = {'mode': 'control_verification', 'slot': 'first'}

        def evaluate():
            meter.begin_device('battery', context)
            meter.observe('sensor.private_name', state)
            meter.observe('sensor.private_name', SimpleNamespace(state='readback', attributes={}))
            meter.end_device()

        evaluate()
        evaluate()
        state.last_reported = 'two'
        evaluate()
        state.attributes['unit'] = 'kW'
        evaluate()
        context['slot'] = 'next'
        evaluate()
        report = meter.snapshot()
        battery = report['devices']['battery']
        self.assertEqual(battery['first_observation'], 1)
        self.assertEqual(battery['unchanged_inputs'], 2)
        self.assertEqual(battery['unchanged_inputs_new_reports'], 1)
        self.assertEqual(battery['changed_inputs'], 2)
        self.assertEqual(battery['completed'], 5)
        self.assertNotIn('secret-value', json.dumps(report))
        self.assertNotIn('private_name', json.dumps(report))
        battery['completed'] = 999
        self.assertEqual(meter.snapshot()['devices']['battery']['completed'], 5)

    def test_wall_time_and_bounded_device_cardinality(self):
        with patch('controller_metrics.perf_counter', return_value=10) as clock:
            meter = ControllerMetrics('test')
            meter.begin_device('pool', {'mode': 'controlling'})
            clock.return_value = 16
            meter.end_device()
        stats = meter.snapshot()['devices']['pool']
        self.assertEqual(stats['elapsed_ms'], 6000)
        self.assertEqual(stats['max_elapsed_ms'], 6000)
        self.assertEqual(stats['over_5_seconds'], 1)
        for i in range(200):
            meter.begin_device(f'device:private_{i}', {'mode': 'disabled'})
            meter.end_device()
        self.assertLessEqual(len(meter.devices), 129)
        self.assertLessEqual(len(meter.previous), 129)
        self.assertNotIn('private_', json.dumps(meter.snapshot()))


class ExportTests(unittest.IsolatedAsyncioTestCase):
    async def test_diagnostics_and_verification_include_session_metrics(self):
        import asyncio
        root = Path(__file__).parents[1] / 'custom_components' / 'shs_energy'
        meter = ControllerMetrics('test')
        meter.trigger('timer', 'busy')
        controller = SimpleNamespace(metrics=meter, lock=asyncio.Lock(),
                                     verification=SimpleNamespace(export=lambda: {'attempts': []}))
        entry = SimpleNamespace(runtime_data=SimpleNamespace(controller=controller,
            client=SimpleNamespace(traffic=SimpleNamespace(snapshot=lambda: {'requests': 0}))))

        def load_function(file, name, namespace):
            tree = ast.parse((root / file).read_text())
            function = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef) and node.name == name)
            function.decorator_list = []
            function.returns = None
            for argument in function.args.args:
                argument.annotation = None
            exec(compile(ast.Module(body=[function], type_ignores=[]), file, 'exec'), namespace)
            return namespace[name]

        diagnostics = load_function('diagnostics.py', 'async_get_config_entry_diagnostics',
                                    {'INTEGRATION_VERSION': 'test'})
        report = await diagnostics(None, entry)
        self.assertEqual(report['controller_metrics']['triggers']['timer']['skipped_busy'], 1)
        self.assertIn('network_traffic', report)
        results = []
        download = load_function('config_panel.py', 'websocket_download_verification', {
            '_entry_from_message': lambda *args: entry, '_entry_state': lambda entry: 'loaded',
        })
        await download(None, SimpleNamespace(send_result=lambda _, value: results.append(value)), {'id': 1, 'config_entry': 'test'})
        self.assertEqual(results[0]['attempts'], [])
        self.assertEqual(results[0]['controller_metrics']['triggers'], report['controller_metrics']['triggers'])
