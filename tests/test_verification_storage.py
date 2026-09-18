"""Journal delta persistence, one-way import, ordering and transaction failures."""
import asyncio
from copy import deepcopy
from datetime import date
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.append(str(Path(__file__).parents[1] / 'custom_components' / 'shs_energy'))
from verification_storage import VerificationStorage
from verification import VerificationJournal, evaluation_record


class Legacy:
    def __init__(self, value=None):
        self.value = value
        self.loads = 0
        self.removed = False
    async def async_load(self):
        self.loads += 1
        return deepcopy(self.value)
    async def async_save(self, value): self.value = deepcopy(value)
    async def async_remove(self): self.removed = True; self.value = None
    def async_delay_save(self, callback, delay): pass


def encode(value):
    return json.dumps(value, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode()


def snapshot(count=3):
    return {'schema_version': 5,
            'attempts': [{'group_id': str(i), 'device': 'pool', 'scope': 'pool:config',
                          'slot_id': 'slot', 'count': 1, 'payload': 'x' * 2000} for i in range(count)],
            'evaluations': [], 'configurations': {'config': {'setting': 1}},
            'slots': {'slot': {'start': 'now'}}, 'lifecycle_events': [], 'discarded_attempts': 0}


class StorageTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'verification.sqlite'
        self.legacy = Legacy(snapshot())
        self.store = self.make_store()

    def make_store(self):
        return VerificationStorage(self.path, asyncio.to_thread, self.legacy, encode)

    async def test_import_reload_update_old_group_and_prune_preserve_order(self):
        original = await self.store.async_load()
        await self.store.async_save(original)
        self.assertTrue(self.legacy.removed)
        self.assertEqual(await self.make_store().async_load(), original)
        # Updating the first group must not move it to the end.
        changed = {**original, 'attempts': [{**original['attempts'][0], 'count': 2}, *original['attempts'][1:]]}
        before = self.store.metrics.copy()
        await self.store.async_save(changed)
        self.assertEqual(self.store.metrics['rows_written'] - before['rows_written'], 1)
        self.assertEqual(await self.make_store().async_load(), changed)
        retained = {**changed, 'attempts': changed['attempts'][1:] + [{'group_id': 'new', 'count': 1}],
                    'configurations': {}, 'slots': {}, 'discarded_attempts': 2}
        await self.store.async_save(retained)
        self.assertEqual(await self.make_store().async_load(), retained)
        self.assertEqual(self.legacy.loads, 1, 'SQLite is authoritative after import')

    async def test_new_decision_encoding_does_not_scale_with_retained_history(self):
        for count in (100, 1000, 4000):
            with self.subTest(count=count):
                path = Path(self.directory.name) / f'{count}.sqlite'
                store = VerificationStorage(path, asyncio.to_thread, Legacy(snapshot(count)), encode)
                original = await store.async_load()
                await store.async_save(original)
                before = store.metrics.copy()
                updated = {**original, 'attempts': [*original['attempts'], {'group_id': 'new', 'value': 42}]}
                await store.async_save(updated)
                self.assertEqual(store.metrics['rows_written'] - before['rows_written'], 1)
                self.assertLess(store.metrics['encoded_bytes'] - before['encoded_bytes'], 100)

    async def test_host_encoder_handles_attributes_and_rejects_failure_before_commit(self):
        def host_encode(value):
            return json.dumps(value, default=lambda v: v.isoformat()).encode()
        self.store.encode = host_encode
        original = await self.store.async_load()
        observed = {**original, 'attempts': [{'group_id': 'date', 'observations': {'date': date(2026, 9, 18)}}]}
        await self.store.async_save(observed)
        restored = await self.make_store().async_load()
        self.assertEqual(restored['attempts'][0]['observations']['date'], '2026-09-18')
        with self.assertRaises(AttributeError):
            await self.store.async_save({**observed, 'attempts': [{'group_id': 'unsupported', 'value': object()}]})
        self.assertEqual(await self.make_store().async_load(), restored)

    async def test_failed_transaction_keeps_previous_database_and_retries(self):
        old = await self.store.async_load()
        await self.store.async_save(old)
        updated = {**old, 'attempts': [{'group_id': 'new'}]}
        real_connect = self.store._connect
        class FailingCommit:
            def __init__(self): self.db = real_connect()
            def __getattr__(self, key): return getattr(self.db, key)
            def commit(self): raise sqlite3.OperationalError('disk full')
        with patch.object(self.store, '_connect', FailingCommit):
            with self.assertRaisesRegex(sqlite3.OperationalError, 'disk full'):
                await self.store.async_save(updated)
        self.assertEqual(await self.make_store().async_load(), old)
        self.assertEqual(self.store.metrics['failures'], 1)
        await self.store.async_save(updated)
        self.assertEqual(await self.make_store().async_load(), updated)

    async def test_cancelled_save_waits_for_commit_before_another_writer(self):
        old = await self.store.async_load()
        await self.store.async_save(old)
        updated = {**old, 'attempts': [{'group_id': 'new'}]}
        entered, release = threading.Event(), threading.Event()
        commit = self.store._commit
        def delayed(*args):
            entered.set()
            if not release.wait(5): raise RuntimeError('test worker timed out')
            return commit(*args)
        with patch.object(self.store, '_commit', delayed):
            saving = asyncio.create_task(self.store.async_save(updated))
            await asyncio.to_thread(entered.wait, 5)
            saving.cancel()
            await asyncio.sleep(0)
            self.assertFalse(saving.done())
            release.set()
            with self.assertRaises(asyncio.CancelledError): await saving
        self.assertEqual(await self.make_store().async_load(), updated)
        await self.store.async_save({**updated, 'discarded_attempts': 1})
        self.assertEqual((await self.make_store().async_load())['discarded_attempts'], 1)

    async def test_failed_import_and_corrupt_database_never_discard_legacy_or_fall_back(self):
        old = await self.store.async_load()
        with patch.object(self.store, '_commit', side_effect=OSError('disk full')):
            with self.assertRaises(OSError): await self.store.async_save(old)
        self.assertFalse(self.legacy.removed)
        self.assertEqual(await self.make_store().async_load(), old)
        self.path.write_bytes(b'corrupt database')
        loads = self.legacy.loads
        with self.assertRaises(sqlite3.DatabaseError): await self.make_store().async_load()
        self.assertEqual(self.legacy.loads, loads)

    async def test_legacy_cleanup_failure_does_not_undo_committed_decision(self):
        old = await self.store.async_load()
        async def failed(): raise OSError('cannot remove old file')
        self.legacy.async_remove = failed
        await self.store.async_save(old)
        self.assertEqual(await self.make_store().async_load(), old)
        self.assertEqual(self.store.metrics['legacy_cleanup_failures'], 1)

    async def test_all_legacy_versions_preserve_evidence_on_one_way_import(self):
        row = evaluation_record('pool', 'control_verification', {'setting': 1}, {'start': 'a'}, {}, 'test')
        seed = VerificationJournal(Legacy(), Legacy())
        await seed.append(row)
        for version in range(1, 6):
            with self.subTest(version=version):
                saved = deepcopy(seed.store.value)
                saved['schema_version'] = version
                if version < 5:
                    saved['configurations'] = deepcopy(seed.configurations)
                    for group in saved['attempts']: group.pop('group_id')
                if version == 1:
                    saved['attempts'] = [{k: v for k, v in row.items() if k != 'configuration'}]
                if version == 4:
                    saved.update(samples=[{'slot_id': 'sample-slot', 'context_id': 'ctx'}],
                                 sample_contexts={'ctx': {'sample': 1}}, discarded_samples=3)
                    saved['slots']['sample-slot'] = {'start': 'sample'}
                legacy, samples = Legacy(saved), Legacy()
                path = Path(self.directory.name) / f'v{version}.sqlite'
                store = VerificationStorage(path, asyncio.to_thread, legacy, encode)
                journal = VerificationJournal(store, samples)
                await journal.load()
                await journal.lifecycle('start', 'test', 'startup')
                before = journal.export()
                self.assertTrue(legacy.removed)
                restored = VerificationJournal(VerificationStorage(path, asyncio.to_thread, legacy, encode), samples)
                await restored.load()
                for field in ('attempts', 'evaluations', 'configurations', 'slots', 'samples',
                              'sample_contexts', 'coverage', 'lifecycle_events'):
                    self.assertEqual(restored.export()[field], before[field], field)
                if version == 4:
                    self.assertEqual(restored.discarded_samples, 3)
                    self.assertEqual(len(restored.samples), 1)

    async def test_swallowed_sample_write_failure_keeps_only_legacy_copy(self):
        self.legacy.value = {**snapshot(0), 'schema_version': 4,
            'samples': [{'slot_id': 'slot', 'context_id': 'ctx'}],
            'sample_contexts': {'ctx': {}}, 'discarded_samples': 0}
        samples = Legacy()
        async def swallowed_failure(value): pass
        samples.async_save = swallowed_failure
        journal = VerificationJournal(self.store, samples)
        with self.assertRaisesRegex(OSError, 'sample migration did not persist'):
            await journal.load()
        self.assertFalse(self.legacy.removed)
        self.assertFalse(self.path.exists())

    async def test_cancelled_failed_commit_does_not_advance_cache(self):
        old = await self.store.async_load()
        await self.store.async_save(old)
        entered, release = threading.Event(), threading.Event()
        def failed(*args):
            entered.set()
            if not release.wait(5): raise RuntimeError('test worker timed out')
            raise OSError('disk full')
        with patch.object(self.store, '_commit', failed):
            saving = asyncio.create_task(self.store.async_save({**old, 'attempts': []}))
            await asyncio.to_thread(entered.wait, 5)
            saving.cancel()
            await asyncio.sleep(0)
            self.assertFalse(saving.done())
            release.set()
            with self.assertRaisesRegex(OSError, 'disk full'): await saving
        self.assertEqual(await self.make_store().async_load(), old)
        await self.store.async_save({**old, 'discarded_attempts': 1})
        self.assertEqual((await self.make_store().async_load())['discarded_attempts'], 1)

    async def test_journal_export_group_links_and_repeated_counts_survive_reload(self):
        self.legacy.value = None
        samples = Legacy()
        journal = VerificationJournal(self.store, samples)
        await journal.load()
        await journal.lifecycle('start', 'test', 'startup')
        record = evaluation_record('pool', 'control_verification', {}, None, {}, 'test')
        record.update(outcome='verified', operations=['heat'])
        group_id = await journal.append(record)
        repeated_id = await journal.append(record)
        self.assertEqual(group_id, repeated_id)
        runtime = {**record, 'verification_group_id': group_id}
        await journal.append(runtime, runtime=True)
        before = journal.export()
        reloaded = VerificationJournal(self.make_store(), samples)
        await reloaded.load()
        after = reloaded.export()
        for field in ('attempts', 'evaluations', 'configurations', 'slots', 'coverage', 'lifecycle_events'):
            self.assertEqual(before[field], after[field], field)
        self.assertEqual(after['attempts'][0]['count'], 2)


if __name__ == '__main__':
    unittest.main()
