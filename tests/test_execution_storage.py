"""Atomic append storage, correction parity, migration and crash boundaries."""
import asyncio
from contextlib import closing, contextmanager
from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.append(str(Path(__file__).parents[1] / 'custom_components' / 'shs_energy'))
from execution_storage import ExecutionStorage
from execution_migration import LegacyExecution, canonical
from home_runtime import ExecutionSession, ExecutionTrace, MAX_EXECUTION_TRACES, retain_traces
from plan_execution import Account, MeterReceipt, StateObservation, admit_plan, balance, objective_history, record_meter, request_replan
from runtime_json import encode_value
from test_plan_execution import contract

META = dict(schema='battery-runtime-v4', checkpoint=None, options=None, devices=[], model_sources=None, ratings=None)


@contextmanager
def database(path):
    with closing(sqlite3.connect(path)) as db:
        with db:
            yield db


def session(count=100):
    receipts = tuple(MeterReceipt(str(i), 'charge', 'charge', 'battery_dc', 'meter', i * 1000, i * 100, i + 1) for i in range(count))
    account = Account(receipt=count, meters=receipts)
    account = admit_plan(account, contract(), 0, StateObservation(0, 5000000, 'soc'))
    return ExecutionSession(account=account)


def trace(at):
    return ExecutionTrace(at, 0, 0, 0, None, '{}', '[]', None, None, None)


class StorageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'execution.sqlite'
        self.store = ExecutionStorage(self.path, asyncio.to_thread)
        self.assertIsNone(await self.store.load())

    def query(self, sql):
        with database(self.path) as db:
            return db.execute(sql).fetchall()

    async def test_large_history_roundtrip_append_and_late_correction_keep_accounting(self):
        original = replace(session(5000), captured_feedback='x' * 1_100_000)
        await self.store.save(META, original)
        restored = (await ExecutionStorage(self.path, asyncio.to_thread).load())[1]
        self.assertEqual(original, restored)
        self.assertEqual(balance(original.account, 900000), balance(restored.account, 900000))
        self.assertEqual(objective_history(original.account, 900000), objective_history(restored.account, 900000))
        corrected = record_meter(original.account, event_id='late', stream='charge', direction='charge',
                                 boundary='battery_dc', epoch='meter', source_at_ms=0, total_mwh=50)
        before = self.store.metrics['rows_appended']
        await self.store.save(META, replace(original, account=corrected))
        self.assertEqual(self.store.metrics['rows_appended'] - before, 1)
        reread = (await ExecutionStorage(self.path, asyncio.to_thread).load())[1]
        self.assertEqual(reread.account, corrected)
        self.assertEqual(balance(reread.account, 900000), balance(corrected, 900000))
        self.assertEqual(len(list(self.path.parent.iterdir())), 1)

    async def test_plan_identity_reuse_allowed_by_domain_is_not_rejected_by_storage(self):
        account = Account()
        previous = None
        for generation, identity in enumerate(('plan-0', 'plan-1', 'plan-0')):
            if generation:
                account = request_replan(account)
            proposal = replace(contract(), id=identity, generation=generation,
                               previous_contract_id=previous, source_receipt=account.receipt)
            account = admit_plan(account, proposal, generation * 1000,
                                 StateObservation(generation * 1000, 5000000, 'soc'))
            await self.store.save(META, ExecutionSession(account=account))
            previous = identity
        restored = (await ExecutionStorage(self.path, asyncio.to_thread).load())[1]
        self.assertEqual(restored.account, account)

    async def test_status_update_writes_no_history_and_meter_rows_need_no_json(self):
        original = session(1000)
        await self.store.save(META, original)
        before = self.store.metrics['rows_appended']
        with patch('execution_storage.encode_value', wraps=encode_value) as encoder:
            await self.store.save(META, replace(original, status='new'))
        self.assertEqual(self.store.metrics['rows_appended'], before)
        self.assertFalse(any(isinstance(call.args[0], MeterReceipt) for call in encoder.call_args_list))

    async def test_failed_checkpoint_rolls_back_appended_evidence_and_can_retry(self):
        original = session()
        await self.store.save(META, original)
        corrected = record_meter(original.account, event_id='late', stream='charge', direction='charge',
                                 boundary='battery_dc', epoch='meter', source_at_ms=0, total_mwh=50)
        updated = replace(original, account=corrected)
        with database(self.path) as db:
            db.execute("CREATE TRIGGER fail_checkpoint BEFORE INSERT ON head BEGIN SELECT RAISE(ABORT, 'disk failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            await self.store.save({**META, 'checkpoint':'new'}, updated)
        self.assertEqual((await ExecutionStorage(self.path, asyncio.to_thread).load()), (META, original))
        with database(self.path) as db:
            db.execute('DROP TRIGGER fail_checkpoint')
        await self.store.save(META, updated)
        self.assertEqual(self.query('SELECT COUNT(*) FROM meters')[0][0], len(updated.account.meters))

    async def test_stale_owner_and_rewritten_history_are_rejected(self):
        original = session()
        await self.store.save(META, original)
        other = ExecutionStorage(self.path, asyncio.to_thread)
        await other.load()
        await self.store.save(META, replace(original, status='updated'))
        with self.assertRaisesRegex(ValueError, 'outside its owner'):
            await other.save(META, replace(other._session, status='stale'))
        account = replace(original.account, meters=(replace(original.account.meters[0], total_mwh=1), *original.account.meters[1:]))
        with self.assertRaisesRegex(ValueError, 'committed prefix'):
            await self.store.save(META, replace(original, account=account))

    async def test_trace_window_deletes_only_dropped_rows(self):
        traces = tuple(trace(i) for i in range(MAX_EXECUTION_TRACES))
        original = ExecutionSession(traces=traces)
        await self.store.save(META, original)
        updated = replace(original, traces=retain_traces((*traces, trace(MAX_EXECUTION_TRACES))))
        before = self.store.metrics['rows_appended']
        await self.store.save(META, updated)
        self.assertEqual(self.store.metrics['rows_appended'] - before, 1)
        self.assertEqual(self.store.metrics['rows_deleted'], 1024)
        self.assertEqual((await ExecutionStorage(self.path, asyncio.to_thread).load())[1], updated)

    async def test_cancellation_joins_committed_worker_before_releasing_owner(self):
        original = session()
        await self.store.save(META, original)
        started, finish = threading.Event(), threading.Event()
        original_commit = self.store._commit
        def blocked(*args):
            result = original_commit(*args)
            started.set()
            finish.wait(5)
            return result
        self.store._commit = blocked
        updated = replace(original, status='committed')
        saving = asyncio.create_task(self.store.save(META, updated))
        await asyncio.to_thread(started.wait, 5)
        saving.cancel()
        await asyncio.sleep(0)
        self.assertFalse(saving.done())
        finish.set()
        with self.assertRaises(asyncio.CancelledError):
            await saving
        self.assertEqual(self.store._session, updated)
        self.assertEqual((await ExecutionStorage(self.path, asyncio.to_thread).load())[1], updated)

    async def test_missing_row_corrupt_schema_and_partial_initialization(self):
        # Empty SQLite is the specific interrupted-initialization case.
        sqlite3.connect(self.path).close()
        self.assertIsNone(await self.store.load())
        await self.store.save(META, session())
        with database(self.path) as db:
            db.execute('DELETE FROM meters WHERE ordinal=2')
        with self.assertRaisesRegex(ValueError, 'committed count'):
            await ExecutionStorage(self.path, asyncio.to_thread).load()
        with database(self.path) as db:
            db.execute('PRAGMA user_version=99')
        with self.assertRaisesRegex(ValueError, 'Unsupported'):
            await ExecutionStorage(self.path, asyncio.to_thread).load()


class MigrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.entry = 'test-entry'
        self.original = session(5000)
        self.prefix = f'shs_energy.execution_evidence.{self.entry}.'
        self.path = self.directory / 'execution.sqlite'
        def page(value):
            key = sha256(canonical(value)).hexdigest()
            (self.directory / (self.prefix + key)).write_text(json.dumps({'version':1,'data':value}))
            return key
        # A real legacy object tree, with chunked lists above transport limits.
        def store(value):
            if isinstance(value, dict):
                return page({'kind':'object', 'fields':{key:store(item) for key,item in value.items()}})
            if isinstance(value, list):
                return page({'kind':'list', 'children':[page({'kind':'value','value':value[i:i+128]}) for i in range(0,len(value),128)]})
            return page({'kind':'value','value':value})
        root = store(encode_value(self.original))
        value = dict(META, schema='battery-runtime-v3', execution_root=root, account=None)
        self.checkpoint = self.directory / f'shs_energy.battery_runtime.{self.entry}'
        self.checkpoint.write_text(json.dumps(value))
        checkpoint = self.checkpoint
        class Store:
            async def async_load(self):
                return json.loads(checkpoint.read_text()) if checkpoint.exists() else None
        self.legacy = LegacyExecution(self.directory, self.entry, Store(), asyncio.to_thread)

    async def test_one_way_import_verifies_all_history_then_cleans_only_own_files(self):
        unrelated = self.directory / (self.prefix + 'not-a-page')
        unrelated.write_text('keep')
        other = self.directory / ('shs_energy.execution_evidence.other.' + 'a' * 64)
        other.write_text('keep')
        store = ExecutionStorage(self.path, asyncio.to_thread, self.legacy)
        self.assertEqual(await store.load(), (META, self.original))
        self.assertFalse(self.checkpoint.exists())
        self.assertTrue(unrelated.exists())
        self.assertTrue(other.exists())
        self.assertEqual(await ExecutionStorage(self.path, asyncio.to_thread, self.legacy).load(), (META, self.original))
        self.assertFalse(store._cleanup_pending)

    async def test_failed_import_keeps_legacy_then_retry_and_cleanup_failure_remains_one_way(self):
        store = ExecutionStorage(self.path, asyncio.to_thread, self.legacy)
        with patch.object(store, '_commit', side_effect=OSError('full')):
            with self.assertRaises(OSError):
                await store.load()
        self.assertTrue(self.checkpoint.exists())
        with patch.object(self.legacy, 'cleanup', side_effect=OSError('unlink')):
            self.assertEqual(await store.load(), (META, self.original))
        self.assertTrue(self.checkpoint.exists())
        self.assertTrue(store._cleanup_pending)
        # A stale legacy checkpoint must never override the committed database.
        self.checkpoint.write_text('{broken')
        reread = ExecutionStorage(self.path, asyncio.to_thread, self.legacy)
        self.assertEqual(await reread.load(), (META, self.original))
        self.assertFalse(self.checkpoint.exists())

    async def test_crash_after_import_commit_resumes_verification_before_cleanup(self):
        store = ExecutionStorage(self.path, asyncio.to_thread, self.legacy)
        read, calls = store._read, 0
        def fail_reopen():
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError('restart before verification')
            return read()
        with patch.object(store, '_read', side_effect=fail_reopen):
            with self.assertRaises(OSError):
                await store.load()
        self.assertTrue(self.checkpoint.exists())
        with database(self.path) as db:
            self.assertEqual(db.execute('SELECT cleanup_pending FROM head').fetchone()[0], 1)
        with patch.object(self.legacy, 'load', wraps=self.legacy.load) as source:
            self.assertEqual(await ExecutionStorage(self.path, asyncio.to_thread, self.legacy).load(), (META, self.original))
            source.assert_awaited_once()
        self.assertFalse(self.checkpoint.exists())

    async def test_corrupt_existing_database_never_falls_back_to_legacy(self):
        self.path.write_text('not sqlite')
        with self.assertRaises(sqlite3.DatabaseError):
            await ExecutionStorage(self.path, asyncio.to_thread, self.legacy).load()
        self.assertTrue(self.checkpoint.exists())

    async def test_missing_or_corrupt_legacy_page_fails_without_committed_database(self):
        path = next(self.directory.glob(self.prefix + '*'))
        path.write_text(json.dumps({'data':{'kind':'value','value':'changed'}}))
        with self.assertRaisesRegex(ValueError, 'corrupt'):
            await ExecutionStorage(self.path, asyncio.to_thread, self.legacy).load()
        self.assertFalse(self.path.exists())
        self.assertTrue(self.checkpoint.exists())


class RuntimeSqlTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_runtime_restart_and_database_failure_fences_commands(self):
        from test_battery_runtime import Rig
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'execution.sqlite'
            rig = Rig()
            rig.store = rig.runtime.store = ExecutionStorage(path, asyncio.to_thread)
            await rig.start()
            try:
                for _ in range(4):
                    await rig.advance()
                self.assertTrue(rig.calls)
                self.assertEqual(rig.runtime.host.state.execution, (await ExecutionStorage(path, asyncio.to_thread).load())[1])
                with database(path) as db:
                    db.execute("CREATE TRIGGER fail_checkpoint BEFORE INSERT ON head BEGIN SELECT RAISE(ABORT, 'disk full'); END")
                count = len(rig.calls)
                await rig.advance()
                self.assertIsNotNone(rig.runtime.host._fault)
                self.assertEqual(len(rig.calls), count)
            finally:
                await rig.runtime.close()
            with database(path) as db:
                db.execute('DROP TRIGGER fail_checkpoint')
            restarted = Rig()
            restarted.store = restarted.runtime.store = ExecutionStorage(path, asyncio.to_thread)
            restarted.fence_store.saved = rig.fence_store.saved
            await restarted.fence.open()
            await restarted.runtime.open()
            try:
                self.assertIsNotNone(restarted.runtime.host)
            finally:
                await restarted.runtime.close()
