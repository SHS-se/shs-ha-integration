"""Commit the completed source mirror in the same transaction as its accounting."""
from copy import deepcopy
from contextlib import closing
from hashlib import sha256
import json

from shs_core.execution_storage import ExecutionStorage, _shell
from shs_core.gateway_journal import GatewayConflict


class CheckpointStorage(ExecutionStorage):
    def __init__(self,path,run,mirror):
        super().__init__(path,run)
        self.mirror=mirror
        self.source_checkpoint=None
        self.metadata=None
        self.pending_checkpoint=None

    async def load(self):
        result=await super().load()
        if result:
            self.metadata=result[0]
            pointer=self.metadata.get('source_checkpoint')
            self.source_checkpoint=await self.run(self._read_source) if pointer is not None else None
            if self.source_checkpoint is not None:
                processing=self.metadata['gateway_processing']
                completed=processing['receipt']-int(not processing['complete'])
                if self.source_checkpoint['receipt']!=completed:
                    raise GatewayConflict('Source mirror differs from committed execution progress')
        return result

    async def _save(self,metadata,session,*,cleanup_pending):
        checkpoint=self.source_checkpoint
        processing=metadata.get('gateway_processing')
        if processing and processing['complete'] and processing['receipt']==self.mirror.revision:
            checkpoint=dict(receipt=self.mirror.revision,context=deepcopy(self.mirror.context),rows=deepcopy(self.mirror.rows))
        self.pending_checkpoint=checkpoint
        metadata={**metadata,**({'source_checkpoint':{'receipt':checkpoint['receipt']}} if checkpoint is not None else {})}
        await super()._save(metadata,session,cleanup_pending=cleanup_pending)
        self.metadata,self.source_checkpoint=metadata,checkpoint

    def _read_source(self):
        with closing(self._connect()) as db:
            receipt,context=db.execute('SELECT receipt,context FROM source_checkpoint WHERE id=1').fetchone()
            return dict(receipt=receipt,context=json.loads(context),
                rows={key:json.loads(value) for key,value in db.execute('SELECT entity,payload FROM source_rows')})

    def _additional_checkpoint(self,db,metadata,session):
        value=self.pending_checkpoint
        if value is None or value is self.source_checkpoint:return
        db.execute('CREATE TABLE IF NOT EXISTS source_checkpoint (id INTEGER PRIMARY KEY CHECK(id=1), receipt INTEGER NOT NULL, context TEXT NOT NULL)')
        db.execute('CREATE TABLE IF NOT EXISTS source_rows (entity TEXT PRIMARY KEY, payload TEXT NOT NULL)')
        encode=lambda data:json.dumps(data,separators=(',',':'),allow_nan=False)
        old=self.source_checkpoint
        if old is None or value['context']!=old['context']:
            db.execute('INSERT OR REPLACE INTO source_checkpoint VALUES (1,?,?)',(value['receipt'],encode(value['context'])))
        else:
            db.execute('UPDATE source_checkpoint SET receipt=? WHERE id=1',(value['receipt'],))
        previous=old['rows'] if old else {}
        for entity in previous.keys()-value['rows'].keys():
            db.execute('DELETE FROM source_rows WHERE entity=?',(entity,))
        for entity,row in value['rows'].items():
            if row!=previous.get(entity):
                db.execute('INSERT OR REPLACE INTO source_rows VALUES (?,?)',(entity,encode(row)))

    def checkpoint_digest(self):
        # Evidence is identified by its committed database revision and receipt.
        # Reconciliation binds this compact checkpoint, not a scan of old pages.
        value=dict(revision=self._revision,metadata=self.metadata,source=self.source_checkpoint,session=_shell(self._session),
                   counts=self.resource_counts())
        return sha256(json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()
