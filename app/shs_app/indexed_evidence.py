"""Immutable ordinal views over operational facts, with bounded pending deltas.

Old views keep their exact prefix. The current view uses materialized meter edges
and daily sums; historical views query the indexed original facts at their prefix.
No timestamp decides whether a receipt is admitted or retained.
"""
from collections.abc import Sequence
from contextlib import closing, contextmanager
from contextvars import ContextVar
from dataclasses import asdict, fields
from hashlib import sha256
import json
import sqlite3
from time import perf_counter

from shs_core import plan_execution as ex
from shs_core.runtime_json import decode_value, encode_value

DAY=86400000
BOUNDARY_EDGES = """SELECT
    coalesce(sum(CASE WHEN left_ms>=:start AND right_ms<=:end THEN energy ELSE 0 END),0),
    coalesce(sum(energy),0),coalesce(sum(unknown),0)
    FROM meter_edges INDEXED BY meter_edge_bounds
    WHERE stream=:stream AND bucket IS :bucket AND right_ms>:start AND left_ms<:end"""
METER_COLUMNS='event_id,stream,direction,boundary,epoch,source_at_ms,total_mwh,receipt,physical_id'
KINDS={'admissions':ex.Admission,'reconciliations':ex.StateReconciliation}


def encode(value):return json.dumps(value,separators=(',',':'),allow_nan=False)


def decode_row(name,row):
    if name=='meters':return ex.MeterReceipt(*row)
    if name=='observations':return ex.StateObservation(row[0],row[1],row[2],bool(row[3]))
    return decode_value(json.loads(row[0]),KINDS[name])


def columns(name):
    return METER_COLUMNS if name=='meters' else 'at_ms,stored_mwh,source,measured' if name=='observations' else 'payload'


class EvidenceDatabase:
    def __init__(self,path):
        self.path=path
        self.reader=ContextVar("evidence_reader",default=None)
        self.boundaries=ContextVar("evidence_boundaries",default=None)
        self.counts={name:0 for name in ('meters','observations','admissions','reconciliations')}
        self.metrics=dict(queries=0,query_ms=0.,max_query_ms=0.,historical_meter_queries=0)

    @contextmanager
    def snapshot(self):
        existing=self.reader.get()
        if existing is not None:
            yield existing
            return
        with closing(sqlite3.connect(self.path.resolve().as_uri()+'?mode=ro',uri=True)) as db:
            db.execute('BEGIN')
            token=self.reader.set(db)
            boundaries=self.boundaries.set({})
            try:yield db
            finally:
                self.boundaries.reset(boundaries)
                self.reader.reset(token)

    def boundary(self,db,stream,bucket,start,end):
        # Within a bucket all edges lie wholly inside that day. Objectives with
        # a shared anchor therefore share their first partial-day sum. Cache only
        # within this SQLite snapshot; the next read sees every late correction.
        if bucket is not None:
            start=max(start,bucket*DAY)
            end=min(end,(bucket+1)*DAY)
        key=(stream,bucket,start,end)
        cache=self.boundaries.get()
        if key not in cache:
            cache[key]=db.execute(BOUNDARY_EDGES,dict(stream=stream,bucket=bucket,start=start,end=end)).fetchone()
        return cache[key]

    def query(self,operation):
        start=perf_counter()
        try:
            with self.snapshot() as db:return operation(db)
        finally:
            elapsed=(perf_counter()-start)*1000
            self.metrics['queries']+=1;self.metrics['query_ms']+=elapsed
            self.metrics['max_query_ms']=max(self.metrics['max_query_ms'],elapsed)

    def rows(self,name,count):return EvidenceRows(self,name,count,count,())


class EvidenceRows(Sequence):
    def __init__(self,database,name,count,tail_start,tail):
        self.database,self.name,self.count,self.tail_start,self.tail=database,name,count,tail_start,tail
        self.latest=None

    def __len__(self):return self.count

    def __getitem__(self,index):
        if isinstance(index,slice):
            start,stop,step=index.indices(self.count)
            return tuple(self[i] for i in range(start,stop,step))
        if index<0:index+=self.count
        if not 0<=index<self.count:raise IndexError(index)
        if index>=self.tail_start:return self.tail[index-self.tail_start]
        if index==self.count-1 and self.latest is not None:return self.latest
        row=self.database.query(lambda db:db.execute(f'SELECT {columns(self.name)} FROM {self.name} WHERE ordinal=?',(index,)).fetchone())
        if row is None:raise ValueError('Missing committed evidence ordinal')
        value=decode_row(self.name,row)
        # Ordinal prefixes are immutable. The latest admission/observation is
        # read by every checkpoint; retain one decoded row per view, not history.
        if index==self.count-1:self.latest=value
        return value

    def __iter__(self):
        for start in range(0,self.tail_start,256):
            rows=self.database.query(lambda db:db.execute(f'SELECT {columns(self.name)} FROM {self.name} WHERE ordinal>=? AND ordinal<? ORDER BY ordinal',(start,min(start+256,self.tail_start))).fetchall())
            for row in rows:yield decode_row(self.name,row)
        yield from self.tail

    def __eq__(self,other):
        if self is other:return True
        if isinstance(other,EvidenceRows) and self.database is other.database and self.name==other.name:
            if self.count!=other.count:return False
            start=min(self.tail_start,other.tail_start)
            return self[start:]==other[start:]
        return isinstance(other,Sequence) and len(self)==len(other) and all(a==b for a,b in zip(self,other))

    def append_record(self,row):
        committed=min(self.count,self.database.counts[self.name])
        return EvidenceRows(self.database,self.name,self.count+1,committed,(*self[committed:],row))

    def appended_since(self,before):
        if not isinstance(before,EvidenceRows):
            if before:raise ValueError('Cannot replace an existing account with another evidence owner')
            return self[:]
        if self.database is not before.database or self.name!=before.name or len(self)<len(before):
            raise ValueError('Evidence changed its committed owner or prefix')
        # All changes must originate in append_record; pending shared rows remain exact.
        overlap=max(before.tail_start,self.tail_start)
        if any(self[i]!=before[i] for i in range(overlap,len(before))):
            raise ValueError('Evidence rewrote a pending prefix')
        return self[len(before):]

    def at_or_before(self,at_ms):
        pending=next((r for r in reversed(self.tail) if r.at_ms<=at_ms),None)
        if pending:return pending
        row=self.database.query(lambda db:db.execute(f'SELECT {columns(self.name)} FROM {self.name} WHERE at_ms<=? AND ordinal<? ORDER BY ordinal DESC LIMIT 1',(at_ms,self.tail_start)).fetchone())
        return decode_row(self.name,row) if row else None


class EvidenceLookup:
    def __init__(self,rows,kind):self.rows,self.kind=rows,kind
    def get(self,key,default=None):
        rows=self.rows
        if self.kind=='event':
            pending=next((r for r in reversed(rows.tail) if r.event_id==key),None)
            predicate='event_id=?'
        else:
            pending=next((r for r in reversed(rows.tail) if r.at_ms==key and (self.kind!='measured' or r.measured)),None)
            predicate='at_ms=?'+(' AND measured=1' if self.kind=='measured' else '')
        if pending:return pending
        row=rows.database.query(lambda db:db.execute(f'SELECT {columns(rows.name)} FROM {rows.name} WHERE {predicate} AND ordinal<? ORDER BY ordinal DESC LIMIT 1',(key,rows.tail_start)).fetchone())
        return decode_row(rows.name,row) if row else default


def edge(a,b):
    valid=a.epoch==b.epoch and b.total_mwh>=a.total_mwh
    return a.source_at_ms,b.source_at_ms,b.total_mwh-a.total_mwh if valid else 0,int(not valid)


def contribution(value,start,end):
    left,right,energy,unknown=value
    if right<=start or left>=end:return 0,0,0
    return (energy if left>=start and right<=end else 0),energy,unknown


class IndexedMeters(ex.MeterIndex):
    def __init__(self,rows):self.rows=rows

    def _prefix(self,db):
        current=json.loads(db.execute('SELECT counts FROM head').fetchone()[0])['meters']
        return current,self.rows[current:] if current<len(self.rows) else ()

    def _neighbours(self,db,stream,at,pending=(),*,successor=False):
        current,_=self._prefix(db)
        if len(self.rows)>=current:
            select=f'SELECT {METER_COLUMNS} FROM meter_knots WHERE stream=?'
            parameters=(stream,at)
            same=db.execute(select+' AND source_at_ms=?',parameters).fetchone()
            other=db.execute(select+(' AND source_at_ms>? ORDER BY source_at_ms LIMIT 1' if successor else ' AND source_at_ms<? ORDER BY source_at_ms DESC LIMIT 1'),parameters).fetchone()
        else:
            select=f'SELECT {METER_COLUMNS} FROM meters WHERE stream=? AND ordinal<?'
            parameters=(stream,len(self.rows),at)
            same=db.execute(select+' AND source_at_ms=? ORDER BY receipt DESC LIMIT 1',parameters).fetchone()
            other=db.execute(select+(' AND source_at_ms>? ORDER BY source_at_ms,receipt DESC LIMIT 1' if successor else ' AND source_at_ms<? ORDER BY source_at_ms DESC,receipt DESC LIMIT 1'),parameters).fetchone()
        same=ex.MeterReceipt(*same) if same else None;other=ex.MeterReceipt(*other) if other else None
        for row in pending:
            if row.stream!=stream:continue
            if row.source_at_ms==at:same=row
            elif ((row.source_at_ms>at and (other is None or row.source_at_ms<=other.source_at_ms)) if successor else
                  (row.source_at_ms<at and (other is None or row.source_at_ms>=other.source_at_ms))):other=row
        return same,other

    def neighbours(self,stream,at_ms):
        return self.rows.database.query(lambda db:self._neighbours(db,stream,at_ms,self._prefix(db)[1]))

    def _historical(self,db,stream,start,end):
        self.rows.database.metrics['historical_meter_queries']+=1
        query='''WITH ranked AS (SELECT *,row_number() OVER (PARTITION BY source_at_ms ORDER BY receipt DESC) AS rn FROM meters WHERE stream=? AND ordinal<?),
        knots AS (SELECT source_at_ms AS t,total_mwh AS v,epoch AS e FROM ranked WHERE rn=1),
        edges AS (SELECT t,v,e,lag(t) OVER (ORDER BY t) AS pt,lag(v) OVER (ORDER BY t) AS pv,lag(e) OVER (ORDER BY t) AS pe FROM knots)
        SELECT pt,t,CASE WHEN e=pe AND v>=pv THEN v-pv ELSE 0 END,CASE WHEN e=pe AND v>=pv THEN 0 ELSE 1 END FROM edges WHERE pt<? AND t>?'''
        low=high=unknown=0
        for row in db.execute(query,(stream,len(self.rows),end,start)):
            a,b,c=contribution(row,start,end);low+=a;high+=b;unknown+=c
        first,last=db.execute('SELECT min(source_at_ms),max(source_at_ms) FROM meters WHERE stream=? AND ordinal<?',(stream,len(self.rows))).fetchone()
        return low,high,unknown,first,last

    def measure(self,direction,start,end):
        if end<start:raise ValueError('reversed accounting interval')
        if end==start:return ex.Bounds(0,0)
        def measure(db):
            current,pending=self._prefix(db)
            streams={r[0] for r in db.execute('SELECT DISTINCT stream FROM meter_bindings WHERE direction=?',(direction,))}
            if len(self.rows)<current:
                streams={r[0] for r in db.execute('SELECT DISTINCT stream FROM meters WHERE direction=? AND ordinal<?',(direction,len(self.rows)))}
            streams.update(r.stream for r in pending if r.direction==direction)
            if not streams:return ex.Bounds(0,None)
            result=ex.Bounds(0,0)
            for stream in streams:
                if len(self.rows)<current:
                    low,high,unknown,first,last=self._historical(db,stream,start,end)
                else:
                    # Whole daily blocks plus the two boundary days and cross-day edges.
                    first_bucket=(start+DAY-1)//DAY;last_bucket=end//DAY
                    low,unknown=db.execute('SELECT coalesce(sum(energy),0),coalesce(sum(unknown),0) FROM meter_blocks WHERE stream=? AND bucket>=? AND bucket<?',(stream,first_bucket,last_bucket)).fetchone()
                    high=low
                    for bucket in (None,*{n for n in (start//DAY,end//DAY) if not first_bucket<=n<last_bucket}):
                        a,b,c=self.rows.database.boundary(db,stream,bucket,start,end)
                        low+=a;high+=b;unknown+=c
                    first_row=db.execute('SELECT source_at_ms FROM meter_knots WHERE stream=? ORDER BY source_at_ms LIMIT 1',(stream,)).fetchone()
                    last_row=db.execute('SELECT source_at_ms FROM meter_knots WHERE stream=? ORDER BY source_at_ms DESC LIMIT 1',(stream,)).fetchone()
                    first=first_row[0] if first_row else None;last=last_row[0] if last_row else None
                    changed={r.source_at_ms for r in pending if r.stream==stream}
                    ends=set(changed)
                    for at in changed:
                        _,next_row=self._neighbours(db,stream,at,(),successor=True)
                        if next_row:ends.add(next_row.source_at_ms)
                    for at in ends:
                        old=db.execute('SELECT left_ms,right_ms,energy,unknown FROM meter_edges WHERE stream=? AND right_ms=?',(stream,at)).fetchone()
                        if old:
                            a,b,c=contribution(old,start,end);low-=a;high-=b;unknown-=c
                        right,left=self._neighbours(db,stream,at,pending)
                        if right and left:
                            a,b,c=contribution(edge(left,right),start,end);low+=a;high+=b;unknown+=c
                    for row in pending:
                        if row.stream==stream:
                            first=min(first,row.source_at_ms) if first is not None else row.source_at_ms
                            last=max(last,row.source_at_ms) if last is not None else row.source_at_ms
                result=result.plus(ex.Bounds(low,None if unknown or first is None or start<first or end>last else high))
            return result
        return self.rows.database.query(measure)


class AccountEvidence:
    def __init__(self,database,rows):self.database,self.rows=database,rows

    def build(self,base):
        account=object.__new__(ex.Account)
        account.__dict__.update(base.__dict__)
        account.__dict__.update(self.rows)
        account.__dict__.update(_evidence=self,meter_index=IndexedMeters(self.rows['meters']),
            _meter_events=EvidenceLookup(self.rows['meters'],'event'),
            _observed_at=EvidenceLookup(self.rows['observations'],'observed'),
            _measured_at=EvidenceLookup(self.rows['observations'],'measured'))
        if '_meter_bindings' not in account.__dict__:
            bindings=self.database.query(lambda db:db.execute('SELECT stream,boundary,direction,physical_id FROM meter_bindings').fetchall())
            account.__dict__['_meter_bindings']={s:(b,d,p or None) for s,b,d,p in bindings}
            account.__dict__['_physical_meters']=frozenset((s,b,d,p or None) for s,b,d,p in bindings)
        return account

    def evolve(self,account,changes):
        for name in ('receipt','requested_generation'):
            if name in changes:ex._integer(changes[name],minimum=0)
        rows={name:changes.get(name,value) for name,value in self.rows.items()}
        evidence=AccountEvidence(self.database,rows)
        result=evidence.build(account)
        result.__dict__.update(changes)
        if 'meters' in changes:
            row=changes['meters'][-1]
            result.__dict__['_meter_bindings']={**account._meter_bindings,row.stream:(row.boundary,row.direction,row.physical_id)}
            result.__dict__['_physical_meters']=account._physical_meters|{(row.stream,row.boundary,row.direction,row.physical_id)}
        result.__dict__.pop('_summary',None)
        result.__dict__.pop('_live_catalog',None)
        return result

    def _admission_prefix(self,db):
        count=json.loads(db.execute('SELECT counts FROM head').fetchone()[0])['admissions']
        return min(count,len(self.rows['admissions'])),self.rows['admissions'][count:] if count<len(self.rows['admissions']) else ()

    def _catalog(self,at_ms=None,live=False):
        def read(db):
            count,pending=self._admission_prefix(db)
            total=json.loads(db.execute('SELECT counts FROM head').fetchone()[0])['admissions']
            latest=db.execute('SELECT max(at_ms) FROM admissions WHERE ordinal<?',(count,)).fetchone()[0]
            if count==total and (at_ms is None or latest is None or at_ms>=latest):
                where=" WHERE responsibility IN ('outstanding','retained')" if live else ''
                rows=[dict(id=key,objective=json.loads(payload),responsibility=owner,closed_at=closed)
                    for key,payload,owner,closed in db.execute('SELECT id,payload,responsibility,closed_at FROM objective_catalog'+where+' ORDER BY first_admission,first_position')]
                number=db.execute('SELECT count(*) FROM objective_catalog').fetchone()[0]
            else:
                rows={}
                clause='admission<?'+(' AND at_ms<=?' if at_ms is not None else '')
                parameters=(count,at_ms) if at_ms is not None else (count,)
                for key,payload in db.execute('SELECT id,payload FROM objective_versions WHERE '+clause+' ORDER BY admission,position',parameters):
                    row=rows.setdefault(key,dict(id=key,responsibility='outstanding',closed_at=None))
                    row['objective']=json.loads(payload)
                for key,payload,at in db.execute('SELECT id,payload,at_ms FROM objective_dispositions WHERE '+clause+' ORDER BY admission,position',parameters):
                    row=rows[key];owner=json.loads(payload)['outcome'];row['responsibility']=owner
                    if owner in ('incorporated','retired') and row['closed_at'] is None:row['closed_at']=at
                number=len(rows);rows=list(rows.values())
            # New admission deltas have not reached the SQL transaction yet.
            if pending:
                # Admission validation needs the complete compact catalog. A live
                # request must include prior rows reintroduced by a disposition.
                if live:return self._catalog(at_ms,False)
                mapping={r['id']:r for r in rows}
                for admission in pending:
                    if at_ms is not None and admission.at_ms>at_ms:continue
                    for objective in admission.contract.objectives:
                        row=mapping.setdefault(objective.id,dict(id=objective.id,responsibility='outstanding',closed_at=None))
                        row['objective']=asdict(objective)
                    for disposition in admission.contract.dispositions:
                        row=mapping[disposition.objective_id];row['responsibility']=disposition.outcome
                        if disposition.outcome in ('incorporated','retired') and row['closed_at'] is None:row['closed_at']=admission.at_ms
                rows=list(mapping.values());number=len(rows)
            return rows,number
        return self.database.query(read)

    def objective_catalog(self):
        rows,_=self._catalog()
        return {row['id']:ex.Objective(**row['objective']) for row in rows}

    def objective_history(self,account,at_ms):
        rows,_=self._catalog(at_ms)
        for row in rows:
            def read(db):
                count,pending=self._admission_prefix(db)
                versions=[dict(objective=json.loads(payload),contract_id=contract,at_ms=at)
                    for payload,contract,at in db.execute('SELECT payload,contract_id,at_ms FROM objective_versions WHERE id=? AND admission<? AND at_ms<=? ORDER BY admission,position',(row['id'],count,at_ms))]
                dispositions=[dict(**json.loads(payload),contract_id=contract,at_ms=at)
                    for payload,contract,at in db.execute('SELECT payload,contract_id,at_ms FROM objective_dispositions WHERE id=? AND admission<? AND at_ms<=? ORDER BY admission,position',(row['id'],count,at_ms))]
                for admission in pending:
                    if admission.at_ms>at_ms:continue
                    versions.extend(dict(objective=asdict(o),contract_id=admission.contract.id,at_ms=admission.at_ms) for o in admission.contract.objectives if o.id==row['id'])
                    dispositions.extend(dict(**asdict(d),contract_id=admission.contract.id,at_ms=admission.at_ms) for d in admission.contract.dispositions if d.objective_id==row['id'])
                return versions,dispositions
            versions,dispositions=self.database.query(read)
            outcome=ex._objective_outcome(account,ex.Objective(**row['objective']),row['closed_at'],at_ms,account._measured_at)
            yield dict(objective=row['objective'],origin_contract_id=versions[0]['contract_id'],versions=versions,
                dispositions=dispositions,**outcome,responsibility=row['responsibility'])

    def live_objectives(self,account,at_ms):
        with self.database.snapshot():
            return self._live_objectives(account,at_ms)

    def _live_objectives(self,account,at_ms):
        rows,number=self._catalog(at_ms,True)
        result=[]
        for row in rows:
            if row['responsibility'] not in ('outstanding','retained'):continue
            value=ex._objective_outcome(account,ex.Objective(**row['objective']),row['closed_at'],at_ms,account._measured_at)
            if value['outcome'] in ('fulfilled','forecast_complete'):continue
            result.append(dict(objective=row['objective'],responsibility=row['responsibility'],
                **{key:value[key] for key in ('outcome','shortfall_mwh','fulfilment_basis')}))
        return result,number

    def receipt_summary(self,account):
        if '_summary' in account.__dict__:return account.__dict__['_summary']
        acknowledged=account.contract.source_receipt if account.contract else 0
        def summary(db):
            current=json.loads(db.execute('SELECT counts FROM head').fetchone()[0])['meters']
            through=min(len(self.rows['meters']),current)
            pending_rows=self.rows['meters'][current:]
            streams={r[0] for r in db.execute('SELECT DISTINCT stream FROM meter_bindings')}|{r.stream for r in pending_rows}
            count=0;earliest=None
            for stream in streams:
                row=db.execute('SELECT max_source FROM meter_prefix WHERE stream=? AND receipt<=? AND ordinal<? ORDER BY receipt DESC LIMIT 1',(stream,acknowledged,through)).fetchone()
                prior=max([row[0] if row else -1]+[r.source_at_ms for r in pending_rows if r.stream==stream and r.receipt<=acknowledged])
                number,at=db.execute('SELECT count(*),min(source_at_ms) FROM meters WHERE stream=? AND receipt>? AND ordinal<? AND source_at_ms<=?',(stream,acknowledged,through,prior)).fetchone()
                count+=number
                if at is not None:earliest=min(earliest,at) if earliest is not None else at
                for pending in pending_rows:
                    if pending.stream==stream and pending.receipt>acknowledged and pending.source_at_ms<=prior:
                        count+=1;earliest=min(earliest,pending.source_at_ms) if earliest is not None else pending.source_at_ms
            return dict(through_receipt=account.receipt,previously_acknowledged_receipt=acknowledged,
                late_evidence_count=count,earliest_amended_source_ms=earliest)
        result=self.database.query(summary);account.__dict__['_summary']=result
        return result

    def planner_feedback(self,account,at_ms):
        with self.database.snapshot():
            return self._planner_feedback(account,at_ms)

    def _planner_feedback(self,account,at_ms):
        history=self.objective_history(account,at_ms) if account.contract else ()
        digest=sha256();digest.update(b'[');live=[];count=0
        for item in history:
            if count:digest.update(b',')
            digest.update(json.dumps(item,sort_keys=True,separators=(',',':')).encode());count+=1
            if item['responsibility'] in ('outstanding','retained') and item['outcome'] not in ('fulfilled','forecast_complete'):
                live.append({key:item[key] for key in ('objective','responsibility','outcome','shortfall_mwh','fulfilment_basis')})
        digest.update(b']')
        return dict(generation=account.requested_generation,source_receipt=account.receipt,
            previous_contract_id=account.contract.id if account.contract else None,
            observed=asdict(account.observed) if account.observed else None,
            state_reconciliations=[asdict(r) for r in account.reconciliations[-1:]],
            balance=asdict(ex.balance(account,at_ms)) if account.contract else None,objectives=live,
            settled_history=dict(objective_count=count,sha256=digest.hexdigest(),**self.receipt_summary(account)))
