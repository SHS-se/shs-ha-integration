"""Active schema adoption never mutates provenance or admits unknown state."""
from copy import deepcopy
from pathlib import Path
import json
import os
import tempfile
import unittest
from unittest.mock import patch

from shs_core.command_journal import WriterLease, WriterActive

from shs_app.records import RecordStore
from shs_app.upgrades import open_runtime_schema


class UpgradeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.pair = dict(protocol=2, app_version='old', integration_version='old', core_sha256='a'*64)
        self.identity = dict(entry_id='entry', migration_id='migration', export_sha256='b'*64, pair=self.pair)
        self.activation = dict(state='active', identity=deepcopy(self.identity), activation_id='original')

    async def test_active_upgrade_is_idempotent_and_keeps_original_identity(self):
        original = deepcopy(self.activation)
        for version in ('new', 'newer'):
            result = await open_runtime_schema(self.root, self.identity, self.activation,
                dict(self.pair, app_version=version, core_sha256='c'*64))
            self.assertEqual(result['identity'],self.identity)
            self.assertEqual(result['protocol'],4)
        self.assertEqual(self.activation,original)

    async def test_rejects_foreign_identity_and_future_schema_without_rewriting(self):
        await open_runtime_schema(self.root,self.identity,self.activation,self.pair)
        record = RecordStore(self.root/'runtime-schema.json')
        value = await record.async_load()
        value['schema'] = 99
        await record.async_save(value)
        with self.assertRaisesRegex(ValueError,'explicit upgrade'):
            await open_runtime_schema(self.root,self.identity,self.activation,self.pair)
        self.assertEqual(await record.async_load(),value)
        with self.assertRaisesRegex(ValueError,'another migration'):
            await open_runtime_schema(self.root,dict(self.identity,entry_id='foreign'),self.activation,self.pair)

    async def test_cold_and_unfinished_old_activation_cannot_skip_release_verification(self):
        for activation in (None, dict(self.activation,state='pending')):
            with self.assertRaises(ValueError):
                await open_runtime_schema(self.root,self.identity,activation,dict(self.pair,app_version='new'))
        self.assertFalse((self.root/'runtime-schema.json').exists())


class ExistingProtocolMarkerTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = UpgradeTests.asyncSetUp

    async def seed_previous_marker(self):
        self.record = RecordStore(self.root / 'runtime-schema.json')
        self.previous = dict(schema=1, protocol=3, identity=deepcopy(self.identity))
        await self.record.async_save(self.previous)
        (self.root / 'import.json').write_text(json.dumps(self.identity))
        await RecordStore(self.root / 'activation.json').async_save(self.activation)
        # Independent synthetic byte witnesses: no replay or household data.
        for name in ('receipts.sqlite', 'checkpoint.sqlite', 'physical-settings.json'):
            (self.root / name).write_bytes(('synthetic unchanged ' + name).encode())
        self.provenance = {path.name: path.read_bytes() for path in self.root.iterdir()
                           if path.name != 'runtime-schema.json'}

    def assert_provenance_unchanged(self):
        for name, value in self.provenance.items():
            self.assertEqual((self.root / name).read_bytes(), value, name)
        self.assertEqual(self.identity['pair']['protocol'], 2)
        self.assertEqual(self.activation['identity']['pair']['protocol'], 2)

    async def test_preexisting_protocol_three_active_marker_rolls_forward_once(self):
        await self.seed_previous_marker()
        original_identity, original_activation = deepcopy(self.identity), deepcopy(self.activation)
        writes = []
        write = RecordStore._write
        def record_write(record, data):
            writes.append(record.path.name)
            return write(record, data)
        # Match the existing AppEngine.load ownership boundary: it acquires this
        # lease before reading the import/activation and opening runtime schema.
        with WriterLease(self.root / 'runtime.lock') as lease:
            lease.require_held()
            with self.assertRaises(WriterActive):
                WriterLease(self.root / 'runtime.lock')
            with patch.object(RecordStore, '_write', record_write):
                migrated = await open_runtime_schema(self.root, self.identity, self.activation,
                    dict(self.pair, app_version='new', core_sha256='c'*64))
                self.assertEqual(migrated, dict(schema=1, protocol=4, identity=self.identity))
                first_bytes = self.record.path.read_bytes()
                reopened = await open_runtime_schema(self.root, self.identity, self.activation,
                    dict(self.pair, app_version='newer', core_sha256='d'*64))
                self.assertEqual(reopened, migrated)
                self.assertEqual(self.record.path.read_bytes(), first_bytes)
            lease.require_held()
        self.assertEqual(writes, ['runtime-schema.json'])
        self.assertEqual(await self.record.async_load(), migrated)
        self.assertEqual(self.identity, original_identity)
        self.assertEqual(self.activation, original_activation)
        self.assert_provenance_unchanged()

    async def test_failed_atomic_replace_preserves_protocol_three_and_retry_completes(self):
        await self.seed_previous_marker()
        before = self.record.path.read_bytes()
        with WriterLease(self.root / 'runtime.lock'):
            with patch('shs_app.records.os.replace', side_effect=OSError('synthetic interrupted replace')):
                with self.assertRaisesRegex(OSError, 'interrupted replace'):
                    await open_runtime_schema(self.root, self.identity, self.activation, self.pair)
            self.assertEqual(self.record.path.read_bytes(), before)
            self.assertEqual(await self.record.async_load(), self.previous)
            migrated = await open_runtime_schema(self.root, self.identity, self.activation, self.pair)
            self.assertEqual(migrated['protocol'], 4)
            self.assertEqual(await self.record.async_load(), migrated)
        self.assert_provenance_unchanged()

    async def test_directory_sync_failure_after_replace_reopens_idempotently(self):
        await self.seed_previous_marker()
        fsync, calls = os.fsync, []
        def interrupted_sync(descriptor):
            calls.append(descriptor)
            if len(calls) == 2:
                raise OSError('synthetic directory sync interruption')
            fsync(descriptor)
        with WriterLease(self.root / 'runtime.lock'):
            with patch('shs_app.records.os.fsync', side_effect=interrupted_sync):
                with self.assertRaisesRegex(OSError, 'directory sync interruption'):
                    await open_runtime_schema(self.root, self.identity, self.activation, self.pair)
            self.assertEqual((await self.record.async_load())['protocol'], 4)
            installed = self.record.path.read_bytes()
            await open_runtime_schema(self.root, self.identity, self.activation, self.pair)
            self.assertEqual(self.record.path.read_bytes(), installed)
        self.assert_provenance_unchanged()

    async def test_previous_marker_requires_exact_active_identity_even_for_matching_release(self):
        await self.seed_previous_marker()
        original = self.record.path.read_bytes()
        for activation in (None, dict(self.activation, state='pending'),
                           dict(self.activation, state='unverified'),
                           dict(self.activation, identity=dict(self.identity, entry_id='foreign'))):
            with self.subTest(activation=activation), self.assertRaises(ValueError):
                await open_runtime_schema(self.root, self.identity, activation, self.pair)
            self.assertEqual(self.record.path.read_bytes(), original)
        self.assert_provenance_unchanged()

    async def test_unknown_or_foreign_previous_marker_is_never_rewritten(self):
        await self.seed_previous_marker()
        for value in (dict(self.previous, protocol=2), dict(self.previous, protocol=5),
                      dict(self.previous, schema=2), dict(self.previous, schema=True),
                      dict(self.previous, identity=dict(self.identity, migration_id='foreign')),
                      dict(self.previous, unknown='field')):
            await self.record.async_save(value)
            before = self.record.path.read_bytes()
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'explicit upgrade'):
                await open_runtime_schema(self.root, self.identity, self.activation, self.pair)
            self.assertEqual(self.record.path.read_bytes(), before)
        self.assert_provenance_unchanged()
