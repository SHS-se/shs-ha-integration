import asyncio
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from shs_app.checkpoint_storage import CheckpointStorage
from shs_app.sources import ObservationMirror
from shs_core.home_runtime import ExecutionSession


class CheckpointTests(unittest.IsolatedAsyncioTestCase):
    def test_live_frame_cannot_overwrite_a_newer_ordered_value_or_tombstone(self):
        mirror=ObservationMirror()
        mirror.install_snapshot(dict(configuration={},through=1,observations={}))
        for value in ('newer',None):
            mirror.apply(dict(ordinal=3,kind='observation',payload=dict(entity_id='sensor.a',state=value)),notify=False)
            mirror.apply_live({'sensor.a':dict(entity_id='sensor.a',state='older frozen value')},through=2)
            if value is None:
                self.assertNotIn('sensor.a',mirror.rows)
            else:
                self.assertEqual(mirror.rows['sensor.a']['state'],value)

    async def test_batch_then_urgent_partial_receipt_reopens_at_exact_predecessor(self):
        with tempfile.TemporaryDirectory() as root:
            mirror=ObservationMirror()
            mirror.install_snapshot(dict(configuration={'home':'fixture'},through=1,
                observations={'sensor.a':{'value':{'state':'1'}}}))
            store=CheckpointStorage(Path(root)/'execution.sqlite',asyncio.to_thread,mirror)
            await store.load()
            metadata=lambda receipt,complete:{'gateway_processing':dict(receipt=receipt,complete=complete)}
            await store.save(metadata(1,True),ExecutionSession())
            for ordinal in (2,3):
                mirror.apply(dict(ordinal=ordinal,kind='observation',payload=dict(entity_id='sensor.a',state=str(ordinal))),notify=False)
            await store.save(metadata(3,False),ExecutionSession())
            reopened=CheckpointStorage(store.path,asyncio.to_thread,mirror)
            await reopened.load()
            self.assertEqual(reopened.source_checkpoint['receipt'],2)
            self.assertEqual(reopened.source_checkpoint['rows']['sensor.a']['state'],'2')
            mirror.apply_live({'sensor.a':dict(entity_id='sensor.a',state='replaceable later value')},through=3)
            await store.save(metadata(3,True),ExecutionSession())
            reopened=CheckpointStorage(store.path,asyncio.to_thread,mirror)
            await reopened.load()
            self.assertEqual(reopened.source_checkpoint['rows']['sensor.a']['state'],'3')

    async def test_partial_and_failed_receipts_keep_predecessor_mirror(self):
        with tempfile.TemporaryDirectory() as root:
            path=Path(root)/'execution.sqlite'
            mirror=ObservationMirror()
            mirror.install_snapshot(dict(configuration={'home':'fixture'},through=1,
                observations={'sensor.a':{'value':{'state':'1'}}}))
            store=CheckpointStorage(path,asyncio.to_thread,mirror)
            await store.load()
            def metadata(receipt,complete):
                return {'gateway_processing':dict(receipt=receipt,complete=complete)}
            await store.save(metadata(1,True),ExecutionSession())
            previous=deepcopy(store.source_checkpoint)
            mirror.revision=2;mirror.rows['sensor.a']['state']='2'
            await store.save(metadata(2,False),ExecutionSession())
            reopened=CheckpointStorage(path,asyncio.to_thread,mirror)
            await reopened.load()
            self.assertEqual(reopened.source_checkpoint,previous)
            with patch.object(store,'_commit',side_effect=OSError('commit interrupted')):
                with self.assertRaises(OSError):await store.save(metadata(2,True),ExecutionSession())
            self.assertEqual(store.source_checkpoint,previous)
            await store.save(metadata(2,True),ExecutionSession())
            reopened=CheckpointStorage(path,asyncio.to_thread,mirror)
            await reopened.load()
            self.assertEqual(reopened.source_checkpoint['rows'],mirror.rows)
            self.assertEqual(reopened.source_checkpoint['receipt'],2)
