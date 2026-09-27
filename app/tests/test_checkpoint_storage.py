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
    async def test_partial_and_failed_receipts_keep_predecessor_mirror(self):
        with tempfile.TemporaryDirectory() as root:
            path=Path(root)/'execution.sqlite'
            mirror=ObservationMirror();mirror.context={'home':'fixture'}
            mirror.revision=1;mirror.rows={'sensor.a':{'state':'1'}}
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
