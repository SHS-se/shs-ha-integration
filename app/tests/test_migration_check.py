"""Snapshot rehearsal never writes source databases or grants control."""
import asyncio
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from shs_core.execution_storage import ExecutionStorage
from shs_core.home_runtime import ExecutionSession
from shs_core.verification_storage import VerificationStorage, read_verification_snapshot
from shs_app.migration_check import backup, capture, database_facts, execution_facts


class Legacy:
    async def async_load(self):
        return None


class MigrationCheckTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.execution = self.root / 'shs_energy.execution.entry.sqlite'
        self.verification = self.root / 'shs_energy.verification.entry.sqlite'
        store = ExecutionStorage(self.execution, asyncio.to_thread)
        await store.load()
        await store.save({'schema':'battery-runtime-v4', 'checkpoint':None}, ExecutionSession())
        verify = VerificationStorage(self.verification, asyncio.to_thread, Legacy(), lambda v: json.dumps(v).encode())
        await verify.async_load()
        verify.legacy_store = None
        await verify.async_save({'attempts':[], 'evaluations':[], 'configurations':{}, 'slots':{}, 'schema_version':5})

    async def test_capture_reopens_both_stores_and_never_changes_sources(self):
        before = {p: p.read_bytes() for p in (self.execution, self.verification)}
        result = capture(self.root, 'entry', self.root/'check')
        self.assertFalse(result['stores_coherent'])
        self.assertFalse(result['migration_complete'])
        self.assertEqual(result['control_owner'], 'Home Assistant integration')
        for p, data in before.items():
            self.assertEqual(p.read_bytes(), data)
        for kind, path in (('execution',self.execution), ('verification',self.verification)):
            self.assertEqual(result['databases'][kind]['tables'], database_facts(path, kind))
        self.assertTrue((self.root/'check/report.json').is_file())
        with self.assertRaises(FileExistsError):
            capture(self.root, 'entry', self.root/'check')

    async def test_backup_includes_committed_wal_and_excludes_uncommitted_write(self):
        with closing(sqlite3.connect(self.execution)) as writer:
            writer.execute('PRAGMA journal_mode=WAL')
            writer.execute('UPDATE head SET revision=42')
            writer.commit()
            writer.execute('UPDATE head SET revision=43')
            destination = self.root/'snapshot.sqlite'
            backup(self.execution, destination)
            self.assertEqual(execution_facts(destination)['revision'], 42)
            writer.rollback()

    async def test_failure_leaves_no_success_report_and_refuses_unknown_sources(self):
        with sqlite3.connect(self.execution) as db:
            db.execute('PRAGMA user_version=99')
        with self.assertRaisesRegex(ValueError, 'schema'):
            capture(self.root, 'entry', self.root/'failed')
        self.assertFalse((self.root/'failed/report.json').exists())
        with self.assertRaises(ValueError):
            capture(self.root, '../entry', self.root/'bad')

    async def test_incomplete_legacy_import_is_preserved_and_not_called_a_migration(self):
        with sqlite3.connect(self.execution) as db:
            db.execute('UPDATE head SET cleanup_pending=1')
        before = self.execution.read_bytes()
        with self.assertRaisesRegex(ValueError, 'cleanup'):
            capture(self.root, 'entry', self.root/'pending')
        self.assertEqual(self.execution.read_bytes(), before)

    async def test_backup_interruption_does_not_publish_report(self):
        with patch('shs_app.migration_check.backup', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                capture(self.root, 'entry', self.root/'interrupted')
        self.assertFalse((self.root/'interrupted/report.json').exists())

    async def test_verification_reader_rejects_missing_and_uncommitted_sources(self):
        missing = self.root/'missing.sqlite'
        with self.assertRaises(FileNotFoundError):
            read_verification_snapshot(missing)
        self.assertFalse(missing.exists())
        with sqlite3.connect(self.verification) as db:
            db.execute('PRAGMA user_version=0')
        before = self.verification.read_bytes()
        with self.assertRaisesRegex(ValueError, 'committed'):
            read_verification_snapshot(self.verification)
        self.assertEqual(self.verification.read_bytes(), before)
