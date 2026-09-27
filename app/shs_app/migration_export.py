"""Cold source export, invoked by scripts/export-migration.py over an operator pipe.

No importer or target activation exists here. All captured files are private.
"""
import argparse
from contextlib import closing
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import shutil
import sys
from uuid import UUID, uuid4

from shs_core.command_journal import CommandJournal, WriterLease, entry_paths
from shs_core.verification_storage import read_verification_snapshot
from .companion import sync_directory
from .migration_check import backup, database_facts, execution_facts, readonly

STORES = ('', 'battery_live_inputs.', 'controller.', 'battery_writer.', 'verification_samples.')
RETAINED = ('battery_policy_delivery.', 'control_agreement.')
DATABASES = ('execution', 'verification', 'commands')


def private_write(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def file_digest(path):
    digest = sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def existing_file(path):
    if path.is_symlink() or not path.is_file():
        raise ValueError(f'Expected a regular source file: {path.name}')
    return path


def command_facts(path):
    with closing(readonly(path)) as db:
        if db.execute('PRAGMA user_version').fetchone()[0] != 1 or db.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
            raise ValueError('Invalid command journal')
        facts = {}
        for table, order in (('authority', 'id'), ('commands', 'ordinal')):
            digest, count = sha256(), 0
            for row in db.execute(f'SELECT * FROM {table} ORDER BY {order}'):
                digest.update(json.dumps(row, separators=(',', ':'), allow_nan=False).encode()+b'\n')
                count += 1
            facts[table] = {'rows': count, 'sha256': digest.hexdigest()}
        # Retain every unresolved command in the database; summarize without
        # loading the entire command history into the dashboard/worker heap.
        facts['outcomes'] = dict(db.execute('SELECT status,count(*) FROM commands GROUP BY status'))
        return facts


def catalog(source, entry):
    storage = source / '.storage'
    if storage.is_symlink():
        raise ValueError('Storage directory must not be a symbolic link')
    required = {f'shs_energy.{kind}.{entry}.sqlite': 'database' for kind in DATABASES}
    required.update({f'shs_energy.{prefix}{entry}': 'store' for prefix in STORES})
    retained = {f'shs_energy.{prefix}{entry}' for prefix in RETAINED}
    journal_path, lock = entry_paths(storage, entry)
    known = set(required) | retained | {lock.name}
    known |= {name + suffix for name in required if name.endswith('.sqlite') for suffix in ('-wal', '-shm')}
    unknown = [p.name for p in storage.iterdir() if p.name.startswith('shs_energy.') and entry in p.name and p.name not in known]
    if unknown:
        raise ValueError('Unclassified SHS stores need review: ' + ', '.join(sorted(unknown)))
    for name, kind in required.items():
        path = existing_file(storage / name)
        if kind == 'store':
            value = json.loads(path.read_bytes())
            if value.get('version') != 1 or value.get('key') != name or not isinstance(value.get('data'), dict):
                raise ValueError(f'Unsupported Store envelope: {name}')
    inventory = {**required, **{name: 'retained_obsolete' for name in retained if (storage/name).exists() or (storage/name).is_symlink()}}
    for name in inventory:
        existing_file(storage/name)
    return inventory


def selected_entry(source, entry):
    value = json.loads(existing_file(source/'.storage/core.config_entries').read_bytes())
    entries = [item for item in value['data']['entries'] if item['entry_id'] == entry and item['domain'] == 'shs_energy']
    if len(entries) != 1:
        raise ValueError('Expected one SHS config entry')
    return entries[0]


def source_facts(storage, inventory):
    result = {}
    for name, kind in inventory.items():
        path = existing_file(storage/name)
        if kind == 'database':
            db_kind = name.split('.')[1]
            result[name] = command_facts(path) if db_kind == 'commands' else database_facts(path, db_kind)
        else:
            result[name] = {'bytes': path.stat().st_size, 'sha256': file_digest(path)}
    return result


def export(source, entry, destination, migration_id, *, require_core_stopped):
    """The probe is supplied by an authenticated HAOS host operator, never the UI."""
    UUID(migration_id)
    source, destination = Path(source), Path(destination)
    if source.is_symlink() or source.resolve() in (destination.resolve(), *destination.resolve().parents):
        raise ValueError('Export destination must be outside the source config directory')
    # Ancestor symlinks would undermine both the lease and private output paths.
    for path in (source, destination):
        if any(p.is_symlink() for p in (path, *path.parents)):
            raise ValueError('Migration paths must not contain symbolic links')
    storage = source/'.storage'
    journal_path, lock_path = entry_paths(storage, entry)
    require_core_stopped()
    with WriterLease(lock_path) as lease:
        journal = CommandJournal(existing_file(journal_path))
        state = journal.state()
        installed = json.loads(existing_file(source/'custom_components/shs_energy/manifest.json').read_bytes())['version']
        if state['release'] != installed or not state['clean_stop']:
            raise ValueError('Install, run and cleanly stop the matching preparation integration before export')
        inventory = catalog(source, entry)
        config = selected_entry(source, entry)
        source_bytes = sum((storage/name).stat().st_size for name in inventory)
        # One full attempt, plus SQLite/WAL and verification overhead. Capacity,
        # not a control validity constraint. Existing attempts remain immutable.
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if shutil.disk_usage(destination.parent).free < 2 * source_bytes:
            raise ValueError('Insufficient disk space for a verified export attempt')
        if destination.exists() and (not destination.is_dir() or destination.stat().st_mode & 0o077):
            raise ValueError('Export directory must be private (0700)')
        before = source_facts(storage, inventory)
        execution_facts(storage/f'shs_energy.execution.{entry}.sqlite')
        revision, records, metadata = read_verification_snapshot(storage/f'shs_energy.verification.{entry}.sqlite')
        del records, metadata
        require_core_stopped()
        journal.seal(lease, migration_id)
        # The seal is the sole source mutation. Compare against its new digest.
        before[journal_path.name] = command_facts(journal_path)
        destination.mkdir(mode=0o700, exist_ok=True)
        if destination.stat().st_mode & 0o077:
            raise ValueError('Export directory must be private (0700)')
        attempt = destination/('attempt-'+uuid4().hex)
        attempt.mkdir(mode=0o700)
        files = {}
        for name, kind in inventory.items():
            target = attempt/name
            if kind == 'database':
                backup(storage/name, target)
            else:
                # Stream the large sample store without keeping another copy.
                fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with (storage/name).open('rb') as src, os.fdopen(fd, 'wb') as dst:
                    shutil.copyfileobj(src, dst)
                    dst.flush()
                    os.fsync(dst.fileno())
            files[name] = {'role': kind, 'bytes': target.stat().st_size, 'sha256': file_digest(target)}
        entry_bytes = json.dumps(config, sort_keys=True, allow_nan=False).encode()
        private_write(attempt/'entry.json', entry_bytes)
        files['entry.json'] = {'role': 'canonical_config_snapshot_with_credentials', 'bytes': len(entry_bytes), 'sha256': sha256(entry_bytes).hexdigest()}
        after = source_facts(storage, inventory)
        copied = source_facts(attempt, inventory)
        if before != after or before != copied or catalog(source, entry) != inventory or selected_entry(source, entry) != config:
            raise ValueError('Source changed during export; no manifest published')
        accounting = execution_facts(attempt/f'shs_energy.execution.{entry}.sqlite')
        read_verification_snapshot(attempt/f'shs_energy.verification.{entry}.sqlite')
        require_core_stopped()
        lease.require_held()
        result = {'schema': 1, 'kind': 'coherent_cold_export', 'migration_id': migration_id,
                  'entry_id': entry, 'source_release': installed, 'source_owner': 'fenced',
                  'stores_coherent': True, 'migration_complete': False,
                  'not_done': ['target_import', 'runtime_parity', 'app_activation'],
                  'coverage': {'gap_started_at_ms': state['stopping_at_ms'], 'clean_stop_at_ms': state['stopped_at_ms'], 'observations_spooled': False},
                  'canonical_config': 'HA remains authoritative; re-read permissions and bindings at activation',
                  'retained_in_ha': ['entity_registry', 'device_registry', 'recorder'],
                  'absent_obsolete_stores': sorted(f'shs_energy.{p}{entry}' for p in RETAINED if f'shs_energy.{p}{entry}' not in inventory),
                  'files': files, 'logical_contents': copied, 'execution': accounting,
                  'verification_revision': revision}
        private_write(attempt/'manifest.json', json.dumps(result, indent=2, allow_nan=False).encode()+b'\n')
        sync_directory(attempt)
        sync_directory(destination)
        sync_directory(destination.parent)
        return attempt


def operator_probe():
    # The SSH wrapper verifies Docker's actual Core process state. A timeout,
    # API outage or HTTP error is never interpreted as a stopped Core.
    print(json.dumps({'request': 'core_state'}), flush=True)
    response = json.loads(sys.stdin.readline())
    if response != {'core_state': 'exited'}:
        raise ValueError('Home Assistant Core is not confirmed stopped')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--entry', required=True)
    parser.add_argument('--migration', required=True)
    args = parser.parse_args()
    path = export(Path('/homeassistant'), args.entry, Path('/data/migrations')/str(UUID(args.migration)), args.migration,
                  require_core_stopped=operator_probe)
    print(json.dumps({'complete': str(path), 'migration_complete': False}), flush=True)


if __name__ == '__main__':
    main()
