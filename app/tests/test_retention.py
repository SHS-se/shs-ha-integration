import asyncio
from copy import deepcopy
from datetime import datetime,timezone,timedelta
from pathlib import Path
import tempfile
import unittest
from shs_app.retention import RecentVerification
from shs_app.storage import Diagnostics
from shs_app.database_census import census

class Store:
    def __init__(self):self.saved=None
    async def async_load(self):return deepcopy(self.saved)
    async def async_save(self,data):self.saved=deepcopy(data)

class RetentionTests(unittest.IsolatedAsyncioTestCase):
    async def test_old_diagnostics_and_unused_contexts_expire(self):
        now=datetime.now(timezone.utc);old=(now-timedelta(days=4)).isoformat();recent=now.isoformat()
        main,samples=Store(),Store();journal=RecentVerification(main,samples,now=lambda:now)
        await journal.load()
        journal.attempts=[dict(at=old,last_at=old,count=4,scope='old',slot_id='old'),dict(at=old,last_at=recent,count=8,scope='live',slot_id='live')]
        journal.configurations={'old':{},'live':{}}
        journal.samples=[dict(at=old,slot_id='old',context_id='old')]
        journal.sample_contexts={'old':{}};journal.slots={'old':{},'live':{}}
        journal.events=[dict(at=old),dict(at=recent)]
        journal.dirty=True
        await journal.flush();await journal.flush_samples()
        self.assertEqual(journal.discarded,4);self.assertEqual(len(journal.attempts),1)
        self.assertEqual(journal.samples,[]);self.assertEqual(journal.sample_contexts,{})
        self.assertEqual(set(journal.slots),{'live'});self.assertEqual(len(journal.events),1)
        self.assertEqual(samples.saved['discarded_samples'],1)

    async def test_census_is_read_only_and_resources_keep_three_days(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);db=Diagnostics(root);now=datetime.now(timezone.utc)
            with db.operation('fixture') as connection:
                connection.execute('INSERT INTO resource_samples VALUES (?,?)',((now-timedelta(days=4)).isoformat(),'{}'))
            db.record(now.isoformat(),{})
            before=db.path.read_bytes();result=census(root)
            self.assertEqual(db.path.read_bytes(),before)
            tables=result['databases'][0]['tables']
            self.assertEqual(next(r['rows'] for r in tables if r['name']=='resource_samples'),1)
            self.assertEqual(result['diagnostic_retention_days'],3)
