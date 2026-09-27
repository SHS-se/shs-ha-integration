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
from household_fixture import Rig
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
        c.client=SimpleNamespace(push_optimisation=AsyncMock())

    def assert_retained(self):
        self.assertEqual(self.c.optimisation_plan,self.plan)
        self.assertEqual(self.c._store.saved['optimisation_plan'],self.plan)
        self.assertTrue(self.c.operational_status['actionable'],self.c.operational_status)
        self.assertIsNotNone(self.c.binding_plan_for('battery',self.rig.options)[1])

    async def test_failed_request_after_configuration_change_retains_schedule_through_restore(self):
        self.c._plan_configuration_changed=True
        self.c._store.saved['plan_configuration_changed']=True
        self.c.client.push_optimisation.side_effect=ShsApiError('Planning worker returned HTTP 546')
        await self.c.async_optimisation_push(force_plan=True)
        self.assert_retained()
        self.assertIn('HTTP 546',self.c.last_optimisation_error)
        self.c.optimisation_plan=None
        await self.c.async_restore_plan()
        self.assert_retained()

    async def test_unusable_replacements_preserve_cache_and_acknowledge_rejection(self):
        for failure in ('invalid','infeasible','execution_infeasible','battery_handover'):
            with self.subTest(failure=failure):
                candidate=deepcopy(self.plan)
                candidate['plan_id']=candidate['execution_plan']['plan_id']='c0debabe-1111-4222-8333-123456789abc'
                self.c.battery_runtime=None
                if failure=='invalid':candidate['schema_version']=999
                elif failure=='infeasible':candidate['status']='infeasible'
                elif failure=='execution_infeasible':candidate['execution_plan']['status']='infeasible'
                else:
                    self.c.battery_runtime=SimpleNamespace(validate_plan_response=Mock(side_effect=ValueError('Invalid handover')),
                        reject_plan_response=AsyncMock())
                self.c.client.push_optimisation.return_value={'plan':candidate,'actuals_accepted_until':self.now.isoformat()}
                await self.c.async_optimisation_push(force_plan=True)
                self.assert_retained()
                self.assertEqual(self.c._store.saved['optimisation_pending_plan_ack']['outcome'],'rejected')
                self.assertEqual(self.c._store.saved['optimisation_actuals_accepted_until'],self.now.isoformat())
                if failure in ('infeasible','execution_infeasible'):
                    self.assertIn('no ready schedule',self.c.last_optimisation_error)
                elif failure=='battery_handover':
                    self.assertEqual(self.c.last_optimisation_error,'Invalid handover')

    async def test_no_replacement_or_missing_snapshot_inputs_keep_the_running_plan(self):
        self.c._build_optimisation_snapshot.side_effect=OptimisationInputError('Price input unavailable')
        self.c._price_quarters=lambda *args:[{'start':self.now.isoformat()}]
        self.c.client.push_optimisation.return_value={}
        await self.c.async_optimisation_push(force_plan=True)
        self.assert_retained()
        self.assertEqual(self.c.last_optimisation_error,'Price input unavailable')

    async def test_ready_replacement_replaces_the_cache_and_clears_refresh_pending(self):
        candidate=deepcopy(self.plan)
        candidate['plan_id']=candidate['execution_plan']['plan_id']='c0debabe-1111-4222-8333-123456789abc'
        self.c._plan_configuration_changed=True
        self.c.client.push_optimisation.return_value={'plan':candidate}
        await self.c.async_optimisation_push(force_plan=True)
        self.assertEqual(self.c.optimisation_plan,candidate)
        self.assertEqual(self.c._store.saved['optimisation_plan'],candidate)
        self.assertFalse(self.c._plan_configuration_changed)
        self.assertEqual(self.c._store.saved['optimisation_pending_plan_ack']['outcome'],'accepted')
