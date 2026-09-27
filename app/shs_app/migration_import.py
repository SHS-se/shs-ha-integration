"""Verified, dormant import of an immutable cold export.

This worker never opens a runtime host, issues commands, edits HA configuration,
or unseals the source journal. Activation is a separate operation.
"""
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import shutil
from uuid import UUID, uuid4

from shs_core.command_journal import CommandJournal, entry_paths
from shs_core.device_ownership import decode_ownership
from shs_core.verification_storage import read_verification_snapshot
from .companion import sync_directory
from .migration_check import backup, database_facts, execution_facts, encoded
from .migration_export import (
    DATABASES, RETAINED, STORES, existing_file, file_digest, private_write,
    source_facts,
)

APP_STORES = ('', 'battery_live_inputs.', 'verification_samples.')


def regular_path(path):
    path = Path(path).absolute()
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError('Migration paths must not contain symbolic links')
    return path


def validate_export(directory, *, source_release):
    directory = regular_path(directory)
    manifest_path = existing_file(directory/'manifest.json')
    manifest_digest = file_digest(manifest_path)
    manifest = json.loads(manifest_path.read_bytes())
    if (manifest.get('schema') != 1 or manifest.get('kind') != 'coherent_cold_export'
            or manifest.get('source_owner') != 'fenced' or manifest.get('stores_coherent') is not True
            or manifest.get('migration_complete') is not False
            or manifest.get('source_release') != source_release):
        raise ValueError('A coherent export from the expected preparation release is required')
    migration = str(UUID(manifest['migration_id']))
    if migration != manifest['migration_id']:
        raise ValueError('Noncanonical migration identity')
    entry = manifest['entry_id']
    entry_paths(directory, entry)  # validates the entry before building any paths
    expected = {f'shs_energy.{kind}.{entry}.sqlite': 'database' for kind in DATABASES}
    expected.update({f'shs_energy.{prefix}{entry}': 'store' for prefix in STORES})
    optional = {f'shs_energy.{prefix}{entry}': 'retained_obsolete' for prefix in RETAINED}
    files = manifest['files']
    missing = set(optional) - files.keys()
    if manifest['absent_obsolete_stores'] != sorted(missing):
        raise ValueError('Invalid retained source inventory')
    expected.update({name: role for name, role in optional.items() if name in files})
    expected['entry.json'] = 'canonical_config_snapshot_with_credentials'
    if set(files) != set(expected) or {p.name for p in directory.iterdir()} != set(expected) | {'manifest.json'}:
        raise ValueError('Export file catalog differs from the known source stores')
    for name, role in expected.items():
        facts = files[name]
        path = existing_file(directory/name)
        if (facts.get('role') != role or type(facts.get('bytes')) is not int
                or facts['bytes'] != path.stat().st_size or facts.get('sha256') != file_digest(path)):
            raise ValueError(f'Export content mismatch: {name}')
        if role == 'store':
            body = json.loads(path.read_bytes())
            if body.get('version') != 1 or body.get('key') != name or type(body.get('data')) is not dict:
                raise ValueError(f'Unsupported Store envelope: {name}')
    decode_ownership(json.loads((directory/f'shs_energy.controller.{entry}').read_bytes())['data'])
    config = json.loads((directory/'entry.json').read_bytes())
    if config.get('entry_id') != entry or config.get('domain') != 'shs_energy':
        raise ValueError('Export configuration belongs to a different entry')
    journal, _ = entry_paths(directory, entry)
    authority = CommandJournal(journal).state()
    if (authority['owner'] != 'fenced' or authority['migration_id'] != migration
            or authority['release'] != source_release or authority['clean_stop'] != 1):
        raise ValueError('Export command journal does not prove the source fence')
    inventory = {name: role for name, role in expected.items() if name != 'entry.json'}
    if source_facts(directory, inventory) != manifest['logical_contents']:
        raise ValueError('Export logical contents differ from the manifest')
    if execution_facts(directory/f'shs_energy.execution.{entry}.sqlite') != manifest['execution']:
        raise ValueError('Export accounting differs from its canonical evidence')
    revision, records, metadata = read_verification_snapshot(directory/f'shs_energy.verification.{entry}.sqlite')
    if revision != manifest['verification_revision']:
        raise ValueError('Export verification revision differs from the manifest')
    # Detect a replacement during the potentially long canonical reopen.
    if file_digest(manifest_path) != manifest_digest:
        raise ValueError('Export manifest changed during verification')
    return manifest, manifest_digest


def _copy(source, destination):
    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with source.open('rb') as src, os.fdopen(fd, 'wb') as dst:
        shutil.copyfileobj(src, dst)
        dst.flush()
        os.fsync(dst.fileno())


def verify_import(target, proof):
    """Check only dormant imported bytes; active runtime writes invalidate this proof."""
    target = regular_path(target)
    if proof.get('schema') != 1 or proof.get('state') != 'imported' or proof.get('migration_complete') is not False:
        raise ValueError('Expected a dormant import proof')
    entry = proof['entry_id']
    entry_paths(target, entry)
    expected = {'entry.json'} | {f'stores/shs_energy.{p}{entry}' for p in APP_STORES}
    expected |= {f'stores/shs_energy.{kind}.{entry}.sqlite' for kind in ('execution', 'verification')}
    expected |= {f'gateway_seed/shs_energy.{p}{entry}' for p in ('controller.', 'battery_writer.')}
    expected.add(f'gateway_seed/shs_energy.commands.{entry}.sqlite')
    optional = {f'gateway_seed/shs_energy.{p}{entry}' for p in RETAINED}
    if set(proof['files']) - optional != expected:
        raise ValueError('Invalid imported file catalog')
    for name, facts in proof['files'].items():
        path = existing_file(target/name)
        if file_digest(path) != facts['sha256'] or path.stat().st_size != facts['bytes']:
            raise ValueError(f'Imported content changed: {name}')
    for kind in ('execution', 'verification'):
        name = f'shs_energy.{kind}.{entry}.sqlite'
        if database_facts(target/'stores'/name, kind) != proof['logical_contents'][name]:
            raise ValueError(f'Imported {kind} logical contents changed')
    if execution_facts(target/'stores'/f'shs_energy.execution.{entry}.sqlite') != proof['execution']:
        raise ValueError('Imported accounting failed canonical parity')
    revision, _, _ = read_verification_snapshot(target/'stores'/f'shs_energy.verification.{entry}.sqlite')
    if revision != proof['verification_revision']:
        raise ValueError('Imported verification revision changed')


def import_export(directory, runtime_root, *, source_release, pair):
    """Return a verified dormant directory; repeated identical imports are idempotent.

    A distinct UUID staging directory is retained after any failure. No previous
    staging attempt or source file is overwritten or deleted.
    """
    directory, runtime_root = regular_path(directory), regular_path(runtime_root)
    if runtime_root == directory or directory in runtime_root.parents or runtime_root in directory.parents:
        raise ValueError('Runtime and export directories must be separate')
    if (type(pair) is not dict or set(pair) != {'protocol', 'app_version', 'integration_version', 'core_sha256'}
            or type(pair['protocol']) is not int or pair['protocol'] < 1
            or any(type(pair[k]) is not str or not pair[k] for k in ('app_version', 'integration_version', 'core_sha256'))):
        raise ValueError('An exact target release pair is required')
    manifest, digest = validate_export(directory, source_release=source_release)
    runtime_root.mkdir(parents=True, mode=0o700, exist_ok=True)
    if runtime_root.stat().st_mode & 0o077:
        raise ValueError('Runtime directory must be private (0700)')
    with import_lease(runtime_root):
        return _import_validated(directory, runtime_root, manifest, digest, pair, source_release)


@contextmanager
def import_lease(runtime_root):
    fd = os.open(runtime_root/'.import.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('Another migration import is already running') from None
        yield
    finally:
        os.close(fd)


def _import_validated(directory, runtime_root, manifest, digest, pair, source_release):
    migration, entry = manifest['migration_id'], manifest['entry_id']
    target = runtime_root/migration
    if target.exists():
        proof = json.loads(existing_file(target/'import.json').read_bytes())
        if proof['export_sha256'] != digest or proof['pair'] != pair or proof['migration_id'] != migration:
            raise ValueError('This migration already belongs to a different export or target pair')
        verify_import(target, proof)
        return target
    required_bytes = sum(f['bytes'] for f in manifest['files'].values())
    if shutil.disk_usage(runtime_root).free < 2 * required_bytes:
        raise ValueError('Insufficient disk space for a verified import')
    staging = runtime_root/('.import-'+uuid4().hex)
    staging.mkdir(mode=0o700)
    for part in ('stores', 'gateway_seed'):
        (staging/part).mkdir(mode=0o700)
    imported = {}
    for name, facts in manifest['files'].items():
        if name == 'entry.json':
            relative = 'entry.json'
        elif name in {f'shs_energy.{prefix}{entry}' for prefix in APP_STORES} or name in {
                f'shs_energy.{kind}.{entry}.sqlite' for kind in ('execution', 'verification')}:
            relative = 'stores/'+name
        else:
            # Ownership and the sealed command journal are provenance for the new
            # HA gateway. They are never opened as app-writable runtime stores.
            relative = 'gateway_seed/'+name
        destination = staging/relative
        if facts['role'] == 'database':
            backup(directory/name, destination)
        else:
            _copy(directory/name, destination)
        imported[relative] = {'bytes': destination.stat().st_size, 'sha256': file_digest(destination)}
        if facts['role'] != 'database' and imported[relative] != {'bytes': facts['bytes'], 'sha256': facts['sha256']}:
            raise ValueError(f'Source changed during import: {name}')
    proof = {'schema': 1, 'state': 'imported', 'migration_complete': False,
             'migration_id': migration, 'entry_id': entry, 'export_sha256': digest,
             'source_release': source_release, 'pair': pair, 'files': imported,
             'logical_contents': manifest['logical_contents'], 'execution': manifest['execution'],
             'verification_revision': manifest['verification_revision'], 'coverage': manifest['coverage'],
             'not_done': ['dormant_runtime_validation', 'gateway_seed', 'physical_reconciliation', 'activation']}
    verify_import(staging, proof)
    # The gateway seed must also preserve the complete sealed command history.
    seed_inventory = {name: facts['role'] for name, facts in manifest['files'].items()
                      if 'gateway_seed/'+name in imported}
    if source_facts(staging/'gateway_seed', seed_inventory) != {name: manifest['logical_contents'][name] for name in seed_inventory}:
        raise ValueError('Gateway seed does not match the exported ownership evidence')
    if file_digest(directory/'manifest.json') != digest:
        raise ValueError('Export manifest changed during import')
    private_write(staging/'import.json', encoded(proof)+b'\n')
    for part in ('stores', 'gateway_seed'):
        sync_directory(staging/part)
    sync_directory(staging)
    # Single operator worker; refuse a competing import rather than replace it.
    if target.exists():
        raise ValueError('A competing import already created this migration')
    staging.rename(target)
    sync_directory(runtime_root)
    return target


def main():
    import argparse
    from hashlib import sha256
    from .companion import hashes
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--export', type=Path, required=True)
    parser.add_argument('--source-release', required=True)
    parser.add_argument('--runtime-root', type=Path, default=Path('/data/runtime'))
    parser.add_argument('--bundle', type=Path, default=Path('/opt/shs/companion'))
    args = parser.parse_args()
    bundle = json.loads((args.bundle/'bundle.json').read_bytes())
    application = json.loads((args.bundle/'app.json').read_bytes())
    pair = {'protocol': bundle['protocol'], 'integration_version': bundle['integration_version'],
            'app_version': application['version'],
            'core_sha256': sha256(encoded(hashes(args.bundle/'core/shs_core'))).hexdigest()}
    target = import_export(args.export, args.runtime_root, source_release=args.source_release, pair=pair)
    print(json.dumps({'imported': str(target), 'state': 'imported', 'migration_complete': False}))


if __name__ == '__main__':
    main()
