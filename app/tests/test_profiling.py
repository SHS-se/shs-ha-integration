"""Profiling distinguishes CPU from waits and stays bounded at every log level."""
import asyncio
from collections import deque
import json
import logging
from logging.handlers import RotatingFileHandler
from io import BytesIO
from zipfile import ZipFile
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from shs_app.logging_config import configure_logging, LEVELS, ConnectionLog
from shs_app.downloads import app_log_download
from shs_app.profiling import AppProfiler, profiled
from shs_app.indexed_evidence import EvidenceDatabase
from shs_app.sources import RemoteHistory


class LoggingTests(unittest.TestCase):
    def setUp(self):
        self.loggers = [logging.getLogger(name) for name in ('', 'aiohttp.access', 'shs_core.controller')]
        levels = [logger.level for logger in self.loggers]
        self.addCleanup(lambda: [logger.setLevel(level) for logger, level in zip(self.loggers, levels)])

    def test_levels_apply_and_dashboard_polling_is_only_verbose_at_debug(self):
        for name, level in LEVELS.items():
            configure_logging(name)
            self.assertEqual(logging.getLogger().level, level)
            self.assertEqual(logging.getLogger('aiohttp.access').isEnabledFor(logging.INFO), name == 'debug')
        for invalid in ('trace', 'DEBUG', 1, []):
            with self.assertRaisesRegex(ValueError, 'log_level must be one of'):
                configure_logging(invalid)

    def test_connection_failures_keep_tracebacks_changes_and_recovery_without_retry_flood(self):
        connection = ConnectionLog(logging.getLogger('shs_app.server'))
        with self.assertLogs('shs_app.server', level='INFO') as logs:
            for _ in range(12):
                connection.attempt()
                try:
                    raise RuntimeError('Receipt stream unavailable; OperationalError: database is locked')
                except RuntimeError as error:
                    connection.disconnected(error)
            connection.attempt()
            connection.disconnected(ConnectionError('socket closed'))
            connection.connected()
            connection.connected()
            connection.attempt()
            connection.disconnected(ConnectionError('socket closed'))
        text = '\n'.join(logs.output)
        self.assertEqual(text.count('Home Assistant connection failed'), 3)
        self.assertEqual(text.count('still disconnected'), 1)
        self.assertEqual(text.count('connection active'), 1)
        self.assertIn('Traceback', text)
        self.assertIn('OperationalError: database is locked', text)

    def test_persistent_logs_survive_reconfiguration_and_download_includes_rotations(self):
        root = logging.getLogger()
        before = set(root.handlers)
        def cleanup():
            for handler in set(root.handlers)-before:
                root.removeHandler(handler)
                handler.close()
        self.addCleanup(cleanup)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory).resolve()
            configure_logging('info', path)
            logger = logging.getLogger('shs_app.server')
            logger.error('original connection failure')
            handler = next(h for h in root.handlers if isinstance(h, RotatingFileHandler) and h.baseFilename == str(path/'shs-energy.log'))
            handler.doRollover()
            configure_logging('info', path)
            logger.error('later reconnect attempt')
            self.assertEqual(sum(isinstance(h, RotatingFileHandler) and h.baseFilename == handler.baseFilename for h in root.handlers), 1)
            with ZipFile(BytesIO(app_log_download(path))) as archive:
                self.assertIn('original connection failure', archive.read('shs-energy.log.1').decode())
                self.assertIn('later reconnect attempt', archive.read('shs-energy.log').decode())
            cleanup()


class AppProfilerTests(unittest.TestCase):
    def test_interval_rates_detachment_and_bounded_compact_projection(self):
        p = AppProfiler()
        p.started = 0
        before = dict(app_processed_receipts=0, app_receipt_backlog=0, storage_commits=0, evidence_queries=0)
        after = dict(app_processed_receipts=30, app_receipt_backlog=5, storage_commits=60, evidence_queries=900)
        with patch('shs_core.resource_profiling.monotonic', side_effect=[0, 30]):
            p.sample(dict(process_cpu_seconds=0, rss_bytes=1048576), before)
            p.operations['reduce']['calls'] = 10
            p.operations['decision']['calls'] = 6
            p.operations['reduce']['cpu_ms'] = 250
            p.sample(dict(process_cpu_seconds=15, rss_bytes=2097152), after)
        with self.assertLogs('shs_app.profiling', level='INFO') as messages:
            p.log_sample()
        output = '\n'.join(messages.output)
        self.assertIn('CPU=50.0% of one core', output)
        self.assertIn('receipts=60.0/min backlog=5', output)
        self.assertIn('checkpoint_saves=120.0/min', output)
        self.assertIn('reduce=250ms/10 calls', output)
        self.assertIn('decisions=12.0/min reducer_events=20.0/min', output)
        report = p.snapshot(after)
        self.assertEqual(report['samples'], [])
        self.assertEqual(report['sample_count'], 2)
        self.assertFalse(report['samples_included'])
        report['latest_interval']['process']['rss_bytes'] = -1
        self.assertEqual(p.samples[-1]['process']['rss_bytes'], 2097152)
        self.assertEqual(len(p.snapshot(after, include_samples=True)['samples']), 2)
        class NoTraversal(deque):
            def __iter__(self): raise AssertionError('Routine projection traversed sample history')
        p.samples = NoTraversal(p.samples, maxlen=120)
        self.assertEqual(p.snapshot(after)['samples'], [])

    def test_first_sample_has_no_invented_cpu_or_rate(self):
        p = AppProfiler()
        p.sample(dict(process_cpu_seconds=1, rss_bytes=1048576), {})
        self.assertIsNone(p.interval())
        with self.assertLogs('shs_app.profiling', level='INFO') as messages:
            p.log_sample()
        self.assertIn('baseline recorded', messages.output[0])
        self.assertNotIn('CPU=0', messages.output[0])

    def test_named_evidence_queries_include_cpu_and_failures_without_changing_results(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'evidence.sqlite'
            with sqlite3.connect(path) as db:
                db.execute('CREATE TABLE example (value INTEGER)')
                db.execute('INSERT INTO example VALUES (7)')
            evidence = EvidenceDatabase(path)
            with patch('shs_app.indexed_evidence.thread_time', side_effect=[1, 1.5]):
                result = evidence.query(lambda db: db.execute('SELECT value FROM example').fetchone(), name='meter_energy')
            self.assertEqual(result, (7,))
            self.assertEqual(evidence.metrics['meter_energy_cpu_ms'], 500)
            with self.assertRaises(sqlite3.OperationalError):
                evidence.query(lambda db: db.execute('SELECT missing FROM example'), name='meter_energy')
            self.assertEqual(evidence.metrics['meter_energy_calls'], 2)
            self.assertEqual(evidence.metrics['meter_energy_failures'], 1)
            with self.assertRaisesRegex(ValueError, 'Unknown evidence operation'):
                evidence.query(lambda _: None, name='unbounded entity name')
            self.assertNotIn('unbounded entity name', json.dumps(evidence.metrics))


class AsyncProfilerTests(unittest.IsolatedAsyncioTestCase):
    async def test_async_waits_report_wall_only_and_failures_propagate(self):
        class Work:
            profiler = AppProfiler()
            @profiled('receipt_consume')
            async def consume(self):
                await asyncio.sleep(0)
                raise OSError('disk error')
        work = Work()
        with patch('shs_core.resource_profiling.thread_time', side_effect=AssertionError('CPU across await')):
            with self.assertRaisesRegex(OSError, 'disk error'):
                await work.consume()
        values = work.profiler.operations['receipt_consume']
        self.assertEqual(values['calls'], 1)
        self.assertEqual(values['failures'], 1)
        self.assertEqual(values['cpu_ms'], 0)

    async def test_history_requests_are_measured_without_logging_payloads(self):
        p = AppProfiler()
        async def call(operation, body):
            self.assertEqual(body, {'operation':'statistics','body':{'credential':'secret'}})
            return {'rows': [1, 2]}
        history = RemoteHistory(SimpleNamespace(profiler=p, call=call))
        self.assertEqual(await history.source('statistics', {'credential':'secret'}), {'rows':[1,2]})
        self.assertEqual(p.operations['ha_statistics']['calls'], 1)
        self.assertNotIn('secret', json.dumps(p.snapshot({})))
