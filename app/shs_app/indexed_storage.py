"""One-way indexed operational storage, retaining exact correction provenance."""
from contextlib import closing
from dataclasses import asdict
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3

from shs_core.execution_storage import HISTORIES, read_execution_snapshot
from shs_core import plan_execution as ex
from shs_core.runtime_json import decode_value
from shs_core.home_runtime import ExecutionSession
from .checkpoint_storage import CheckpointStorage
from .indexed_evidence import (AccountEvidence,EvidenceDatabase,METER_COLUMNS,DAY,BUCKET_MS,encode,edge)

BOUNDARY_INDEX='CREATE INDEX IF NOT EXISTS meter_edge_bounds ON meter_edges(stream,bucket,right_ms,left_ms,energy,unknown)'
BLOCK_LAYOUT='CREATE TABLE meter_block_layout (id INTEGER PRIMARY KEY CHECK(id=1), milliseconds INTEGER NOT NULL)'
SCHEMA=(
 'CREATE TABLE indexed_evidence_schema (id INTEGER PRIMARY KEY CHECK(id=1), version INTEGER NOT NULL, verified INTEGER NOT NULL)',
 'CREATE TABLE meter_knots (event_id TEXT NOT NULL,stream TEXT NOT NULL,direction TEXT NOT NULL,boundary TEXT NOT NULL,epoch TEXT NOT NULL,source_at_ms INTEGER NOT NULL,total_mwh INTEGER NOT NULL,receipt INTEGER NOT NULL,physical_id TEXT,PRIMARY KEY(stream,source_at_ms)) WITHOUT ROWID',
 'CREATE TABLE meter_edges (stream TEXT NOT NULL,right_ms INTEGER NOT NULL,left_ms INTEGER NOT NULL,energy INTEGER NOT NULL,unknown INTEGER NOT NULL,bucket INTEGER,PRIMARY KEY(stream,right_ms)) WITHOUT ROWID',
 BOUNDARY_INDEX,
 'CREATE TABLE meter_blocks (stream TEXT NOT NULL,bucket INTEGER NOT NULL,energy INTEGER NOT NULL,unknown INTEGER NOT NULL,PRIMARY KEY(stream,bucket)) WITHOUT ROWID',
 BLOCK_LAYOUT,
 f'INSERT INTO meter_block_layout VALUES (1,{BUCKET_MS})',
 'CREATE TABLE meter_bindings (stream TEXT NOT NULL,boundary TEXT NOT NULL,direction TEXT NOT NULL,physical_id TEXT NOT NULL,PRIMARY KEY(stream,boundary,direction,physical_id)) WITHOUT ROWID',
 'CREATE TABLE meter_prefix (stream TEXT NOT NULL,receipt INTEGER NOT NULL,ordinal INTEGER NOT NULL,max_source INTEGER NOT NULL,PRIMARY KEY(stream,receipt)) WITHOUT ROWID',
 'CREATE INDEX meter_receipt_stream ON meters(stream,receipt,source_at_ms)',
 'CREATE INDEX measured_observation ON observations(at_ms,ordinal) WHERE measured=1',
 'CREATE INDEX reconciliation_time ON reconciliations(at_ms,ordinal)',
 'CREATE TABLE objective_versions (admission INTEGER NOT NULL,position INTEGER NOT NULL,id TEXT NOT NULL,payload TEXT NOT NULL,contract_id TEXT NOT NULL,at_ms INTEGER NOT NULL,PRIMARY KEY(admission,position)) WITHOUT ROWID',
 'CREATE INDEX objective_version_id ON objective_versions(id,admission)',
 'CREATE TABLE objective_dispositions (admission INTEGER NOT NULL,position INTEGER NOT NULL,id TEXT NOT NULL,payload TEXT NOT NULL,contract_id TEXT NOT NULL,at_ms INTEGER NOT NULL,PRIMARY KEY(admission,position)) WITHOUT ROWID',
 'CREATE INDEX objective_disposition_id ON objective_dispositions(id,admission)',
 'CREATE TABLE objective_catalog (id TEXT PRIMARY KEY,payload TEXT NOT NULL,origin_contract TEXT NOT NULL,first_admission INTEGER NOT NULL,first_position INTEGER NOT NULL,responsibility TEXT NOT NULL,closed_at INTEGER) WITHOUT ROWID',
)


def _block(db,stream,left,right,energy,unknown,sign):
    bucket=right//BUCKET_MS if left//BUCKET_MS==right//BUCKET_MS else None
    if bucket is not None:
        db.execute('INSERT INTO meter_blocks VALUES (?,?,?,?) ON CONFLICT(stream,bucket) DO UPDATE SET energy=energy+excluded.energy,unknown=unknown+excluded.unknown',
            (stream,bucket,sign*energy,sign*unknown))
    return bucket


def index_meter(db,ordinal,row):
    stream,at=row.stream,row.source_at_ms
    before=db.execute(f'SELECT {METER_COLUMNS} FROM meter_knots WHERE stream=? AND source_at_ms<? ORDER BY source_at_ms DESC LIMIT 1',(stream,at)).fetchone()
    after=db.execute(f'SELECT {METER_COLUMNS} FROM meter_knots WHERE stream=? AND source_at_ms>? ORDER BY source_at_ms LIMIT 1',(stream,at)).fetchone()
    left=ex.MeterReceipt(*before) if before else None;right=ex.MeterReceipt(*after) if after else None
    for endpoint in (at,right.source_at_ms if right else None):
        if endpoint is None:continue
        old=db.execute('SELECT left_ms,right_ms,energy,unknown FROM meter_edges WHERE stream=? AND right_ms=?',(stream,endpoint)).fetchone()
        if old:
            _block(db,stream,*old,-1)
            db.execute('DELETE FROM meter_edges WHERE stream=? AND right_ms=?',(stream,endpoint))
    db.execute('INSERT OR REPLACE INTO meter_knots VALUES (?,?,?,?,?,?,?,?,?)',tuple(getattr(row,name) for name in METER_COLUMNS.split(',')))
    for a,b in ((left,row),(row,right)):
        if a is None or b is None:continue
        start,end,energy,unknown=edge(a,b)
        bucket=_block(db,stream,start,end,energy,unknown,1)
        db.execute('INSERT INTO meter_edges VALUES (?,?,?,?,?,?)',(stream,end,start,energy,unknown,bucket))
    db.execute('INSERT OR IGNORE INTO meter_bindings VALUES (?,?,?,?)',(stream,row.boundary,row.direction,row.physical_id or ''))
    previous=db.execute('SELECT max_source FROM meter_prefix WHERE stream=? ORDER BY receipt DESC LIMIT 1',(stream,)).fetchone()
    db.execute('INSERT INTO meter_prefix VALUES (?,?,?,?)',(stream,row.receipt,ordinal,max(at,previous[0]) if previous else at))


def index_admission(db,ordinal,admission):
    contract=admission.contract
    for position,objective in enumerate(contract.objectives):
        payload=encode(asdict(objective))
        db.execute('INSERT INTO objective_versions VALUES (?,?,?,?,?,?)',(ordinal,position,objective.id,payload,contract.id,admission.at_ms))
        db.execute("INSERT INTO objective_catalog VALUES (?,?,?,?,?,'outstanding',NULL) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload",
            (objective.id,payload,contract.id,ordinal,position))
    for position,disposition in enumerate(contract.dispositions):
        db.execute('INSERT INTO objective_dispositions VALUES (?,?,?,?,?,?)',(ordinal,position,disposition.objective_id,encode(asdict(disposition)),contract.id,admission.at_ms))
        closed=admission.at_ms if disposition.outcome in ('incorporated','retired') else None
        changed=db.execute('UPDATE objective_catalog SET responsibility=?,closed_at=coalesce(closed_at,?) WHERE id=?',(disposition.outcome,closed,disposition.objective_id))
        if changed.rowcount!=1:raise ValueError('Unknown historical objective disposition')


class IndexedStorage(CheckpointStorage):
    def __init__(self,path,run,mirror):
        super().__init__(path,run,mirror)
        self.evidence=EvidenceDatabase(Path(path))

    def _history_factory(self,db,base,counts):
        self.evidence.counts={name:counts[name] for name in HISTORIES}
        return AccountEvidence(self.evidence,{name:self.evidence.rows(name,counts[name]) for name in HISTORIES}).build(base)

    def _prepare(self):
        if not self.path.exists():return
        with closing(sqlite3.connect(self.path)) as db:
            if not db.execute("SELECT name FROM sqlite_master WHERE name='head'").fetchone():return
            indexed=db.execute("SELECT name FROM sqlite_master WHERE name='indexed_evidence_schema'").fetchone()
            state=db.execute('SELECT version,verified FROM indexed_evidence_schema').fetchone() if indexed else None
            if state and state[0]!=1:raise ValueError('Unsupported indexed evidence schema')
            if state:
                # A physical query-index upgrade, without rewriting any facts,
                # execution checkpoint, activation or schema verification.
                with db:
                    db.execute('BEGIN')
                    db.execute(BOUNDARY_INDEX)
                    db.execute('DROP INDEX IF EXISTS meter_edge_bucket')
                    if not db.execute("SELECT name FROM sqlite_master WHERE name='meter_block_layout'").fetchone():
                        # Rebuild only the derived query layout, atomically with
                        # its marker. Original facts and checkpoint stay intact.
                        db.execute('UPDATE meter_edges SET bucket=CASE WHEN left_ms / ? = right_ms / ? THEN right_ms / ? ELSE NULL END', (BUCKET_MS,)*3)
                        db.execute('DELETE FROM meter_blocks')
                        db.execute('INSERT INTO meter_blocks SELECT stream,bucket,sum(energy),sum(unknown) FROM meter_edges WHERE bucket IS NOT NULL GROUP BY stream,bucket')
                        db.execute(BLOCK_LAYOUT)
                        db.execute('INSERT INTO meter_block_layout VALUES (1,?)',(BUCKET_MS,))
                    if db.execute('SELECT milliseconds FROM meter_block_layout WHERE id=1').fetchone()!=(BUCKET_MS,):
                        raise ValueError('Unsupported meter block layout')
            if state==(1,1):return
        # Backup is a consistent offline snapshot, never an alternate live owner.
        archive=self.path.with_name(self.path.name+'.pre-indexed')
        if not archive.exists():
            temporary=archive.with_suffix(archive.suffix+'.pending')
            with closing(sqlite3.connect(self.path)) as source,closing(sqlite3.connect(temporary)) as target:
                source.backup(target)
            temporary.replace(archive)
        baseline=read_execution_snapshot(self.path)
        with closing(sqlite3.connect(self.path)) as db,db:
            if state is None:
                for statement in SCHEMA:db.execute(statement)
                for ordinal,row in enumerate(baseline.session.account.meters):index_meter(db,ordinal,row)
                for ordinal,row in enumerate(baseline.session.account.admissions):index_admission(db,ordinal,row)
                db.execute('INSERT INTO indexed_evidence_schema VALUES (1,1,0)')
        with closing(sqlite3.connect(self.path)) as db:
            counts=json.loads(db.execute('SELECT counts FROM head').fetchone()[0])
            indexed=self._history_factory(db,baseline.session.account,counts)
        account=baseline.session.account
        points={r.source_at_ms for r in (account.meters[:1]+account.meters[-1:])}
        if account.observed:points.add(account.observed.at_ms)
        if account.admissions:points.add(account.admissions[-1].at_ms)
        points.update(o.deadline_ms for o in (account.contract.objectives if account.contract else ()))
        for at in sorted(points):
            if account.contract and at>=account.admissions[-1].at_ms:
                if ex.planner_feedback(indexed,at)!=ex.planner_feedback(account,at):raise ValueError('Indexed accounting differs from original planner feedback')
            for direction in ('charge','discharge','import','export'):
                start=min(points)
                if indexed.meter_index.measure(direction,start,at)!=account.meter_index.measure(direction,start,at):
                    raise ValueError('Indexed meter bounds differ from original evidence')
        with closing(sqlite3.connect(self.path)) as db,db:
            db.execute('UPDATE indexed_evidence_schema SET verified=1 WHERE id=1')

    def _retire_traces(self):
        if not self.path.exists():return
        cutoff=int(datetime.now(timezone.utc).timestamp()*1000)-3*DAY
        with closing(sqlite3.connect(self.path)) as db,db:
            if not db.execute("SELECT name FROM sqlite_master WHERE name='head'").fetchone():return
            first=db.execute('SELECT min(ordinal) FROM traces WHERE at_ms>=?',(cutoff,)).fetchone()[0]
            # Delete only an ordinal prefix, preserving contiguous trace validation.
            removed=db.execute('DELETE FROM traces'+(' WHERE ordinal<?' if first is not None else ''),
                (first,) if first is not None else ()).rowcount
            if removed:
                counts=json.loads(db.execute('SELECT counts FROM head').fetchone()[0])
                counts['traces']-=removed
                db.execute('UPDATE head SET counts=?,revision=revision+1',(json.dumps(counts),))

    async def load(self):
        await self.run(self._prepare)
        await self.run(self._retire_traces)
        result=await super().load()
        if result is None:
            await self.save(dict(schema='battery-runtime-v4',checkpoint=None,options=None,devices=[],model_sources=None,ratings=None),ExecutionSession())
            result=await super().load()
        return result

    def _additional_checkpoint(self,db,metadata,session):
        super()._additional_checkpoint(db,metadata,session)
        if not db.execute("SELECT name FROM sqlite_master WHERE name='indexed_evidence_schema'").fetchone():
            for statement in SCHEMA:db.execute(statement)
            db.execute('INSERT INTO indexed_evidence_schema VALUES (1,1,1)')
        appended,_=self._delta(session)
        for name,index in (('meters',index_meter),('admissions',index_admission)):
            for ordinal,row in enumerate(appended[name],len(getattr(self._session.account,name))):index(db,ordinal,row)

    async def _save(self,metadata,session,*,cleanup_pending):
        await super()._save(metadata,session,cleanup_pending=cleanup_pending)
        self.evidence.counts={name:len(getattr(session.account,name)) for name in HISTORIES}

    def resource_counts(self):
        return {**super().resource_counts(),**{'evidence_'+key:value for key,value in self.evidence.metrics.items()}}
