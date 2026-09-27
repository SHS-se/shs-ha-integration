"""Configuration acknowledgement, conflicts and recovery across lost replies."""
import asyncio
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from shs_app.configuration import Configuration
from shs_app.records import RecordStore
from shs_core.execution_configuration import ExecutionConfiguration
from shs_core.gateway_journal import digest


class ConfigurationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.invalidations, self.captures = 0, []
        def invalidate(): self.invalidations += 1
        async def capture(): self.captures.append(deepcopy(self.gateway.value))
        self.gateway = ExecutionConfiguration(RecordStore(self.root/'gateway.json'),invalidate,capture)
        await self.gateway.load({'device_modes':{'battery':'control_verification'}})
        self.credentials_calls = 0
        async def credentials():
            self.credentials_calls += 1
            return {'device_token':'private-app-credential'}
        self.credentials = credentials
        self.app = Configuration(self.root,{'installation':'original'},self.gateway.install)
        await self.app.load(self.gateway.value,credentials)

    async def test_adoption_is_once_and_mode_success_means_native_revision_applied(self):
        result = await self.app.commit(1,{'device_modes':{'battery':'controlling'}},'ha-mode')
        self.assertEqual(result,{'revision':2})
        self.assertEqual(self.app.options(),self.gateway.options())
        self.assertEqual(self.captures[-1]['revision'],2)
        reopened = Configuration(self.root,{'installation':'original'},self.gateway.install)
        await reopened.load(self.gateway.value,self.credentials)
        self.assertEqual(reopened.options(),self.app.options())
        self.assertEqual(self.credentials_calls,1)

    async def test_lost_reply_retains_pending_then_reconciles_without_duplicate_revision(self):
        async def lose_reply(body):
            await self.gateway.install(body)
            raise OSError('reply lost')
        previous = self.app.options()
        self.app.install = lose_reply
        with self.assertRaises(OSError):
            await self.app.commit(1,{'device_modes':{}},'edit')
        self.assertEqual(self.app.status()['state'],'pending')
        self.assertEqual(self.app.options(),previous)
        with self.assertRaisesRegex(ValueError,'still waiting'):
            await self.app.commit(1,{},'other')
        reopened = Configuration(self.root,{'installation':'original'},self.gateway.install)
        await reopened.load(self.gateway.value,self.credentials)
        count = self.invalidations
        await reopened.commit(1,{'device_modes':{}},'edit')
        self.assertEqual(reopened.revision,2)
        self.assertEqual(self.invalidations,count)
        with self.assertRaisesRegex(ValueError,'reused'):
            await reopened.commit(1,{},'edit')

    async def test_concurrent_ha_and_browser_edits_cannot_overwrite_each_other(self):
        results = await asyncio.gather(self.app.commit(1,{'mode':'ha'},'ha'),
            self.app.commit(1,{'mode':'browser'},'browser'),return_exceptions=True)
        self.assertEqual(sum(isinstance(r,ValueError) for r in results),1)
        self.assertEqual(self.app.revision,2)
        self.assertEqual(self.gateway.options(),self.app.options())

    async def test_gateway_rejects_stale_conflicting_or_tampered_configuration(self):
        for body in (dict(expected_revision=0,revision=2,options={},digest=digest({})),
                     dict(expected_revision=1,revision=1,options={},digest=digest({})),
                     dict(expected_revision=1,revision=2,options={},digest='wrong')):
            with self.assertRaises(ValueError): await self.gateway.install(body)
        self.assertEqual(self.gateway.value['revision'],1)

    async def test_missing_app_record_is_not_readopted_after_ownership_transfer(self):
        other = self.root/'empty';other.mkdir()
        app = Configuration(other,{'installation':'original'},self.gateway.install)
        with self.assertRaisesRegex(ValueError,'missing after adoption'):
            await app.load(self.gateway.value,self.credentials)
