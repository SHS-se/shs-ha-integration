"""Import the real cold-export format; never start an execution host."""
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import test_migration_export as fixtures
from shs_app.migration_export import file_digest
from shs_app.migration_import import import_export


PAIR = {'protocol': 2, 'app_version': 'app-final', 'integration_version': 'gateway-final', 'core_sha256': 'fixture-core'}


class ImportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await fixtures.ExportTests.asyncSetUp(self)
        self.export = fixtures.ExportTests.capture(self)
        self.runtime = self.root/'runtime'

    def perform(self, **changes):
        return import_export(self.export, self.runtime, source_release='preparation', pair={**PAIR, **changes})

    async def test_import_preserves_data_and_sealed_source_without_starting_runtime(self):
        before = {p.name: file_digest(p) for p in self.export.iterdir()}
        source_before = {p.name: file_digest(p) for p in self.storage.iterdir() if p.is_file()}
        target = self.perform()
        proof = json.loads((target/'import.json').read_bytes())
        self.assertEqual(proof['state'], 'imported')
        self.assertFalse(proof['migration_complete'])
        self.assertEqual(json.loads((target/'entry.json').read_bytes()), self.entry)
        self.assertEqual({p.name: file_digest(p) for p in self.export.iterdir()}, before)
        self.assertEqual({p.name: file_digest(p) for p in self.storage.iterdir() if p.is_file()}, source_before)
        self.assertTrue((target/'gateway_seed/shs_energy.commands.entry.sqlite').exists())
        self.assertFalse((target/'stores/shs_energy.commands.entry.sqlite').exists())
        for path in target.rglob('*'):
            self.assertEqual(path.stat().st_mode & 0o777, 0o700 if path.is_dir() else 0o600)
        self.assertNotIn('fixture-secret', (target/'import.json').read_text())
        self.assertEqual(self.perform(), target)

    async def test_pair_cannot_change_on_retry_and_imported_bytes_cannot_change(self):
        target = self.perform()
        with self.assertRaisesRegex(ValueError, 'different export or target pair'):
            self.perform(app_version='other')
        (target/'stores/shs_energy.entry').write_text('{}')
        with self.assertRaisesRegex(ValueError, 'Imported content changed'):
            self.perform()

    async def test_changed_source_bytes_or_extra_files_refuse_import(self):
        path = self.export/'shs_energy.controller.entry'
        original = path.read_bytes()
        path.write_bytes(original+b' ')
        with self.assertRaisesRegex(ValueError, 'content mismatch'):
            self.perform()
        path.write_bytes(original)
        (self.export/'unexpected').write_text('x')
        with self.assertRaisesRegex(ValueError, 'catalog'):
            self.perform()
        self.assertFalse(self.runtime.exists())

    async def test_catalog_paths_and_symlinks_are_rejected(self):
        path = self.export/'shs_energy.controller.entry'
        path.unlink()
        path.symlink_to(self.storage/path.name)
        with self.assertRaisesRegex(ValueError, 'regular source file'):
            self.perform()
        self.assertFalse(self.runtime.exists())

    async def test_interruption_leaves_private_evidence_and_retry_uses_new_staging(self):
        from shs_app.migration_import import backup
        copies = 0
        def fail_later(source, target):
            nonlocal copies
            copies += 1
            if copies == 2:
                raise OSError('fixture disk full')
            backup(source, target)
        with patch('shs_app.migration_import.backup', side_effect=fail_later):
            with self.assertRaisesRegex(OSError, 'disk full'):
                self.perform()
        old = list(self.runtime.glob('.import-*'))
        self.assertEqual(len(old), 1)
        self.assertFalse((old[0]/'import.json').exists())
        before = {str(p.relative_to(old[0])): file_digest(p) for p in old[0].rglob('*') if p.is_file()}
        target = self.perform()
        self.assertTrue(target.exists())
        self.assertEqual({str(p.relative_to(old[0])): file_digest(p) for p in old[0].rglob('*') if p.is_file()}, before)

    async def test_false_fence_or_wrong_accounting_proof_is_rejected(self):
        manifest = self.export/'manifest.json'
        body = json.loads(manifest.read_bytes())
        body['execution']['receipt'] += 1
        manifest.write_text(json.dumps(body))
        with self.assertRaisesRegex(ValueError, 'canonical evidence'):
            self.perform()
        body['execution']['receipt'] -= 1
        body['source_owner'] = 'integration'
        manifest.write_text(json.dumps(body))
        with self.assertRaisesRegex(ValueError, 'coherent export'):
            self.perform()

    async def test_wrong_source_release_and_overlapping_destination_refused(self):
        with self.assertRaisesRegex(ValueError, 'preparation release'):
            import_export(self.export, self.runtime, source_release='other', pair=PAIR)
        with self.assertRaisesRegex(ValueError, 'must be separate'):
            import_export(self.export, self.export/'runtime', source_release='preparation', pair=PAIR)

    async def test_parallel_import_refuses_without_overwriting_any_attempt(self):
        from shs_app.migration_import import import_lease
        self.runtime.mkdir(mode=0o700)
        with import_lease(self.runtime):
            with self.assertRaisesRegex(ValueError, 'already running'):
                self.perform()
        self.assertEqual(list(self.runtime.glob('.import-*')), [])
        self.assertTrue(self.perform().exists())

    async def test_import_retry_rejects_a_symbolic_link_to_an_existing_target(self):
        target = self.perform()
        held = self.root/'held-import'
        target.rename(held)
        target.symlink_to(held, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'symbolic links'):
            self.perform()
