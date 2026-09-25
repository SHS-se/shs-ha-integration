"""Append execution evidence and its command checkpoint in one SQLite transaction.

The runtime owns immutable, append-only account sequences and a bounded trace
window. This store retains one last committed snapshot to find deltas, not a
second encoded history. JSON/SQL and restore validation run in the executor.
This changes persistence, not the runtime's full-history memory model.
"""
from __future__ import annotations

import asyncio
from dataclasses import fields, replace
import json
from pathlib import Path
import sqlite3
from time import perf_counter, thread_time

if __package__:
    from . import plan_execution as execution
    from .home_runtime import ExecutionSession, ExecutionTrace, MAX_EXECUTION_TRACES
    from .runtime_json import encode_value, decode_value
else:
    import plan_execution as execution
    from home_runtime import ExecutionSession, ExecutionTrace, MAX_EXECUTION_TRACES
    from runtime_json import encode_value, decode_value

HISTORIES = ('meters', 'observations', 'admissions', 'reconciliations')
TABLES = (*HISTORIES, 'traces')
SCHEMA = (
    'CREATE TABLE head (id INTEGER PRIMARY KEY CHECK(id=1), revision INTEGER NOT NULL, metadata TEXT NOT NULL, session TEXT NOT NULL, cleanup_pending INTEGER NOT NULL, counts TEXT NOT NULL)',
    'CREATE TABLE meters (ordinal INTEGER PRIMARY KEY, event_id TEXT NOT NULL UNIQUE, stream TEXT NOT NULL, direction TEXT NOT NULL, boundary TEXT NOT NULL, epoch TEXT NOT NULL, source_at_ms INTEGER NOT NULL, total_mwh INTEGER NOT NULL, receipt INTEGER NOT NULL UNIQUE, physical_id TEXT)',
    'CREATE INDEX meter_source ON meters(stream, source_at_ms, receipt)',
    'CREATE TABLE observations (ordinal INTEGER PRIMARY KEY, at_ms INTEGER NOT NULL, stored_mwh INTEGER NOT NULL, source TEXT NOT NULL, measured INTEGER NOT NULL CHECK(measured IN (0,1)))',
    'CREATE INDEX observation_time ON observations(at_ms, ordinal)',
    'CREATE TABLE admissions (ordinal INTEGER PRIMARY KEY, receipt INTEGER NOT NULL UNIQUE, at_ms INTEGER NOT NULL, contract_id TEXT NOT NULL, payload TEXT NOT NULL)',
    'CREATE INDEX admission_time ON admissions(at_ms)',
    'CREATE TABLE reconciliations (ordinal INTEGER PRIMARY KEY, at_ms INTEGER NOT NULL, payload TEXT NOT NULL)',
    'CREATE TABLE traces (ordinal INTEGER PRIMARY KEY, at_ms INTEGER NOT NULL, payload TEXT NOT NULL)',
)


def _json(value):
    return json.dumps(value, separators=(',', ':'), allow_nan=False)


def _shell(session):
    account = {field.name: encode_value(getattr(session.account, field.name))
               for field in fields(execution.Account) if field.name not in HISTORIES}
    account.update(type='Account', **{name: [] for name in HISTORIES})
    value = {field.name: encode_value(getattr(session, field.name))
             for field in fields(ExecutionSession) if field.name not in ('account', 'traces')}
    return dict(value, type='ExecutionSession', account=account, traces=[])


class ExecutionStorage:
    def __init__(self, path, run, legacy=None):
        self.path, self.run, self.legacy = Path(path), run, legacy
        self._lock = asyncio.Lock()
        self._loaded = False
        self._revision = 0
        self._session = ExecutionSession()
        self._trace_start = 0
        self._cleanup_pending = False
        self.metrics = dict(commits=0, failures=0, rows_appended=0, rows_deleted=0,
                            encoded_bytes=0, worker_cpu_ms=0., wall_ms=0., max_wall_ms=0.,
                            cleanup_failures=0, database_bytes=0)

    def resource_counts(self):
        return {**{f'storage_{name}_rows': len(self._session.traces if name == 'traces'
                         else getattr(self._session.account, name)) for name in TABLES},
                **{f'storage_{name}': value for name, value in self.metrics.items()},
                'storage_revision': self._revision,
                'storage_legacy_cleanup_pending': int(self._cleanup_pending)}

    def _connect(self):
        db = sqlite3.connect(self.path)
        try:
            db.execute('PRAGMA synchronous=FULL')
            return db
        except BaseException:
            db.close()
            raise

    async def _settle(self, operation):
        """A worker owns its transaction even when its awaiting caller cancels."""
        task = asyncio.ensure_future(self.run(operation))
        cancelled = False
        while True:
            try:
                return await asyncio.shield(task), cancelled
            except asyncio.CancelledError:
                cancelled = True
                if task.cancelled():
                    raise

    def _version(self, db):
        version = db.execute('PRAGMA user_version').fetchone()[0]
        if version == 0:
            if db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchone():
                raise ValueError('Unversioned execution database contains tables')
        elif version != 1:
            raise ValueError(f'Unsupported execution database version: {version}')
        return version

    def _read(self):
        if not self.path.exists():
            return None
        db = self._connect()
        try:
            if self._version(db) == 0:
                return None  # A first/import transaction was never committed.
            db.execute('BEGIN')
            row = db.execute('SELECT revision, metadata, session, cleanup_pending, counts FROM head WHERE id=1').fetchone()
            if row is None:
                raise ValueError('Execution database has no committed checkpoint')
            revision, metadata, shell, cleanup, counts = row
            if cleanup not in (0, 1, 2):
                raise ValueError('Invalid execution migration state')
            counts = json.loads(counts)
            for name in TABLES:
                count, first, last = db.execute(f'SELECT COUNT(*),MIN(ordinal),MAX(ordinal) FROM {name}').fetchone()
                if count != counts[name] or (count and last - first + 1 != count) or (count and name != 'traces' and first != 0):
                    raise ValueError(f'Execution {name} differs from its committed count')
            raw = json.loads(shell)
            captured = raw['captured_feedback']
            if captured is not None and not isinstance(captured, str):
                raise ValueError('Invalid captured planning request')
            base = replace(decode_value({**raw, 'captured_feedback':None}, ExecutionSession), captured_feedback=captured)
            # Decode one record at a time: never build a second full JSON tree.
            meters = tuple(execution.MeterReceipt(*row) for row in db.execute(
                'SELECT event_id,stream,direction,boundary,epoch,source_at_ms,total_mwh,receipt,physical_id FROM meters ORDER BY ordinal'))
            observations = tuple(execution.StateObservation(a, b, c, bool(d)) for a, b, c, d in db.execute(
                'SELECT at_ms,stored_mwh,source,measured FROM observations ORDER BY ordinal'))
            histories = {name: tuple(decode_value(json.loads(row[0]), kind) for row in db.execute(
                f'SELECT payload FROM {name} ORDER BY ordinal')) for name, kind in
                (('admissions', execution.Admission), ('reconciliations', execution.StateReconciliation))}
            traces = tuple(decode_value(json.loads(row[0]), ExecutionTrace) for row in db.execute(
                'SELECT payload FROM traces ORDER BY ordinal'))
            if len(traces) > MAX_EXECUTION_TRACES:
                raise ValueError('Execution database exceeds trace retention')
            trace_start = db.execute('SELECT COALESCE(MIN(ordinal),0) FROM traces').fetchone()[0]
            account = replace(base.account, meters=meters, observations=observations, **histories)
            return json.loads(metadata), replace(base, account=account, traces=traces), revision, trace_start, cleanup, self.path.stat().st_size
        finally:
            db.close()

    async def load(self):
        async with self._lock:
            imported = None
            result, cancelled = await self._settle(self._read)
            self._loaded = True
            if result is None:
                self._revision, self._session, self._trace_start = 0, ExecutionSession(), 0
                if cancelled:
                    raise asyncio.CancelledError
                imported = await self.legacy.load() if self.legacy else None
                if imported is None:
                    return None
                metadata, session = imported
                await self._save(metadata, session, cleanup_pending=1)
                # A committed import is still unverified until a real reopen.
                result, cancelled = await self._settle(self._read)
                if result is None:
                    raise ValueError('Imported execution database has no checkpoint')
            metadata, self._session, self._revision, self._trace_start, self._cleanup_pending, size = result
            self.metrics['database_bytes'] = size
            if not cancelled:
                if self._cleanup_pending == 1:
                    if imported is None:
                        if self.legacy is None:
                            raise ValueError('Execution import requires its legacy verification source')
                        imported = await self.legacy.load()
                    same, cancelled = await self._settle(lambda: imported == (metadata, self._session))
                    if not same:
                        raise ValueError('Imported execution database differs from legacy checkpoint')
                    if not cancelled:
                        await self._mark_cleanup(2)
                if not cancelled:
                    await self._cleanup()
            if cancelled:
                raise asyncio.CancelledError
            return metadata, self._session

    def _delta(self, session):
        """Only append extensions are legal; published records are immutable.

        Check the prefix by identity in the worker, so a malformed producer can
        never silently rewrite evidence. Encoding and SQL remain delta-only.
        Runtime tuple creation is still linear; removing it belongs to the
        compact Account/EvidenceReader change, not to this storage boundary.
        """
        appended = {}
        for name in HISTORIES:
            before, after = getattr(self._session.account, name), getattr(session.account, name)
            if before is after:
                appended[name] = ()
                continue
            if len(after) < len(before) or any(a is not b and a != b for a, b in zip(before, after)):
                raise ValueError(f'Execution {name} must preserve its committed prefix')
            appended[name] = after[len(before):]
        if session.account.receipt < self._session.account.receipt:
            raise ValueError('Execution receipt cannot rewind its committed prefix')
        before, after = self._session.traces, session.traces
        if len(after) > MAX_EXECUTION_TRACES:
            raise ValueError('Execution traces exceed retention')
        # Traces are bounded, and may trim at the front. Keep the remaining rows.
        dropped = next((i for i, row in enumerate(before) if after and row is after[0]), len(before))
        retained = before[dropped:]
        if len(after) < len(retained) or any(a is not b and a != b for a, b in zip(retained, after)):
            raise ValueError('Execution traces must append or trim their oldest prefix')
        appended['traces'] = after[len(retained):]
        return appended, dropped

    def _commit(self, metadata, session, cleanup_pending):
        started = thread_time()
        appended, dropped = self._delta(session)
        encoded_bytes = 0
        shell = _json(_shell(session))
        metadata_json = _json(metadata)
        encoded_bytes += len(shell.encode()) + len(metadata_json.encode())
        db = self._connect()
        try:
            db.execute('BEGIN IMMEDIATE')
            if self._version(db) == 0:
                for statement in SCHEMA:
                    db.execute(statement)
            row = db.execute('SELECT revision FROM head WHERE id=1').fetchone()
            if (row[0] if row else 0) != self._revision:
                raise ValueError('Execution database changed outside its owner')
            # Stream rows into the transaction, retaining no encoded history copy.
            for name in TABLES:
                start = self._trace_start + len(self._session.traces) if name == 'traces' else len(getattr(self._session.account, name))
                for ordinal, record in enumerate(appended[name], start):
                    if name == 'meters':
                        row = (ordinal, *(getattr(record, field.name) for field in fields(record)))
                        db.execute('INSERT INTO meters VALUES (?,?,?,?,?,?,?,?,?,?)', row)
                    elif name == 'observations':
                        db.execute('INSERT INTO observations VALUES (?,?,?,?,?)',
                                   (ordinal, record.at_ms, record.stored_mwh, record.source, int(record.measured)))
                    else:
                        payload = _json(encode_value(record))
                        encoded_bytes += len(payload.encode())
                        if name == 'admissions':
                            db.execute('INSERT INTO admissions VALUES (?,?,?,?,?)',
                                       (ordinal, record.receipt, record.at_ms, record.contract.id, payload))
                        else:
                            db.execute(f'INSERT INTO {name} VALUES (?,?,?)', (ordinal, record.at_ms, payload))
            db.execute('DELETE FROM traces WHERE ordinal < ?', (self._trace_start + dropped,))
            db.execute('INSERT OR REPLACE INTO head VALUES (1,?,?,?,?,?)',
                       (self._revision + 1, metadata_json, shell, int(cleanup_pending),
                        _json({name:len(session.traces if name=='traces' else getattr(session.account,name)) for name in TABLES})))
            db.execute('PRAGMA user_version=1')
            db.commit()
        finally:
            db.close()
        return sum(map(len, appended.values())), dropped, encoded_bytes, (thread_time() - started) * 1000, self.path.stat().st_size

    async def save(self, metadata, session):
        async with self._lock:
            if not self._loaded:
                raise RuntimeError('Load execution storage before saving')
            await self._save(metadata, session, cleanup_pending=self._cleanup_pending)

    async def _save(self, metadata, session, *, cleanup_pending):
        start = perf_counter()
        try:
            result, cancelled = await self._settle(lambda: self._commit(metadata, session, cleanup_pending))
        except BaseException:
            self.metrics['failures'] += 1
            raise
        written, dropped, size, cpu, database_bytes = result
        self._session = session
        self._trace_start += dropped
        self._revision += 1
        self._cleanup_pending = cleanup_pending
        self.metrics['commits'] += 1
        self.metrics['rows_appended'] += written
        self.metrics['rows_deleted'] += dropped
        self.metrics['encoded_bytes'] += size  # JSON bytes, excludes typed SQLite columns.
        self.metrics['worker_cpu_ms'] += cpu
        elapsed = (perf_counter() - start) * 1000
        self.metrics['wall_ms'] += elapsed
        self.metrics['max_wall_ms'] = max(self.metrics['max_wall_ms'], elapsed)
        self.metrics['database_bytes'] = database_bytes
        if cancelled:
            raise asyncio.CancelledError

    async def _mark_cleanup(self, stage):
        def mark():
            db = self._connect()
            try:
                with db:
                    cursor = db.execute('UPDATE head SET cleanup_pending=? WHERE id=1 AND revision=?', (stage, self._revision))
                    if cursor.rowcount != 1:
                        raise ValueError('Execution database changed outside its owner')
            finally:
                db.close()
        _, cancelled = await self._settle(mark)
        self._cleanup_pending = stage
        if cancelled:
            raise asyncio.CancelledError

    async def _cleanup(self):
        if self._cleanup_pending != 2 or self.legacy is None:
            return
        try:
            await self.legacy.cleanup()
            await self._mark_cleanup(0)
        except Exception:
            self.metrics['cleanup_failures'] += 1
            # The verified DB remains authoritative. Retry cleanup at next load.
