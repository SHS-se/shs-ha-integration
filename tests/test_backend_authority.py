"""An observing planner has no physical admission or execution side effects."""
from copy import deepcopy
import unittest
from unittest.mock import AsyncMock
from household_fixture import Rig
from shs_core.operating_modes import transfer_admissions, reconcile_admissions


class BackendAuthorityTests(unittest.IsolatedAsyncioTestCase):
    async def test_observer_choices_do_not_admit_devices_or_advance_battery(self):
        rig=Rig(options={'planning_mode':'live','device_modes':{'$battery':'controlling'}})
        h=rig.household
        h.control_authority=False
        h.battery_runtime=AsyncMock()
        before=deepcopy(rig.options)
        await h._record_device_exchange({},[],{'device_configuration':[],
            'home_configuration':{'battery':{'included':False,'choice_at':'other'}}})
        self.assertEqual(rig.options,before)
        await h.async_battery_inputs_refresh()
        h.battery_runtime.refresh.assert_not_awaited()
        await h.async_report_runtime()
        h.client.report_runtime.assert_not_called()
        pending={'optimisation_pending_plan_ack':{'plan':{'plan_id':'observer'},'outcome':'accepted'}}
        await h._retry_pending_plan_ack(pending)
        h.client.acknowledge_optimisation_plan.assert_not_called()
        self.assertNotIn('optimisation_pending_plan_ack',pending)

    async def test_cloud_watermarks_and_same_job_ids_are_independent(self):
        test=Rig(stored={'optimisation_actuals_accepted_until':'test', 'optimisation_pending_job':{'id':'same'}})
        prod=Rig(stored={'optimisation_actuals_accepted_until':'prod', 'optimisation_pending_job':{'id':'same'}})
        record=await test.household._store.async_load()
        record['optimisation_actuals_accepted_until']='new-test'
        await test.household._store.async_save(record)
        self.assertEqual((await prod.household._store.async_read())['optimisation_actuals_accepted_until'],'prod')

    async def test_admission_transfer_preserves_modes_and_rejects_different_owners(self):
        old={'device_modes':{'$battery':'controlling'},'planning_admissions':{'$battery':[['$battery','old','battery']]}}
        home={'battery':{'included':True,'choice_at':'new'}}
        result=transfer_admissions(old,[],home)
        self.assertEqual(result['device_modes'],old['device_modes'])
        self.assertEqual(reconcile_admissions(result,[],home),result)
        self.assertEqual(old['planning_admissions']['$battery'][0][1],'old')
        with self.assertRaisesRegex(ValueError,'participation differs'):
            transfer_admissions(old,[],{'battery':{'included':False}})
