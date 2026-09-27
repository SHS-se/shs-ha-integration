"""Complete cold-export fixtures, including credentials and interrupted retry."""
import asyncio
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from shs_core.command_journal import CommandJournal, SourceFenced, WriterLease, entry_paths
from shs_core.execution_storage import ExecutionStorage
from shs_core.home_runtime import ExecutionSession
from shs_core.verification_storage import VerificationStorage
from shs_app.migration_export import export, file_digest, STORES


class Legacy:
    async def async_load(self):
        return None


class ExportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.source = self.root/'config'
        self.storage = self.source/'.storage'
        self.storage.mkdir(parents=True)
        component = self.source/'custom_components/shs_energy'
        component.mkdir(parents=True)
        (component/'manifest.json').write_text('{"version":"preparation"}')
        self.entry = {'entry_id':'entry','domain':'shs_energy','version':1,'data':{'token':'fixture-secret'},'options':{'mode':'controlling'}}
        (self.storage/'core.config_entries').write_text(json.dumps({'data':{'entries':[self.entry, {'entry_id':'unrelated','token':'never-copy'}]}}))
        for prefix in STORES:
            name = 'shs_energy.'+prefix+'entry'
            (self.storage/name).write_text(json.dumps({'version':1,'key':name,'data':{}}))
        self.journal_path, self.lock_path = entry_paths(self.storage,'entry')
        self.journal = CommandJournal(self.journal_path).open('preparation')
        self.journal.stopped()
        store = ExecutionStorage(self.storage/'shs_energy.execution.entry.sqlite',asyncio.to_thread)
        await store.load()
        await store.save({'schema':'battery-runtime-v4','checkpoint':None},ExecutionSession())
        verify = VerificationStorage(self.storage/'shs_energy.verification.entry.sqlite',asyncio.to_thread,Legacy(),lambda v:json.dumps(v).encode())
        await verify.async_load()
        verify.legacy_store = None
        await verify.async_save({'attempts':[],'evaluations':[],'configurations':{},'slots':{},'schema_version':5})
        self.destination=self.root/'exports'
        self.migration=str(uuid4())

    def capture(self, probe=lambda:None):
        return export(self.source,'entry',self.destination,self.migration,require_core_stopped=probe)

    async def test_complete_export_is_private_and_fences_source(self):
        path=self.capture()
        manifest=json.loads((path/'manifest.json').read_bytes())
        self.assertTrue(manifest['stores_coherent'])
        self.assertFalse(manifest['migration_complete'])
        self.assertFalse(manifest['coverage']['observations_spooled'])
        self.assertEqual(json.loads((path/'entry.json').read_bytes()),self.entry)
        for name,facts in manifest['files'].items():
            self.assertEqual(facts['sha256'],file_digest(path/name))
            self.assertEqual((path/name).stat().st_mode & 0o777,0o600)
        self.assertNotIn('fixture-secret',(path/'manifest.json').read_text())
        self.assertNotIn('never-copy',(path/'entry.json').read_text())
        before=self.journal_path.read_bytes()
        with self.assertRaises(SourceFenced):
            CommandJournal(self.journal_path).open('preparation')
        self.assertEqual(self.journal_path.read_bytes(),before)
        again=self.capture()
        self.assertNotEqual(path,again)
        self.assertEqual(manifest['logical_contents'],json.loads((again/'manifest.json').read_bytes())['logical_contents'])

    async def test_running_core_or_held_process_lease_prevents_seal(self):
        def running():
            raise ValueError('running')
        with self.assertRaisesRegex(ValueError,'running'):
            self.capture(running)
        with WriterLease(self.lock_path):
            with self.assertRaises(RuntimeError):
                self.capture()
        self.assertEqual(self.journal.state()['owner'],'integration')

    async def test_unclean_or_unknown_source_refuses_before_seal(self):
        self.journal.open('preparation')
        with self.assertRaisesRegex(ValueError,'cleanly'):
            self.capture()
        self.journal.stopped()
        (self.storage/'shs_energy.unknown.entry').write_text('{}')
        with self.assertRaisesRegex(ValueError,'Unclassified'):
            self.capture()
        self.assertEqual(self.journal.state()['owner'],'integration')

    async def test_incomplete_legacy_and_wrong_version_refuse_before_seal(self):
        path=self.storage/'shs_energy.execution.entry.sqlite'
        with closing(sqlite3.connect(path)) as db, db:
            db.execute('UPDATE head SET cleanup_pending=1')
        with self.assertRaisesRegex(ValueError,'cleanup'):
            self.capture()
        self.assertEqual(self.journal.state()['owner'],'integration')

    async def test_interrupted_export_retries_same_fence_without_overwriting(self):
        with patch('shs_app.migration_export.backup',side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                self.capture()
        self.assertEqual(self.journal.state()['owner'],'fenced')
        self.assertEqual(list(self.destination.rglob('manifest.json')),[])
        path=self.capture()
        self.assertTrue((path/'manifest.json').exists())
        self.migration=str(uuid4())
        with self.assertRaises(SourceFenced):
            self.capture()

    async def test_source_changes_or_core_restart_leave_no_manifest(self):
        calls=0
        def probe():
            nonlocal calls
            calls+=1
            if calls==3:
                raise ValueError('Core restarted')
        with self.assertRaisesRegex(ValueError,'restarted'):
            self.capture(probe)
        self.assertEqual(list(self.destination.rglob('manifest.json')),[])
        self.assertEqual(self.journal.state()['owner'],'fenced')

    async def test_source_change_during_copy_is_detected(self):
        from shs_app.migration_export import backup
        def changed(source,dest):
            backup(source,dest)
            name='shs_energy.controller.entry'
            p=self.storage/name
            body=json.loads(p.read_bytes());body['data']['changed']=True;p.write_text(json.dumps(body))
        with patch('shs_app.migration_export.backup',side_effect=changed):
            with self.assertRaisesRegex(ValueError,'Source changed'):
                self.capture()
        self.assertEqual(list(self.destination.rglob('manifest.json')),[])
