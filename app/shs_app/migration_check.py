"""One-off, read-only snapshot rehearsal. This cannot activate a controller.

Run as a short-lived worker, not in the dashboard process. The two databases
are independently consistent snapshots, never a coherent household export.
"""
import argparse
from contextlib import closing
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import resource
import sqlite3
import time

from shs_core.execution_storage import read_execution_snapshot
from shs_core.verification_storage import read_verification_snapshot
from shs_core.home_runtime_checkpoint import decode_checkpoint
from shs_core.plan_execution import feedback
from shs_core.runtime_json import record_json

TABLES = {
    'execution': {'head': 'id', 'meters': 'ordinal', 'observations': 'ordinal',
                  'admissions': 'ordinal', 'reconciliations': 'ordinal', 'traces': 'ordinal'},
    'verification': {'journal_state': 'id', 'records': 'section,key', 'metadata': 'key'},
}


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def readonly(path):
    db = sqlite3.connect(Path(path).resolve(strict=True).as_uri() + '?mode=ro', uri=True)
    db.execute('PRAGMA query_only=ON')
    return db


def backup(source, destination):
    """SQLite coordinates committed pages including WAL; no live-file copy."""
    destination = Path(destination)
    # Reserve a private new file; never replace a prior capture or source.
    descriptor = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(descriptor)
    with closing(readonly(source)) as src, closing(sqlite3.connect(destination)) as dst:
        src.backup(dst, pages=256)
    with destination.open('rb') as handle:
        os.fsync(handle.fileno())


def database_facts(path, kind):
    """Ordered logical contents, bounded to one SQL row at a time."""
    with closing(readonly(path)) as db:
        db.execute('BEGIN')
        if db.execute('PRAGMA user_version').fetchone()[0] != 1:
            raise ValueError(f'Unsupported {kind} schema')
        integrity = db.execute('PRAGMA integrity_check').fetchall()
        if integrity != [('ok',)]:
            raise ValueError(f'{kind} integrity check failed: {integrity[:3]}')
        names = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if names != set(TABLES[kind]):
            raise ValueError(f'Unexpected {kind} tables: {sorted(names)}')
        result = {}
        for table, order in TABLES[kind].items():
            digest, count = sha256(), 0
            for row in db.execute(f'SELECT * FROM {table} ORDER BY {order}'):
                # SQL text and blob values remain distinct in the fingerprint.
                values = [{'blob': value.hex()} if isinstance(value, bytes) else value for value in row]
                digest.update(encoded(values) + b'\n')
                count += 1
            result[table] = {'rows': count, 'sha256': digest.hexdigest()}
        return result


def execution_facts(path):
    snapshot = read_execution_snapshot(path)
    if snapshot.metadata.get('schema') != 'battery-runtime-v4':
        raise ValueError('Unsupported execution metadata schema')
    if snapshot.cleanup_pending:
        raise ValueError('Legacy execution cleanup must finish in HA before migration')
    checkpoint = snapshot.metadata['checkpoint']
    state = decode_checkpoint(checkpoint.encode()) if checkpoint is not None else None
    account = snapshot.session.account
    # Evidence time, not the current clock: repeated probes calculate the same result.
    at_ms = max([a.at_ms for a in account.admissions] +
                [o.at_ms for o in account.observations] + [state.last_time_ms if state else 0])
    accounting = feedback(account, at_ms)
    digest = sha256()
    for fragment in record_json(snapshot.session, encoded):
        digest.update(fragment)
    return {'revision': snapshot.revision, 'receipt': account.receipt,
            'session_sha256': digest.hexdigest(), 'accounting_at_ms': at_ms,
            'accounting_sha256': sha256(encoded(accounting)).hexdigest(),
            'runtime_revision': state.revision if state else None,
            'pending_attempts': sum(len(g.attempts) for g in state.groups) if state else 0,
            'retained_traces': len(snapshot.session.traces), 'replayed_traces': 0}


def inspect(directory):
    directory = Path(directory)
    result = {}
    for kind in TABLES:
        path = directory / f'{kind}.sqlite'
        started = time.monotonic()
        before = database_facts(path, kind)
        if kind == 'execution':
            facts = execution_facts(path)
        else:
            revision, records, metadata = read_verification_snapshot(path)
            facts = {'revision': revision, 'records': len(records), 'metadata_items': len(metadata)}
            del records, metadata
        # Prove the canonical reader left the snapshot unchanged.
        if database_facts(path, kind) != before:
            raise ValueError(f'{kind} snapshot changed during read-only verification')
        result[kind] = {'tables': before, 'facts': facts, 'bytes': path.stat().st_size,
                        'reopen_seconds': round(time.monotonic() - started, 3)}
    return result


def capture(storage, entry_id, destination):
    if not re.fullmatch(r'[A-Za-z0-9_-]+', entry_id):
        raise ValueError('Invalid entry identity')
    storage, destination = Path(storage), Path(destination)
    sources = {kind: storage / f'shs_energy.{kind}.{entry_id}.sqlite' for kind in TABLES}
    for path in sources.values():
        if path.is_symlink() or not path.is_file():
            raise ValueError(f'Expected an existing database: {path.name}')
    destination.mkdir(mode=0o700, parents=False, exist_ok=False)
    started = time.monotonic()
    for kind, source in sources.items():
        backup(source, destination / f'{kind}.sqlite')
    copied = time.monotonic()
    result = {'schema': 1, 'kind': 'read_only_rehearsal', 'entry_id': entry_id,
              'captured_at': datetime.now(timezone.utc).isoformat(),
              'control_owner': 'Home Assistant integration', 'stores_coherent': False,
              'migration_complete': False, 'databases': inspect(destination),
              'backup_seconds': round(copied - started, 3),
              'elapsed_seconds': round(time.monotonic() - started, 3),
              'peak_rss_native': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
              'peak_rss_unit': 'KiB on Linux; bytes on macOS'}
    report = destination / 'report.json'
    with report.open('x') as handle:
        json.dump(result, handle, indent=2)
        handle.write('\n')
        handle.flush()
        os.fsync(handle.fileno())
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--storage', type=Path, required=True)
    parser.add_argument('--entry', required=True)
    parser.add_argument('--destination', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(capture(args.storage, args.entry, args.destination), indent=2))


if __name__ == '__main__':
    main()
