"""Run the real coordinator exchange/cache boundary with fake HA and cloud ports."""
import ast
import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
from time import monotonic
from types import SimpleNamespace
from typing import Any
import unittest
from unittest.mock import AsyncMock, Mock

ROOT = Path(__file__).parents[1] / 'custom_components/shs_energy'
sys.path.append(str(ROOT))
from refresh import refresh_in_progress
from shs_core import const
from shs_core.optimisation import OptimisationInputError, validate_plan_contract, optimisation_plan_due, quarter_start
from household_fixture import Rig, replacement_plan
from shs_core.api import ShsApiError


class ContinuityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        fixture=json.loads((Path(__file__).parent/'fixtures/schema-9-mixed-mode-plan.json').read_text())
        self.plan=fixture['plan']
        self.now=datetime.fromisoformat(fixture['validation_time'])
        self.rig = Rig(now=self.now, options={'planning_mode':'live','device_modes':deepcopy(self.plan['operating_scope']['modes'])}, stored={'optimisation_plan':deepcopy(self.plan)})
        self.c = c = self.rig.household
        # Store assertions refer to the durable bytes written by the real wrapper.
        c._store = self.rig.records
        c._plan_configuration_changed=False;c._plan_contract=validate_plan_contract
        c.optimisation_plan=self.plan;c.optimisation_missing_inputs=[]
        c.last_optimisation_error=None;c.supplier_prices=[]
        c._configured_entities=lambda:{}
        c._prepared_device_inventory=AsyncMock(return_value=[])
        c._observe_calibration=Mock();c._thermal_quarters=AsyncMock(return_value=[])
        c._optimisation_options=lambda:self.rig.options
        c._build_optimisation_snapshot=AsyncMock(return_value={})
        c._price_quarters=lambda *args:[]
        c.equipment_presence=lambda:{}
        c._record_device_exchange=AsyncMock(return_value={})
        c._retry_pending_plan_ack=AsyncMock(return_value=False)
        c._sync_plan_refused_issue=Mock();c._sync_optimisation_issue=Mock()
        c.async_update_listeners=Mock();c.async_report_runtime=AsyncMock()
        c.async_battery_inputs_refresh=AsyncMock()
        c.client=SimpleNamespace(submit_optimisation=AsyncMock())

    def assert_retained(self):
        self.assertEqual(self.c.optimisation_plan,self.plan)
        self.assertEqual(self.c._store.saved['optimisation_plan'],self.plan)
        self.assertTrue(self.c.operational_status['actionable'],self.c.operational_status)
        self.assertIsNotNone(self.c.binding_plan_for('battery',self.rig.options)[1])

    async def test_failed_request_after_configuration_change_retains_schedule_through_restore(self):
        self.c._plan_configuration_changed=True
        self.c._store.saved['plan_configuration_changed']=True
        self.c.client.submit_optimisation.side_effect=ShsApiError('Planning worker returned HTTP 546')
        await self.c.async_optimisation_push(force_plan=True)
        self.assert_retained()
        self.assertIn('HTTP 546',self.c.last_optimisation_error)
        self.c.optimisation_plan=None
        await self.c.async_restore_plan()
        self.assert_retained()

    async def test_unreadable_replacement_preserves_cache_and_acknowledges_rejection(self):
        candidate=replacement_plan(self.plan)
        candidate['schema_version']=999
        self.c.client.submit_optimisation.return_value={'plan':candidate,'actuals_accepted_until':self.now.isoformat()}
        await self.c.async_optimisation_push(force_plan=True)
        self.assert_retained()
        self.assertEqual(self.c._store.saved['optimisation_pending_plan_ack']['outcome'],'rejected')
        self.assertEqual(self.c._store.saved['optimisation_actuals_accepted_until'],self.now.isoformat())

    async def test_partial_replacements_keep_available_device_instructions(self):
        for branch in ('root', 'execution_plan'):
            with self.subTest(branch=branch):
                self.setUp()  # Independent candidates must not inherit the prior case's pending ACK.
                candidate=replacement_plan(self.plan)
                (candidate if branch=='root' else candidate[branch])['status']='infeasible'
                self.c.client.submit_optimisation.return_value={'plan':candidate}
                await self.c.async_optimisation_push(force_plan=True)
                self.assertEqual(self.c.optimisation_plan,candidate)
                self.assertEqual(self.c._store.saved['optimisation_pending_plan_ack']['outcome'],'accepted')
                self.assertTrue(self.c.operational_status['actionable'])
                self.assertIsNotNone(self.c.binding_plan_for('pool',self.rig.options)[1])

    async def test_battery_handover_failure_accepts_household_and_reports_only_battery(self):
        candidate=replacement_plan(self.plan)
        self.c.battery_runtime=SimpleNamespace(validate_plan_response=Mock(side_effect=ValueError('Invalid handover')),
            reject_plan_response=AsyncMock())
        self.c.client.submit_optimisation.return_value={'plan':candidate}
        await self.c.async_optimisation_push(force_plan=True)
        self.assertEqual(self.c.optimisation_plan,candidate)
        self.assertEqual(self.c._store.saved['optimisation_pending_plan_ack']['outcome'],'accepted')
        self.c.battery_runtime.reject_plan_response.assert_awaited_once_with(candidate,'Invalid handover')
        self.assertIsNotNone(self.c.binding_plan_for('pool',self.rig.options)[1])
        self.assertIsNone(self.c.last_optimisation_error)

    async def test_real_battery_account_survives_rejected_amendment_while_household_advances(self):
        from dataclasses import replace
        import test_battery_runtime as battery_fixtures
        from shs_core.plan_execution import contract_wire, Disposition
        battery=battery_fixtures.Rig('control_verification')
        battery.plan['plan_id']=self.plan['plan_id']
        battery.install_contract()
        await battery.start()
        try:
            account=battery.runtime.host.state.execution.account
            old=account.contract
            feedback=await battery.runtime.capture_feedback(5000000,'snapshot')
            revised=replace(old,id='amendment',generation=feedback['generation'],
                source_receipt=feedback['source_receipt'],previous_contract_id=old.id,
                objectives=(replace(old.objectives[0],target_mwh=old.objectives[0].target_mwh+1000),))
            candidate=deepcopy(self.plan)
            candidate['battery_execution']=contract_wire(revised)
            self.c.battery_runtime=battery.runtime
            stored=await self.c._store.async_load()
            await self.c._accept_returned_plan(stored,candidate)
            self.assertEqual(self.c.optimisation_plan,candidate)
            self.assertEqual(stored['optimisation_pending_plan_ack']['outcome'],'accepted')
            self.assertEqual(battery.runtime.host.state.execution.account.contract,old)
            self.assertIn('explicit retained amendment',battery.runtime.snapshot()['plan_rejection']['reason'])
            self.assertIsNotNone(self.c.binding_plan_for('pool',self.rig.options)[1])
            revised=replace(revised,dispositions=(Disposition(old.objectives[0].id,'retained',None,'Amend target'),))
            candidate['battery_execution']=contract_wire(revised)
            await self.c._accept_returned_plan(stored,candidate)
            battery.plan['battery_execution']=candidate['battery_execution']
            await battery.advance()
            self.assertEqual(battery.runtime.host.state.execution.account.contract.id,'amendment')
            self.assertIsNone(battery.runtime.snapshot()['plan_rejection'])
            self.assertEqual(battery.calls,[])
        finally:await battery.runtime.close()

    async def test_failed_battery_journal_does_not_block_household_acceptance(self):
        import test_battery_runtime as battery_fixtures
        battery=battery_fixtures.Rig('control_verification')
        await battery.start()
        try:
            old=battery.runtime.plan_feedback()['accepted']
            battery.store.save=AsyncMock(side_effect=OSError('disk full'))
            candidate=replacement_plan(self.plan)
            candidate['battery_execution']={'id':'unreadable','generation':1}
            self.c.battery_runtime=battery.runtime
            stored=await self.c._store.async_load()
            await self.c._accept_returned_plan(stored,candidate)
            self.assertEqual(self.c.optimisation_plan,candidate)
            self.assertEqual(stored['optimisation_pending_plan_ack']['outcome'],'accepted')
            self.assertEqual(battery.runtime.plan_feedback()['accepted'],old)
            self.assertEqual(battery.runtime.snapshot()['state'],'fault')
            self.assertEqual(battery.runtime.snapshot()['technical_error'],'disk full')
            self.assertEqual(battery.runtime.snapshot()['fix'],{'kind':'diagnostics'})
            self.assertIsNotNone(battery.runtime.host._fault)
            self.assertEqual(battery.calls,[])
        finally:await battery.runtime.close()

    async def test_no_replacement_or_missing_snapshot_inputs_keep_the_running_plan(self):
        self.c._build_optimisation_snapshot.side_effect=OptimisationInputError('Price input unavailable')
        self.c._price_quarters=lambda *args:[{'start':self.now.isoformat()}]
        self.c.client.submit_optimisation.return_value={}
        await self.c.async_optimisation_push(force_plan=True)
        self.assert_retained()
        self.assertEqual(self.c.last_optimisation_error,'Price input unavailable')

    async def test_ready_replacement_replaces_the_cache_and_clears_refresh_pending(self):
        candidate=replacement_plan(self.plan)
        self.c._plan_configuration_changed=True
        self.c.client.submit_optimisation.return_value={'plan':candidate}
        await self.c.async_optimisation_push(force_plan=True)
        self.assertEqual(self.c.optimisation_plan,candidate)
        self.assertEqual(self.c._store.saved['optimisation_plan'],candidate)
        self.assertFalse(self.c._plan_configuration_changed)
        self.assertEqual(self.c._store.saved['optimisation_pending_plan_ack']['outcome'],'accepted')
