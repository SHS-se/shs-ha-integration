"""Seed only from verified cold evidence; retain every source and failed attempt."""
from contextlib import closing
import json
import unittest
from unittest.mock import patch

import test_migration_export as fixtures
from test_migration_import import PAIR
from shs_core.command_journal import WriterActive, WriterLease
from shs_core.gateway_journal import GatewayJournal
from shs_app.gateway_seed import seed_gateway
from shs_app.migration_export import file_digest
from shs_app.migration_import import import_export


class SeedTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await fixtures.ExportTests.asyncSetUp(self)
        self.export = fixtures.ExportTests.capture(self)
        self.target = import_export(self.export, self.root/'runtime', source_release='preparation', pair=PAIR)

    def perform(self, probe=lambda: None):
        return seed_gateway(self.target, self.storage, require_core_stopped=probe)

    async def test_seed_is_inert_idempotent_and_retains_all_source_bytes(self):
        before = {p.name:file_digest(p) for p in self.storage.iterdir() if p.is_file()}
        path = self.perform()
        self.assertEqual(self.perform(), path)
        self.assertEqual({name:file_digest(self.storage/name) for name in before}, before)
        with closing(GatewayJournal(path).connect(readonly=True)) as db:
            self.assertIsNone(db.execute('SELECT activation FROM authority').fetchone()[0])
            self.assertEqual(db.execute('SELECT count(*) FROM receipts').fetchone()[0], 0)
        self.assertEqual(self.journal.state()['owner'], 'fenced')
        self.assertFalse(json.loads((self.target/'import.json').read_bytes())['migration_complete'])

    async def test_running_core_process_lease_and_changed_ownership_prevent_seed(self):
        def running():
            raise ValueError('Core is running')
        with self.assertRaisesRegex(ValueError, 'Core is running'):
            self.perform(running)
        with WriterLease(self.lock_path):
            with self.assertRaises(WriterActive):
                self.perform()
        path = self.storage/'shs_energy.controller.entry'
        path.write_text(path.read_text()+' ')
        with self.assertRaisesRegex(ValueError, 'ownership evidence'):
            self.perform()
        self.assertFalse((self.storage/'shs_energy.gateway.entry.sqlite').exists())

    async def test_logically_identical_sqlite_backup_is_accepted_but_changed_source_is_not(self):
        # Exercise logical comparison even when backup file layout differs.
        with patch('shs_app.gateway_seed.file_digest', side_effect=lambda p: str(p) if str(p).endswith('.sqlite') else file_digest(p)):
            self.perform()
        with closing(self.journal.connect()) as db, db:
            db.execute('UPDATE authority SET sealed_at_ms=sealed_at_ms+1')
        with self.assertRaisesRegex(ValueError, 'command evidence'):
            self.perform()

    async def test_partial_seed_remains_evidence_and_is_not_replaced_on_retry(self):
        path = self.storage/'shs_energy.gateway.entry.sqlite'
        path.write_bytes(b'')  # crash immediately after exclusive file creation
        with self.assertRaisesRegex(ValueError, 'Unsupported gateway'):
            self.perform()
        self.assertEqual(path.read_bytes(), b'')
        self.assertEqual(self.journal.state()['owner'], 'fenced')
