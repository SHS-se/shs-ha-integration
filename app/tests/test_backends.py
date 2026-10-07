"""Backend adoption, revision fencing and independent cloud-session supervision."""
import asyncio
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from shs_app.backends import Backends, URLS
from shs_app.records import RecordStore
from shs_app.engine import AppEngine
from shs_core.gateway_journal import GatewayConflict
from shs_core.household_ports import HouseholdRefreshError


class BackendTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root/'stores').mkdir()
        self.identity = {'entry_id':'home'}
        self.backends = Backends(self.root,self.identity)
        await RecordStore(self.root/'stores/shs_energy.home').async_save({'accepted_until':'old'})
        await self.backends.load({'base_url':URLS['test'],'device_token':'test-private','home_id':'home'})

    async def test_adoption_partitions_cloud_state_and_never_reimports_old_progress(self):
        record = RecordStore(self.root/'stores/shs_energy.cloud.test.home')
        self.assertEqual(await record.async_load(),{'accepted_until':'old'})
        await record.async_save({'accepted_until':'new'})
        reopened = Backends(self.root,self.identity)
        await reopened.load({'base_url':URLS['production'],'device_token':'ignored'})
        self.assertEqual(reopened.controlling,'test')
        self.assertEqual(await record.async_load(),{'accepted_until':'new'})
        self.assertEqual(set(reopened.data['credentials']),{'test'})
        (self.root/'backends.json').unlink()
        with self.assertRaisesRegex(GatewayConflict,'missing after adoption'):
            await Backends(self.root,self.identity).load({'base_url':URLS['test'],'device_token':'old'})

    async def test_selection_and_pairing_are_revision_fenced_and_persist_independently(self):
        credentials={'base_url':URLS['production'],'device_token':'prod-private','home_id':'home'}
        await self.backends.change('production',1,'pair',credentials=credentials)
        self.assertEqual(self.backends.controlling,'test')
        await self.backends.change('production',2,'select')
        await self.backends.change('production',2,'select')
        with self.assertRaisesRegex(GatewayConflict,'changed'):
            await self.backends.change('test',2,'stale')
        await self.backends.settled()
        reopened = Backends(self.root,self.identity)
        await reopened.load({})
        self.assertEqual(reopened.controlling,'production')
        self.assertEqual(reopened.credentials('test')['device_token'],'test-private')
        status=reopened.public_status({})
        self.assertNotIn('private',str(status))
        self.assertEqual([row['selected'] for row in status['environments']],[True,False])

    async def test_one_failed_backend_does_not_stop_sibling_delivery(self):
        engine=AppEngine.__new__(AppEngine)
        engine.backends=self.backends
        engine.wake_projection=asyncio.Event()
        failed=asyncio.Event()
        delivered=asyncio.Event()
        async def bad():
            failed.set()
            raise HouseholdRefreshError('inactive account')
        async def good(): delivered.set()
        task=asyncio.create_task(engine.cloud_job('production',bad))
        try:
            await failed.wait()
            await engine.cloud_job('test',good)
            self.assertTrue(delivered.is_set())
            self.assertFalse(task.done())
            self.assertEqual(self.backends.errors['production'],'inactive account')
        finally:
            task.cancel()
            await asyncio.gather(task,return_exceptions=True)

    async def test_unpaired_and_inactive_selection_preserve_source_and_request_no_restart(self):
        engine=AppEngine.__new__(AppEngine)
        engine.backends=self.backends
        engine.configuration=SimpleNamespace(options=lambda:{})
        engine.households={}
        engine.request_restart=lambda:self.fail('Invalid selection must not restart')
        with self.assertRaisesRegex(ValueError,'Pair this backend'):
            await engine.select_backend({'environment':'production'})
        h=SimpleNamespace(client=SimpleNamespace(status=AsyncMock(return_value={'subscription_active':False})))
        engine.households['production']=h
        with patch('shs_app.engine.validate_server_contract'):
            with self.assertRaisesRegex(ValueError,'Activate a subscription'):
                await engine.select_backend({'environment':'production'})
        self.assertEqual(self.backends.controlling,'test')

    async def test_ready_selection_changes_only_durable_source_and_preserves_both_credentials(self):
        await self.backends.change('production',1,'pair',credentials={
            'base_url':URLS['production'],'device_token':'prod-private','home_id':'home'})
        options={'device_modes':{},'planning_admissions':{}}
        h=SimpleNamespace(client=SimpleNamespace(status=AsyncMock(return_value={'subscription_active':True})),
            async_refresh_device_configuration=AsyncMock(),
            async_cached_planning_configuration=AsyncMock(return_value={'devices':[],'home':{'battery':{'included':False}}}),
            optimisation_plan={'status':'ready'},_plan_configuration_changed=False)
        engine=AppEngine.__new__(AppEngine)
        engine.backends=self.backends
        engine.configuration=SimpleNamespace(options=lambda:options)
        engine.households={'production':h}
        with patch('shs_app.engine.validate_server_contract') as server, patch('shs_app.engine.validate_plan_contract') as plan:
            await engine.select_backend({'environment':'production','backend_revision':2,'request_id':'select'})
            server.assert_called_once()
            plan.assert_called_once()
        self.assertEqual(self.backends.controlling,'production')
        self.assertEqual(self.backends.data['admitted_for'],'test','Selection settles after the executable plan is accepted')
        self.assertEqual(options,{'device_modes':{},'planning_admissions':{}})
        self.assertEqual(set(self.backends.data['credentials']),{'production','test'})

    async def selected_engine(self):
        await self.backends.change('production',1,'pair',credentials={
            'base_url':URLS['production'],'device_token':'prod-private','home_id':'home'})
        await self.backends.change('production',2,'select')
        h=SimpleNamespace(_plan_configuration_changed=True,
            client=SimpleNamespace(request_replan=AsyncMock(return_value='request')),
            async_cached_exchange_status=AsyncMock(return_value={'planning_job':None,'planning_submission':None}),
            async_answer_replan=AsyncMock(return_value=True),async_optimisation_push=AsyncMock())
        engine=AppEngine.__new__(AppEngine)
        engine.backends=self.backends
        engine.environment='production'
        engine.household=h
        return engine,h

    async def test_selection_explicitly_requests_executable_plan_before_settling(self):
        engine,h=await self.selected_engine()
        async def accepted(request):
            self.assertEqual(self.backends.data['admitted_for'],'test')
            self.assertEqual(request,'request')
            h._plan_configuration_changed=False
            return True
        h.async_answer_replan.side_effect=accepted
        await engine.complete_backend_selection(h)
        h.client.request_replan.assert_awaited_once()
        self.assertEqual(self.backends.data['admitted_for'],'production')
        reopened=Backends(self.root,self.identity)
        await reopened.load({})
        self.assertEqual(reopened.data['admitted_for'],'production')

    async def test_selection_resumes_pending_job_and_submission_without_replacing_them(self):
        engine,h=await self.selected_engine()
        for key in ('planning_job','planning_submission'):
            with self.subTest(key=key):
                self.backends.data['admitted_for']='test'
                h._plan_configuration_changed=True
                pending={'planning_job':None,'planning_submission':None}
                pending[key]={'id':'existing'}
                h.async_cached_exchange_status.return_value=pending
                async def delivered(_):h._plan_configuration_changed=False
                with patch('shs_app.engine.asyncio.sleep',side_effect=delivered):
                    await engine.complete_backend_selection(h)
                self.assertEqual(pending[key],{'id':'existing'})
                self.assertEqual(self.backends.data['admitted_for'],'production')
        h.client.request_replan.assert_not_awaited()
        h.async_answer_replan.assert_not_awaited()

    async def test_interrupted_selection_stays_pending_and_observer_cannot_settle_it(self):
        engine,h=await self.selected_engine()
        await engine.complete_backend_selection(SimpleNamespace())
        self.assertEqual(self.backends.data['admitted_for'],'test')
        h.client.request_replan.side_effect=asyncio.CancelledError
        with self.assertRaises(asyncio.CancelledError):
            await engine.complete_backend_selection(h)
        reopened=Backends(self.root,self.identity)
        await reopened.load({})
        self.assertEqual(reopened.controlling,'production')
        self.assertEqual(reopened.data['admitted_for'],'test')

    async def test_explicit_replan_reanswers_unacknowledged_observer_request(self):
        engine,h=await self.selected_engine()
        h.async_answer_replan.return_value=False
        await engine.request_plan(h)
        h.async_optimisation_push.assert_awaited_once_with(force_plan=True,replan_request_id='request')
        self.assertEqual(self.backends.data['admitted_for'],'test')

    async def test_listener_admitted_job_is_preserved_when_explicit_request_is_deduplicated(self):
        engine,h=await self.selected_engine()
        h.async_answer_replan.return_value=False
        h.async_cached_exchange_status.side_effect=[
            {'planning_job':None,'planning_submission':None},
            {'planning_job':{'job_id':'listener-job'},'planning_submission':None}]
        await engine.request_plan(h)
        h.async_optimisation_push.assert_not_awaited()

    async def test_explicit_request_preserves_existing_submission(self):
        engine,h=await self.selected_engine()
        h.async_cached_exchange_status.return_value={'planning_job':None,'planning_submission':{'snapshot_id':'existing'}}
        await engine.request_plan(h)
        h.client.request_replan.assert_not_awaited()
        h.async_optimisation_push.assert_not_awaited()

    async def test_settled_source_startup_preserves_inflight_execution_request(self):
        engine,h=await self.selected_engine()
        await self.backends.settled()
        engine.periodic=AsyncMock()
        h.async_replan_poll=AsyncMock()
        for key in ('planning_job','planning_submission'):
            with self.subTest(key=key):
                h._plan_configuration_changed=True
                exchange={'planning_job':None,'planning_submission':None}
                exchange[key]={'id':'existing'}
                h.async_cached_exchange_status.return_value=exchange
                async def delivered(delay):
                    if delay==5:h._plan_configuration_changed=False
                with patch('shs_app.engine.asyncio.sleep',side_effect=delivered):
                    await engine.planning(h)
                self.assertEqual(exchange[key],{'id':'existing'})
        h.client.request_replan.assert_not_awaited()
        h.async_optimisation_push.assert_not_awaited()

    async def test_settled_source_startup_requests_executable_replacement(self):
        engine,h=await self.selected_engine()
        await self.backends.settled()
        engine.periodic=AsyncMock()
        h.async_replan_poll=AsyncMock()
        async def accepted(request):
            h._plan_configuration_changed=False
            return True
        h.async_answer_replan.side_effect=accepted
        with patch('shs_app.engine.asyncio.sleep',new=AsyncMock()):
            await engine.planning(h)
        h.client.request_replan.assert_awaited_once()
        h.async_optimisation_push.assert_not_awaited()

    async def test_configuration_replan_preserves_selected_job_and_updates_observer(self):
        engine,h=await self.selected_engine()
        h.async_cached_exchange_status.return_value={'planning_job':{'job_id':'existing'},'planning_submission':None}
        observer=SimpleNamespace(_plan_configuration_changed=False,async_optimisation_push=AsyncMock())
        engine.households={'production':h,'test':observer}
        tasks=[]
        engine.spawn=lambda work,name:tasks.append(asyncio.create_task(work,name=name))
        await engine.replan_all()
        await asyncio.gather(*tasks)
        h.client.request_replan.assert_not_awaited()
        h.async_optimisation_push.assert_not_awaited()
        observer.async_optimisation_push.assert_awaited_once_with(force_plan=True)
