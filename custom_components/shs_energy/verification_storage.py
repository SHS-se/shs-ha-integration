"""Transactional, incremental persistence for the verification journal.

The journal owns aggregation/retention and publishes immutable JSON records.
This store owns ordering, changed-record persistence and one-way JSON import.
All SQLite and JSON encoding work runs in the supplied executor. No connection
survives an operation. Recovery can require SQLite's adjacent rollback journal;
backups must capture consistent database state, including that file if present.
"""
import asyncio
import json
from pathlib import Path
import sqlite3
from time import perf_counter, thread_time

SECTIONS = ('attempts', 'evaluations', 'configurations', 'slots')


class VerificationStorage:
    def __init__(self, path, run_blocking, legacy_store, encode):
        self.path = Path(path)
        self.run_blocking = run_blocking
        self.legacy_store = legacy_store
        # HA's encoder preserves its existing date/set/enum attribute semantics.
        # The boundary takes a JSON-to-bytes encoder; the storage stays HA-free.
        self.encode = encode
        self._lock = asyncio.Lock()
        self._loaded = False
        self._revision = 0
        self._records = {}
        self._metadata = {}
        self.metrics = {
            'commits': 0, 'failures': 0, 'rows_written': 0, 'rows_deleted': 0,
            'encoded_bytes': 0, 'prepare_cpu_ms': 0.0, 'worker_cpu_ms': 0.0, 'elapsed_ms': 0.0,
            'max_elapsed_ms': 0.0, 'legacy_cleanup_failures': 0,
            'basis': 'Session totals; encoded JSON bytes are not physical disk bytes. '
                     'CPU separates event-loop delta preparation from storage worker work; '
                     'elapsed includes executor wait and commit.',
        }

    def _connect(self):
        db = sqlite3.connect(self.path)
        try:
            db.execute('PRAGMA synchronous=FULL')
        except BaseException:
            db.close()
            raise
        return db

    async def _settle(self, operation):
        """A cancelled caller must not leave a still-running transaction unowned."""
        task = asyncio.ensure_future(self.run_blocking(operation))
        cancelled = False
        while True:
            try:
                return await asyncio.shield(task), cancelled
            except asyncio.CancelledError:
                cancelled = True
                if task.cancelled():
                    raise

    def _read(self):
        if not self.path.exists():
            return None
        db = self._connect()
        try:
            version = db.execute('PRAGMA user_version').fetchone()[0]
            if version == 0:
                return None  # Initialization transaction never committed.
            if version != 1:
                raise ValueError(f'Unsupported verification database version: {version}')
            state = db.execute('SELECT revision FROM journal_state WHERE id=1').fetchone()
            if state is None:
                raise ValueError('Verification database has no committed state')
            records = {(section, key): (json.loads(payload), ordinal)
                       for section, key, ordinal, payload in db.execute(
                           'SELECT section, key, ordinal, payload FROM records ORDER BY ordinal')}
            metadata = {key: json.loads(payload) for key, payload in
                        db.execute('SELECT key, payload FROM metadata')}
        finally:
            db.close()
        return state[0], records, metadata

    async def async_load(self):
        async with self._lock:
            result, cancelled = await self._settle(self._read)
            if result is not None:
                self._revision, self._records, self._metadata = result
                value = dict(self._metadata)
                for section in SECTIONS:
                    rows = [(key, record) for (name, key), (record, _) in self._records.items() if name == section]
                    value[section] = [record for _, record in rows] if section in SECTIONS[:2] else dict(rows)
            else:
                value = await self.legacy_store.async_load()
            self._loaded = True
            if cancelled:
                raise asyncio.CancelledError
            return value

    def _changes(self, snapshot):
        """Only changed immutable records are encoded, never retained history."""
        current, writes = {}, []
        next_ordinal = max((ordinal for _, ordinal in self._records.values()), default=-1) + 1
        for section in SECTIONS:
            values = snapshot[section]
            rows = ((row['group_id'], row) for row in values) if section in SECTIONS[:2] else values.items()
            for key, value in rows:
                identity = (section, key)
                if identity in current:
                    raise ValueError('Duplicate verification record identity')
                old = self._records.get(identity)
                ordinal = old[1] if old else next_ordinal
                if old is None:
                    next_ordinal += 1
                # Configuration/slot objects can be freshly detached yet equal.
                # Groups are replaced on every change; identity skips all history.
                if old is None or (old[0] is not value and old[0] != value):
                    writes.append((section, key, ordinal, value))
                current[identity] = (value, ordinal)
        metadata = {key: value for key, value in snapshot.items() if key not in SECTIONS}
        changed_metadata = {key: value for key, value in metadata.items()
                            if key not in self._metadata or self._metadata[key] != value}
        return current, metadata, writes, self._records.keys() - current.keys(), changed_metadata

    def _commit(self, writes, removed, metadata, removed_metadata, revision):
        started = thread_time()
        rows = [(section, key, ordinal, self.encode(value)) for section, key, ordinal, value in writes]
        meta = [(key, self.encode(value)) for key, value in metadata.items()]
        encoded_bytes = sum(len(row[-1]) for row in rows + meta)
        db = self._connect()
        try:
            db.execute('BEGIN IMMEDIATE')
            db.execute('CREATE TABLE IF NOT EXISTS journal_state (id INTEGER PRIMARY KEY, revision INTEGER NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS records (section TEXT NOT NULL, key TEXT NOT NULL, ordinal INTEGER NOT NULL, payload BLOB NOT NULL, PRIMARY KEY(section,key))')
            db.execute('CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, payload BLOB NOT NULL)')
            current = db.execute('SELECT revision FROM journal_state WHERE id=1').fetchone()
            if (current[0] if current else 0) != revision:
                raise ValueError('Verification database changed outside its owner')
            db.executemany('INSERT OR REPLACE INTO records VALUES (?,?,?,?)', rows)
            db.executemany('DELETE FROM records WHERE section=? AND key=?', removed)
            db.executemany('INSERT OR REPLACE INTO metadata VALUES (?,?)', meta)
            db.executemany('DELETE FROM metadata WHERE key=?', [(key,) for key in removed_metadata])
            db.execute('INSERT OR REPLACE INTO journal_state VALUES (1,?)', (revision + 1,))
            db.execute('PRAGMA user_version=1')
            db.commit()
        finally:
            db.close()  # Rolls back any incomplete transaction, including commit failure.
        return encoded_bytes, (thread_time() - started) * 1000

    async def async_save(self, snapshot):
        async with self._lock:
            if not self._loaded:
                raise RuntimeError('Load verification storage before saving')
            started = perf_counter()
            try:
                preparing = thread_time()
                current, metadata, writes, removed, changed_metadata = self._changes(snapshot)
                removed_metadata = self._metadata.keys() - metadata.keys()
                self.metrics['prepare_cpu_ms'] += (thread_time() - preparing) * 1000
                result, cancelled = await self._settle(lambda: self._commit(
                    writes, removed, changed_metadata, removed_metadata, self._revision))
            except BaseException:
                self.metrics['failures'] += 1
                raise
            self._records, self._metadata = current, metadata
            self._revision += 1
            elapsed = (perf_counter() - started) * 1000
            self.metrics['commits'] += 1
            self.metrics['rows_written'] += len(writes)
            self.metrics['rows_deleted'] += len(removed)
            self.metrics['encoded_bytes'] += result[0]
            self.metrics['worker_cpu_ms'] += result[1]
            self.metrics['elapsed_ms'] += elapsed
            self.metrics['max_elapsed_ms'] = max(self.metrics['max_elapsed_ms'], elapsed)
            # The database is authoritative after commit. Cleanup failure must
            # never roll back memory or cause a later read of the old JSON file.
            if self.legacy_store is not None:
                try:
                    await self.legacy_store.async_remove()
                except Exception:
                    self.metrics['legacy_cleanup_failures'] += 1
                self.legacy_store = None
            if cancelled:
                raise asyncio.CancelledError
