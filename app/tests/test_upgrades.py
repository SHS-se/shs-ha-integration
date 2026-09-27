"""Active schema adoption never mutates provenance or admits unknown state."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

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
            self.assertEqual(result['protocol'],3)
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
