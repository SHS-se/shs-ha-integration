"""Durable planning acceptance and delivery through the real household owner."""
import asyncio
from copy import deepcopy
from datetime import datetime, timedelta
import json
from pathlib import Path
import sys
from types import MethodType, SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

sys.path.append(str(Path(__file__).parents[1] / 'custom_components/shs_energy'))
from household_fixture import Rig, cloud_status, replacement_plan
from shs_core.api import ShsApiError, ShsConnectionError
from shs_core.const import ISSUE_OPTIMISATION_CONFIGURATION
from shs_core.tariff import CALCULATION_VERSION, SCHEMA_VERSION


class PlanningJobTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        fixture = json.loads((Path(__file__).parent / 'fixtures/schema-9-mixed-mode-plan.json').read_text())
        self.plan = fixture['plan']
        self.rig = Rig(now=datetime.fromisoformat(fixture['validation_time']),
            options={'planning_mode': 'live', 'device_modes': deepcopy(self.plan['operating_scope']['modes'])},
            stored={'optimisation_plan': deepcopy(self.plan)})
        self.h = h = self.rig.household
        h.optimisation_plan = deepcopy(self.plan)
        h._configured_entities = lambda: {}
        h._prepared_device_inventory = AsyncMock(return_value=[])
        h._observe_calibration = Mock()
        h._thermal_quarters = AsyncMock(return_value=[])
        h._optimisation_options = lambda: self.rig.options
        h._build_optimisation_snapshot = AsyncMock(return_value={'snapshot_id': 'snapshot'})
        h._price_quarters = lambda *args: []
        h.equipment_presence = lambda: {}
        h._record_device_exchange = AsyncMock(return_value={})
        h._sync_plan_refused_issue = Mock()
        h._sync_optimisation_issue = Mock()
        h.async_report_runtime = AsyncMock()
        h.async_battery_inputs_refresh = AsyncMock()
        h.client = SimpleNamespace(submit_optimisation=AsyncMock(return_value={
            'job_id': 'job', 'state': 'pending', 'pending': True, 'retry_after_ms': 1000,
            'actuals_accepted_until': self.rig.now.isoformat(),
        }), planning_status=AsyncMock(), planning_submission_status=AsyncMock(), acknowledge_optimisation_plan=AsyncMock(), report_replan_failure=AsyncMock())
        self.candidate = replacement_plan(self.plan)

    def published(self, job_id='job'):
        return {'job_id': job_id, 'state': 'published', 'pending': False, 'plan': self.candidate}

    async def accept(self, manual=None):
        await self.h.async_optimisation_push(force_plan=True, replan_request_id=manual)
        return (await self.h._store.async_load())['optimisation_pending_job']

    def failures(self):
        return {item['key']:item for item in self.h.attention_items}

    async def test_supplier_timeout_keeps_replan_pending_and_reports_only_the_price_connection(self):
        h = self.h
        h._sync_optimisation_issue = MethodType(type(h)._sync_optimisation_issue, h)
        h.client.status = AsyncMock(return_value=cloud_status(subscription_active=True))
        h.client.tariff = AsyncMock(return_value={
            'schema_version': SCHEMA_VERSION, 'calculation_version': CALCULATION_VERSION,
            'timezone': 'Europe/Stockholm', 'configuration': None, 'profiles': [], 'missing_inputs': [],
        })
        h.client.prices = AsyncMock(side_effect=ShsConnectionError('Supplier price request timed out'))

        async def snapshot(*_):
            h._supplier_price_catalog()
            return {'snapshot_id': 'snapshot'}

        h._build_optimisation_snapshot.side_effect = snapshot
        await h.async_request_refresh()
        await h.async_optimisation_push(force_plan=True, replan_request_id='manual')

        self.assertEqual(h.optimisation_plan, self.plan)
        self.assertTrue(h.operational_status['actionable'])
        self.assertNotIn(ISSUE_OPTIMISATION_CONFIGURATION, self.failures())
        self.assertNotIn('pipeline:inputs', self.failures())
        self.assertNotIn('pipeline:prices', self.failures())
        self.assertEqual(h.optimisation_missing_inputs, [])
        self.assertEqual(self.rig.records.saved['optimisation_requested_replan'], {'replan_request_id': 'manual'})
        h.connection_health.episodes['prices']['observed_seconds'] = 3600
        self.assertEqual(self.failures()['pipeline:prices']['fix'], {'kind': 'diagnostics'})

        start = self.rig.now
        h.client.prices.side_effect = None
        h.client.prices.return_value = {
            'schema_version': 1, 'missing_inputs': [], 'terms_valid_from': '2026-01-01',
            'configuration': {'supplier': 'tibber', 'price_area': 'SE3'},
            'forecast': [{'start': start.isoformat(), 'end': (start + timedelta(minutes=15)).isoformat(),
                          'spot_price_sek_per_kwh': 0.5, 'supplier_import_price_sek_per_kwh': 0.6,
                          'supplier_export_price_sek_per_kwh': 0.4}],
        }
        await h.async_request_refresh()
        self.assertNotIn('pipeline:prices', self.failures())
        self.assertTrue(h._planning_job_wake.is_set())
        await h.async_optimisation_push()
        self.assertEqual(self.rig.records.saved['optimisation_pending_job']['replan_request_id'], 'manual')

    async def test_confirmed_missing_supplier_settings_still_link_to_the_website(self):
        h = self.h
        h._sync_optimisation_issue = MethodType(type(h)._sync_optimisation_issue, h)
        h.supplier_prices = {'configuration': None}
        h._build_optimisation_snapshot.side_effect = lambda *_: h._supplier_price_catalog()
        await h.async_optimisation_push(force_plan=True)
        issue = self.failures()[ISSUE_OPTIMISATION_CONFIGURATION]
        self.assertEqual(issue['fix'], {'kind': 'website', 'path': '/portal/settings/energy-tariffs'})
        self.assertTrue(any(key == ISSUE_OPTIMISATION_CONFIGURATION and severity == 'warning'
                            for key, severity, _ in self.rig.repairs))
        h.client.submit_optimisation.assert_not_awaited()

    async def test_manual_replan_uploads_completed_meter_evidence_across_dst(self):
        """Synthetic recorder changes, through the real exchange and aggregation.

        These UTC quarters straddle Stockholm's repeated local 02:00 hour.
        The pool's memory must receive the OFF quarter as well as the ON one.
        """
        from datetime import timezone
        boundary = datetime(2025, 10, 26, 1, 15, tzinfo=timezone.utc)
        self.rig.now = boundary + timedelta(minutes=7)
        start = boundary - timedelta(minutes=30)
        self.rig.records.saved['optimisation_actuals_accepted_until'] = start.isoformat()
        self.h._configured_entities = lambda: {'grid_import': ['sensor.synthetic_grid']}
        self.h._prepared_device_inventory.return_value = [{
            'key': 'synthetic_pool', 'statistic_id': 'sensor.synthetic_pool',
            'category': 'pool_heating', 'planning_role': 'controllable'}]

        async def statistics(begin, end, entities, period, units, kinds):
            # Recent quarters behind the watermark are re-offered, so a late
            # statistic fills its quarter instead of becoming a permanent hole.
            # The read starts one period early: the first change is judged against its neighbour.
            self.assertEqual((begin, end, period),
                             (boundary - timedelta(hours=2, minutes=5), boundary, '5minute'))
            return {entity: [{'start': (start + timedelta(minutes=5*i)).timestamp(),
                              'change': .1 if entity == 'sensor.synthetic_grid' else (0 if i < 3 else .3)}
                             for i in range(6)] for entity in entities}

        self.rig.history.statistics.side_effect = statistics
        await self.h.async_optimisation_push(force_plan=True, replan_request_id='synthetic-manual')
        body = self.h.client.submit_optimisation.call_args.args[0]
        actuals, snapshot = body["actual_slots"], body["snapshot"]
        self.assertEqual(snapshot['snapshot_id'], 'snapshot')
        self.assertEqual([row['start'] for row in actuals],
                         [start.isoformat(), (start + timedelta(minutes=15)).isoformat()])
        self.assertEqual([row['device_energy_kwh']['synthetic_pool'] for row in actuals], [0, .9])
        self.assertEqual([row['quality']['duration_seconds'] for row in actuals], [900, 900])

    async def test_a_device_quarter_beside_a_household_row_keeps_its_setting_and_heat(self):
        """Synthetic meters through the real exchange.

        Every quarter the household meter covers also carries the heater's
        power setting and metered heat. They were once dropped exactly there,
        and kept only on quarters the household meter had missed.
        """
        from datetime import timezone
        boundary = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)
        self.rig.now = boundary + timedelta(minutes=3)
        self.rig.records.saved['optimisation_actuals_accepted_until'] = (boundary - timedelta(minutes=15)).isoformat()
        self.h._configured_entities = lambda: {'grid_import': ['sensor.synthetic_grid']}
        self.h._prepared_device_inventory.return_value = [{
            'key': 'synthetic_heater', 'statistic_id': 'sensor.synthetic_heater',
            'category': 'pool_heating', 'planning_role': 'controllable'}]
        self.h._device_observations = lambda devices: {
            'synthetic_heater': {'setting': 'number.synthetic_output', 'heat': 'sensor.synthetic_heat'}}
        first = boundary - timedelta(hours=2, minutes=5)
        per_period = {'sensor.synthetic_grid': .1, 'sensor.synthetic_heater': .25, 'sensor.synthetic_heat': 1.0}

        async def statistics(begin, end, entities, period, units, kinds):
            rows = {}
            for entity in entities:
                # The household meter misses the last quarter; the device does not.
                periods = 25 - (3 if entity == 'sensor.synthetic_grid' else 0)
                rows[entity] = [{'start': (first + timedelta(minutes=5 * i)).timestamp(),
                                 'end': (first + timedelta(minutes=5 * i + 5)).timestamp(),
                                 'state': 100 + per_period[entity] * (i + 1), 'change': per_period[entity]}
                                for i in range(periods)]
            return rows

        self.rig.history.statistics.side_effect = statistics
        self.rig.history.states.return_value = {
            'number.synthetic_output': [(boundary - timedelta(days=1), '12.0', None)]}
        await self.h.async_optimisation_push()
        actuals = self.h.client.submit_optimisation.call_args.args[0]['actual_slots']
        self.assertEqual(len(actuals), 8)
        for row in actuals:
            self.assertEqual(row['device_energy_kwh'], {'synthetic_heater': .75})
            self.assertEqual(row['device_power_setting'], {'synthetic_heater': 12.0})
            self.assertEqual(row['device_heat_kwh'], {'synthetic_heater': 3.0})
        self.assertEqual([row.get('grid_import_kwh') for row in actuals], [.3] * 7 + [None])

    async def test_changed_observations_are_resent_back_to_what_the_website_accepts(self):
        """Seventy-two hours at once, then one older stretch per routine exchange."""
        from datetime import timezone
        boundary = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)
        self.rig.now = boundary + timedelta(minutes=3)
        self.rig.records.saved['optimisation_actuals_accepted_until'] = (boundary - timedelta(minutes=15)).isoformat()
        self.h._prepared_device_inventory.return_value = [{
            'key': 'synthetic_pump', 'statistic_id': 'sensor.synthetic_pump',
            'category': 'pool_heating', 'planning_role': 'controllable'}]
        self.h.client.submit_optimisation.return_value = {
            **self.h.client.submit_optimisation.return_value, 'actuals_accepted_until': boundary.isoformat()}
        self.h._device_observations = lambda devices: {'synthetic_pump': {'setting': 'number.synthetic_capacity'}}
        windows = []

        async def statistics(begin, end, entities, period, units, kinds):
            windows.append((round((boundary - begin - timedelta(minutes=5)) / timedelta(hours=1)),
                            round((boundary - end) / timedelta(hours=1))))
            return {}

        self.rig.history.statistics.side_effect = statistics
        await self.h.async_optimisation_push()
        self.assertEqual(windows, [(72, 0)])
        del windows[:]
        # The exchanges that follow carry history only, so the website starts no job.
        self.h.client.submit_optimisation.return_value = {'actuals_accepted_until': boundary.isoformat()}
        for _ in range(4):
            await self.h.async_optimisation_push()
        # Hours before the boundary: the routine two-hour sweep, then one older
        # stretch, until the website's eight-day window less an hour is reached.
        self.assertEqual(windows, [(2, 0), (144, 72), (2, 0), (191, 144), (2, 0), (2, 0)])
        self.assertNotIn('optimisation_history_resend', await self.h._store.async_load())

    async def test_recent_and_resent_quarters_share_the_ingest_limit(self):
        from datetime import timezone
        from shs_core.device_observations import observation_signature

        boundary = datetime(2026, 11, 10, 12, tzinfo=timezone.utc)
        quarter = timedelta(minutes=15)
        self.rig.now = boundary + timedelta(minutes=3)
        key = 'synthetic_pump'
        self.h._prepared_device_inventory.return_value = [{
            'key': key, 'statistic_id': 'sensor.synthetic_pump',
            'category': 'pool_heating', 'planning_role': 'controllable',
        }]
        observed = {key: {'setting': 'number.synthetic_capacity'}}
        self.h._device_observations = lambda devices: observed
        signature = observation_signature(observed)
        before = boundary - 288 * quarter
        stored = self.rig.records.saved
        stored.update(
            optimisation_actuals_accepted_until=(boundary - quarter).isoformat(),
            optimisation_device_observations_sent=signature,
            optimisation_history_resend={'signature': signature, 'before': before.isoformat()},
            optimisation_catch_up_pending=True,
        )

        async def device_quarters(devices, start, end):
            return [{'start': (start + index * quarter).isoformat(), 'device_energy_kwh': {key: .1}}
                    for index in range((end - start) // quarter)]

        async def submit(body):
            actuals = body['actual_slots']
            self.assertLessEqual(len(actuals), 288)
            return {'actuals_accepted_until': actuals[-1]['start']}

        self.h._device_actual_quarters = AsyncMock(side_effect=device_quarters)
        self.h.client.submit_optimisation.side_effect = submit
        starts = lambda: [row['start'] for row in self.h.client.submit_optimisation.call_args.args[0]['actual_slots']]

        await self.h.async_optimisation_push()
        self.assertEqual(starts(), [(before + index * quarter).isoformat() for index in range(288)])
        stored = await self.h._store.async_load()
        self.assertEqual(stored['optimisation_history_resend']['before'], before.isoformat())
        self.assertNotIn('optimisation_catch_up_pending', stored)

        await self.h.async_optimisation_push()
        first = before - 280 * quarter
        self.assertEqual(starts(), [(first + index * quarter).isoformat() for index in range(280)]
                         + [(boundary - (8 - index) * quarter).isoformat() for index in range(8)])
        stored = await self.h._store.async_load()
        self.assertEqual(stored['optimisation_history_resend']['before'], first.isoformat())

    async def test_a_counter_that_caught_up_has_its_stalled_quarters_sent_again_once(self):
        """Synthetic grid meter: silent from 08:00, then everything at 11:30.

        The routine exchange only reads the last two hours, so the zeros it
        sent for 08:00-10:00 were never measured. The retained quarters are
        sent again once, without the stalled ones.
        """
        from datetime import timezone
        boundary = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)
        self.rig.now = boundary + timedelta(minutes=3)
        self.rig.options['grid_power_entity'] = 'sensor.synthetic_grid_power'
        self.rig.records.saved['optimisation_actuals_accepted_until'] = (boundary - timedelta(minutes=15)).isoformat()
        self.h._configured_entities = lambda: {'grid_import': ['sensor.synthetic_grid']}
        self.h._prepared_device_inventory.return_value = [{
            'key': 'synthetic_pump', 'statistic_id': 'sensor.synthetic_pump',
            'category': 'pool_heating', 'planning_role': 'controllable'}]
        self.h.client.submit_optimisation.return_value = {'actuals_accepted_until': boundary.isoformat()}
        stall, caught_up = boundary - timedelta(hours=4), boundary - timedelta(minutes=30)
        origin = boundary - timedelta(hours=80)
        windows = []

        def energy(start):
            if stall <= start < caught_up:
                return 0.0
            return 4.3 if start == caught_up else .1

        async def statistics(begin, end, entities, period, units, kinds):
            if period == 'month':
                # Never more than 12 kW through the grid meter: 1 kWh in five minutes.
                return {'sensor.synthetic_grid_power': [{'max': 12000.0, 'min': -500.0}]}
            windows.append(round((boundary - begin - timedelta(minutes=5)) / timedelta(hours=1)))
            rows, reading, start = [], 500.0, origin
            while start < end:
                reading += energy(start)
                if start >= begin:
                    rows.append({'start': start.timestamp(), 'end': (start + timedelta(minutes=5)).timestamp(),
                                 'state': reading, 'change': energy(start)})
                start += timedelta(minutes=5)
            return {'sensor.synthetic_grid': rows} if 'sensor.synthetic_grid' in entities else {}

        self.rig.history.statistics.side_effect = statistics
        sent = lambda: [row['start'] for row in self.h.client.submit_optimisation.call_args.args[0]['actual_slots']
                        if 'grid_import_kwh' in row]
        quarter = lambda hours: (boundary - timedelta(hours=hours)).isoformat()
        await self.h.async_optimisation_push()
        # Of the last two hours only the quarter after the catch-up was measured.
        self.assertEqual(sent(), [quarter(.25)])
        del windows[:]
        await self.h.async_optimisation_push()
        resent = sent()
        # One 72-hour stretch (household and device meters), less the stalled
        # quarters and the one that caught up.
        self.assertEqual(set(windows), {72})
        self.assertEqual(len(resent), 72 * 4 - 15)
        self.assertEqual(len(set(resent)), len(resent))
        self.assertIn(quarter(4.25), resent)
        self.assertFalse({quarter(4), quarter(2.25), quarter(2), quarter(.5)} & set(resent))
        # The same catch-up, still inside the two-hour sweep, does not start over.
        del windows[:]
        for _ in range(3):
            await self.h.async_optimisation_push()
        self.assertEqual(set(windows), {2})
        self.assertNotIn('optimisation_catch_up_pending', await self.h._store.async_load())

    async def test_a_requested_replan_does_not_carry_the_older_stretch(self):
        from datetime import timezone
        boundary = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)
        self.rig.now = boundary + timedelta(minutes=3)
        self.rig.records.saved['optimisation_actuals_accepted_until'] = (boundary - timedelta(minutes=15)).isoformat()
        self.h._prepared_device_inventory.return_value = [{
            'key': 'synthetic_pump', 'statistic_id': 'sensor.synthetic_pump',
            'category': 'pool_heating', 'planning_role': 'controllable'}]
        self.h.client.submit_optimisation.return_value = {
            **self.h.client.submit_optimisation.return_value, 'actuals_accepted_until': boundary.isoformat()}
        self.h._device_observations = lambda devices: {'synthetic_pump': {'setting': 'number.synthetic_capacity'}}
        begins = []

        async def statistics(begin, end, entities, period, units, kinds):
            begins.append(round((boundary - begin - timedelta(minutes=5)) / timedelta(hours=1)))
            return {}

        self.rig.history.statistics.side_effect = statistics
        await self.h.async_optimisation_push()
        await self.h.async_optimisation_push(force_plan=True, replan_request_id='synthetic-manual')
        self.assertEqual(begins, [72, 2])
        stored = await self.h._store.async_load()
        self.assertEqual(stored['optimisation_history_resend']['before'],
                         (boundary - timedelta(hours=72)).isoformat())

    async def test_a_newly_observed_device_setting_resends_the_retained_quarters_once(self):
        """Naming a power setting or heat meter is learned from history too."""
        from datetime import timezone
        boundary = datetime(2026, 11, 8, 9, tzinfo=timezone.utc)
        self.rig.now = boundary + timedelta(minutes=3)
        self.rig.records.saved['optimisation_actuals_accepted_until'] = (boundary - timedelta(minutes=15)).isoformat()
        self.h._prepared_device_inventory.return_value = [{
            'key': 'synthetic_pump', 'statistic_id': 'sensor.synthetic_pump',
            'category': 'pool_heating', 'planning_role': 'controllable'}]
        self.h.client.submit_optimisation.return_value = {
            **self.h.client.submit_optimisation.return_value, 'actuals_accepted_until': boundary.isoformat()}
        observed = {'synthetic_pump': {'setting': 'number.synthetic_capacity'}}
        self.h._device_observations = lambda devices: observed
        begins = []

        async def statistics(begin, end, entities, period, units, kinds):
            begins.append(begin + timedelta(minutes=5))  # each read starts one period early
            return {}

        self.rig.history.statistics.side_effect = statistics
        for _ in range(2):
            await self.h.async_optimisation_push(force_plan=True, replan_request_id='synthetic-manual')
        observed['synthetic_pump']['heat'] = 'sensor.synthetic_heat'
        await self.h.async_optimisation_push(force_plan=True, replan_request_id='synthetic-manual')
        self.assertEqual(begins, [boundary - timedelta(hours=72), boundary - timedelta(hours=2),
                                  boundary - timedelta(hours=72)])

    async def test_device_quarters_upload_without_a_household_meter_or_its_quarter(self):
        """The heater's quarter is evidence of its own: neither a missing grid
        meter nor that meter's late quarter may hold it back."""
        from datetime import timezone
        boundary = datetime(2026, 11, 6, 20, 15, tzinfo=timezone.utc)
        self.rig.now = boundary + timedelta(seconds=40)
        self.rig.records.saved['optimisation_actuals_accepted_until'] = (boundary - timedelta(minutes=30)).isoformat()
        self.h._prepared_device_inventory.return_value = [{
            'key': 'synthetic_pool', 'statistic_id': 'sensor.synthetic_pool',
            'category': 'pool_heating', 'planning_role': 'controllable'}]
        newest = boundary - timedelta(minutes=15)

        async def statistics(begin, end, entities, period, units, kinds):
            # The grid meter has not settled its newest quarter; the heater has.
            return {entity: [{'start': (newest - timedelta(minutes=15) + timedelta(minutes=5*i)).timestamp(), 'change': .1}
                             for i in range(6 if entity == 'sensor.synthetic_pool' else 3)] for entity in entities}

        self.rig.history.statistics.side_effect = statistics
        for entities in ({'grid_import': ['sensor.synthetic_grid']}, {}):
            self.h._configured_entities = lambda entities=entities: entities
            await self.h.async_optimisation_push(force_plan=True, replan_request_id='synthetic-manual')
            actuals = self.h.client.submit_optimisation.call_args.args[0]["actual_slots"]
            by_start = {row['start']: row for row in actuals}
            self.assertAlmostEqual(by_start[newest.isoformat()]['device_energy_kwh']['synthetic_pool'], .3)
            self.assertNotIn('grid_import_kwh', by_start[newest.isoformat()])

    async def test_failed_solve_survives_telemetry_and_new_pending_work_until_a_plan_is_accepted(self):
        pending=await self.accept('manual')
        self.h.client.planning_status.return_value={'state':'failed','detail':'Committed owner removed: old-meter'}
        await self.h._poll_planning_job(pending)
        self.assertIn('pipeline:planning',self.failures())
        self.assertEqual(self.h.optimisation_plan,self.plan)
        self.assertTrue(self.h.operational_status['actionable'])
        self.h.client.submit_optimisation.return_value={}
        self.h._price_quarters=lambda *args:[{'start':self.rig.now.isoformat()}]
        await self.h.async_optimisation_push()
        self.assertIn('pipeline:planning',self.failures())
        self.h.client.submit_optimisation.return_value={'job_id':'replacement','state':'pending','pending':True,'retry_after_ms':1000}
        pending=await self.accept('corrected')
        self.assertIn('pipeline:planning',self.failures())
        self.h.client.planning_status.return_value=self.published('replacement')
        await self.h._poll_planning_job(pending)
        self.assertNotIn('pipeline:planning',self.failures())
        self.assertEqual(self.h.optimisation_plan,self.candidate)

    async def test_delivery_upload_and_acknowledgement_recover_independently(self):
        pending=await self.accept('manual')
        self.h.client.planning_status.side_effect=ShsApiError('Plan delivery timed out')
        await self.h._poll_planning_job(pending)
        self.assertIn('pipeline:delivery',self.failures())
        self.h._price_quarters=lambda *args:[{'start':self.rig.now.isoformat()}]
        self.h.client.submit_optimisation.side_effect=ShsApiError('Upload refused')
        await self.h.async_optimisation_push()
        self.assertIn('pipeline:upload',self.failures())
        self.assertIn('pipeline:delivery',self.failures())
        self.h.client.submit_optimisation.side_effect=None
        self.h.client.submit_optimisation.return_value={}
        await self.h.async_optimisation_push()
        self.assertNotIn('pipeline:upload',self.failures())
        self.assertIn('pipeline:delivery',self.failures())
        self.h.client.planning_status.side_effect=None
        self.h.client.planning_status.return_value={'state':'pending','retry_after_ms':1000}
        await self.h._poll_planning_job(pending)
        self.assertNotIn('pipeline:delivery',self.failures())
        self.h.client.acknowledge_optimisation_plan.side_effect=ShsApiError('Acknowledgement failed', retryable=True)
        self.h.client.planning_status.return_value=self.published()
        await self.h._poll_planning_job(pending)
        self.assertIn('pipeline:acknowledgement',self.failures())
        self.assertEqual(self.h.optimisation_plan,self.candidate)
        stored=await self.h._store.async_load()
        self.h.client.acknowledge_optimisation_plan.side_effect=None
        self.rig.now += timedelta(seconds=6)
        await self.h._retry_pending_plan_ack(stored)
        self.assertNotIn('pipeline:acknowledgement',self.failures())

    async def test_old_delivery_completion_cannot_clear_the_current_receipts_failure(self):
        pending=await self.accept('manual')
        entered,finish=asyncio.Event(),asyncio.Event()
        async def delayed(_job):
            entered.set();await finish.wait()
            return {'state':'pending','retry_after_ms':1000}
        self.h.client.planning_status.side_effect=delayed
        task=asyncio.create_task(self.h._poll_planning_job(pending))
        await entered.wait()
        stored=await self.h._store.async_load()
        stored['optimisation_pending_job']['job_id']='new-job'
        await self.h._store.async_save(stored)
        self.h._pipeline_error('delivery','The new job could not be received')
        finish.set();await task
        self.assertEqual(self.failures()['pipeline:delivery']['detail'],'The new job could not be received')

    async def test_source_failure_is_visible_despite_successful_upload_and_keeps_its_source_action(self):
        from shs_core.optimisation import OptimisationInputError
        self.h._build_optimisation_snapshot.side_effect=OptimisationInputError(
            'sensor.pv_forecast has no timestamped watts',fix={'kind':'entity','entity_id':'sensor.pv_forecast'})
        await self.h.async_optimisation_push(force_plan=True,replan_request_id='missing-forecast')
        issue=self.failures()['pipeline:inputs']
        self.assertIn('sensor.pv_forecast',issue['detail'])
        self.assertEqual(issue['fix'],{'kind':'entity','entity_id':'sensor.pv_forecast'})
        self.assertNotIn('pipeline:upload',self.failures())
        self.h._build_optimisation_snapshot.side_effect=None
        await self.h.async_optimisation_push(force_plan=True,replan_request_id='corrected-forecast')
        self.assertNotIn('pipeline:inputs',self.failures())

    async def test_a_rejected_replacement_stays_visible_after_an_unrelated_exchange(self):
        stored=await self.h._store.async_load()
        bad=deepcopy(self.candidate);bad['schema_version']=-1
        self.h._sync_plan_refused_issue=MethodType(type(self.h)._sync_plan_refused_issue,self.h)
        await self.h._accept_returned_plan(stored,bad)
        key=next(item['key'] for item in self.h.attention_items if item['title']=='The latest plan was refused')
        await self.h._accept_returned_plan(stored,None)
        self.assertIn(key,self.failures())
        self.assertEqual(self.h.optimisation_plan,self.plan)
        await self.h._accept_returned_plan(stored,self.candidate)
        self.assertNotIn(key,self.failures())

    async def test_acceptance_advances_watermarks_and_returns_before_delivery(self):
        pending = await self.accept('manual')
        self.assertEqual(pending['job_id'], 'job')
        self.assertEqual(pending['replan_request_id'], 'manual')
        self.assertEqual(self.rig.records.saved['optimisation_actuals_accepted_until'], self.rig.now.isoformat())
        self.assertEqual(self.h.optimisation_plan, self.plan)
        self.assertFalse(self.h._push_lock.locked())
        self.h.client.planning_status.assert_not_awaited()
        self.h.client.planning_status.return_value = {'job_id': 'job', 'state': 'pending', 'pending': True, 'retry_after_ms': 2345}
        self.assertEqual(await self.h._poll_planning_job(pending), 2.345)
        self.h.client.planning_status.return_value = self.published()
        await self.h._poll_planning_job(pending)
        self.assertEqual(self.h.optimisation_plan, self.candidate)
        self.assertNotIn('optimisation_pending_job', self.rig.records.saved)
        self.h.client.acknowledge_optimisation_plan.assert_awaited_once()
        self.assertEqual(self.h.client.acknowledge_optimisation_plan.call_args.args[1], 'accepted')

    async def test_delayed_durable_result_is_installed_and_acknowledged_with_its_original_issue_time(self):
        pending = await self.accept('manual')
        issued = self.candidate['issued_at']
        self.rig.now += timedelta(minutes=32)
        self.h.client.planning_status.return_value = self.published()
        await self.h._poll_planning_job(pending)
        self.assertEqual(self.h.optimisation_plan, self.candidate)
        self.assertEqual(self.h.optimisation_plan['issued_at'], issued)
        self.assertEqual(self.rig.records.saved['optimisation_plan'], self.candidate)
        self.assertNotIn('optimisation_pending_job', self.rig.records.saved)
        self.assertEqual(self.h.client.acknowledge_optimisation_plan.call_args.args[1], 'accepted')

    async def test_normal_exchange_does_not_recapture_accepted_work_and_keeps_settings_available(self):
        await self.accept()
        self.h._build_optimisation_snapshot.reset_mock()
        self.h.client.submit_optimisation.reset_mock()
        self.h.client.submit_optimisation.return_value = {}
        self.h._price_quarters = lambda *args: [{'start': self.rig.now.isoformat()}]
        await self.h.async_optimisation_push()
        self.h._build_optimisation_snapshot.assert_not_awaited()
        self.assertIsNone(self.h.client.submit_optimisation.call_args.args[0].get("snapshot"))
        self.assertFalse(self.h.configuration_busy)
        self.assertEqual((await self.h.async_cached_exchange_status())['planning_job'], {'job_id': 'job', 'state': 'pending'})
        self.h.client.submit_optimisation.return_value = {'job_id': 'new-job', 'state': 'pending', 'pending': True, 'retry_after_ms': 1000}
        await self.h.async_optimisation_push(force_plan=True, replan_request_id='new-manual')
        self.h._build_optimisation_snapshot.assert_awaited_once()
        self.assertEqual(self.rig.records.saved['optimisation_pending_job']['job_id'], 'new-job')

    async def test_controller_recommendation_preserves_pending_execution_snapshot(self):
        pending = await self.accept('manual')
        for key in ('optimisation_pending_job', 'optimisation_pending_submission'):
            with self.subTest(key=key):
                stored = await self.h._store.async_load()
                stored.pop('optimisation_pending_job', None)
                stored.pop('optimisation_pending_submission', None)
                expected = deepcopy(pending)
                if key == 'optimisation_pending_submission':
                    expected.pop('job_id')
                    expected['snapshot_id'] = 'snapshot'
                stored[key] = expected
                await self.h._store.async_save(stored)
                self.h._build_optimisation_snapshot.reset_mock()
                self.h.client.submit_optimisation.reset_mock()
                self.h.client.submit_optimisation.return_value = {}
                self.h._price_quarters = lambda *args: [{'start': self.rig.now.isoformat()}]
                await self.h.async_optimisation_push(force_plan=True,
                    replan_reason='Battery controller: execution_contract_required. Consider a manual replan.')
                self.h._build_optimisation_snapshot.assert_not_awaited()
                self.assertIsNone(self.h.client.submit_optimisation.call_args.args[0].get("snapshot"))
                self.assertEqual(self.rig.records.saved[key], expected)

    async def test_host_loop_restores_receipt_and_manual_answer_after_restart(self):
        await self.accept('manual')
        # Restoring the same durable record starts no task by itself.
        restarted = Rig(now=self.rig.now, options=self.rig.options, stored=self.rig.records.saved).household
        restarted.client = self.h.client
        restarted.async_report_runtime = AsyncMock()
        restarted.async_battery_inputs_refresh = AsyncMock()
        await restarted.async_restore_plan()
        self.assertEqual(restarted._answered_replan_request_id, 'manual')
        self.assertFalse(await restarted.async_answer_replan('manual'))
        restarted.client.planning_status.return_value = self.published()
        record = await restarted._store.async_load()
        if record.get('optimisation_pending_job'):
            record['optimisation_pending_job'].pop('retry_at', None)
            await restarted._store.async_save(record)
        installed = asyncio.Event()
        restarted.async_update_listeners = installed.set
        task = asyncio.create_task(restarted.async_planning_delivery())
        await asyncio.wait_for(installed.wait(), timeout=1)
        self.assertEqual(restarted.optimisation_plan, self.candidate)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

    async def test_poll_wait_and_transport_failure_keep_lock_free_and_receipt_durable(self):
        pending = await self.accept()
        entered, release = asyncio.Event(), asyncio.Event()
        async def delayed(job):
            entered.set()
            await release.wait()
            raise ShsApiError('connection lost', retryable=True)
        self.h.client.planning_status.side_effect = delayed
        task = asyncio.create_task(self.h._poll_planning_job(pending))
        await entered.wait()
        async with self.h._push_lock:
            self.assertFalse(task.done())
        release.set()
        self.assertGreater(await task, 0)
        self.assertEqual(self.rig.records.saved['optimisation_pending_job']['job_id'], pending['job_id'])
        self.assertEqual(self.h.optimisation_plan, self.plan)
        self.h.client.acknowledge_optimisation_plan.assert_not_awaited()

    async def test_failed_or_superseded_jobs_retain_plan_and_do_not_report_manual_failure(self):
        for state in ('failed', 'superseded'):
            pending = await self.accept('manual')
            self.h.client.planning_status.return_value = {
                'job_id': 'job', 'state': state, 'pending': False, 'detail': 'worker terminated',
            }
            await self.h._poll_planning_job(pending)
            self.assertEqual(self.h.optimisation_plan, self.plan)
            self.assertNotIn('optimisation_pending_job', self.rig.records.saved)
            self.h.client.report_replan_failure.assert_not_awaited()
            self.h.client.acknowledge_optimisation_plan.assert_not_awaited()

    async def test_lost_acceptance_reply_recovers_by_snapshot_and_then_polls_job_only(self):
        self.h.client.submit_optimisation.side_effect = ShsApiError('reply lost', retryable=True)
        await self.h.async_answer_replan('manual')
        pending = (await self.h._store.async_read())['optimisation_pending_submission']
        self.assertEqual(pending['snapshot_id'], 'snapshot')
        self.assertNotIn('job_id', pending)
        self.h.client.report_replan_failure.assert_not_awaited()
        self.h.client.planning_submission_status.return_value = {
            'job_id': 'job', 'snapshot_id': 'snapshot', 'state': 'pending', 'pending': True,
            'retry_after_ms': 1000, 'actuals_accepted_until': self.rig.now.isoformat(),
        }
        await self.h._poll_planning_job(pending)
        accepted = (await self.h._store.async_read())['optimisation_pending_job']
        self.assertEqual(accepted['job_id'], 'job')
        self.assertNotIn('snapshot_id', accepted)
        self.assertNotIn('devices', accepted)
        self.assertNotIn('submission_error', accepted)
        self.assertNotIn('pipeline:upload', self.failures())
        self.assertEqual(self.rig.records.saved['answered_replan_request_id'], 'manual')
        self.assertEqual(self.rig.records.saved['optimisation_actuals_accepted_until'], self.rig.now.isoformat())
        self.h.client.planning_status.return_value = self.published()
        await self.h._poll_planning_job(accepted)
        self.assertEqual(self.h.optimisation_plan, self.candidate)
        self.h.client.planning_submission_status.assert_awaited_once_with('snapshot')
        self.h.client.planning_status.assert_awaited_once_with('job')
        self.h.client.submit_optimisation.assert_awaited_once()

    async def test_restart_recovers_a_lost_acceptance_reply_without_resending_capture(self):
        self.h.client.submit_optimisation.side_effect = ShsApiError('reply lost', retryable=True)
        await self.h.async_answer_replan('manual')
        rig = Rig(now=self.rig.now, options=self.rig.options, stored=self.rig.records.saved)
        restarted = rig.household
        self.assertEqual(rig.spawned, [])
        restarted.client = self.h.client
        restarted._record_device_exchange = AsyncMock(return_value={})
        restarted.async_report_runtime = AsyncMock()
        restarted.async_battery_inputs_refresh = AsyncMock()
        await restarted.async_restore_plan()
        self.assertFalse(await restarted.async_answer_replan('manual'))
        restarted.client.planning_submission_status.return_value = {
            **self.published(), 'snapshot_id': 'snapshot',
        }
        record = await restarted._store.async_load()
        if record.get('optimisation_pending_job'):
            record['optimisation_pending_job'].pop('retry_at', None)
            await restarted._store.async_save(record)
        installed = asyncio.Event()
        restarted.async_update_listeners = installed.set
        task = asyncio.create_task(restarted.async_planning_delivery())
        await asyncio.wait_for(installed.wait(), timeout=1)
        self.assertEqual(restarted.optimisation_plan, self.candidate)
        restarted.client.planning_submission_status.assert_awaited_once_with('snapshot')
        restarted.client.submit_optimisation.assert_awaited_once()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

    async def test_never_accepted_submission_retries_same_capture_without_manual_failure(self):
        self.h.client.submit_optimisation.side_effect = ShsApiError('reply lost', retryable=True)
        await self.h.async_answer_replan('manual')
        pending = (await self.h._store.async_read())['optimisation_pending_submission']
        self.h.client.planning_submission_status.side_effect = ShsApiError('not accepted', code='planning_job_not_found')
        await self.h._poll_planning_job(pending)
        self.assertIn('optimisation_pending_submission', self.rig.records.saved)
        self.assertEqual(self.h.optimisation_plan, self.plan)
        self.h.client.report_replan_failure.assert_not_awaited()
        self.assertEqual(self.h.client.submit_optimisation.await_count, 2)

    async def test_missing_submission_preserves_original_546_across_restart(self):
        original = ShsConnectionError(
            'energy-optimisation-ingest returned HTTP 546 without an SHS API envelope',
            code='upstream_transport_failure', request_id='upload-request', retryable=True,
        )
        self.h.client.submit_optimisation.side_effect = original
        await self.h.async_answer_replan('manual')
        rig = Rig(now=self.rig.now, options=self.rig.options, stored=self.rig.records.saved)
        restarted = rig.household
        restarted.client = self.h.client
        await restarted.async_restore_plan()
        pending = (await restarted._store.async_read())['optimisation_pending_submission']
        restarted.client.planning_submission_status.side_effect = ShsApiError(
            'planning job not found', code='planning_job_not_found', request_id='lookup-request',
        )
        await restarted._poll_planning_job(pending)
        self.assertEqual(restarted.last_optimisation_error, str(original))
        self.assertIn('optimisation_pending_submission', rig.records.saved)
        restarted.client.report_replan_failure.assert_not_awaited()
        self.assertEqual(restarted.optimisation_plan, self.plan)

    async def test_upload_failure_without_snapshot_identity_keeps_original_error(self):
        self.h._build_optimisation_snapshot.return_value = {}
        original = ShsApiError('ingest returned HTTP 546', retryable=True)
        self.h.client.submit_optimisation.side_effect = original
        await self.h.async_optimisation_push(force_plan=True)
        self.assertEqual(self.h.last_optimisation_error, str(original))
        self.assertNotIn('optimisation_pending_submission', self.rig.records.saved)

    async def test_rejected_submission_reports_the_original_error_without_receipt_recovery(self):
        accepted = await self.accept()
        self.h._build_optimisation_snapshot.return_value = {'snapshot_id': 'replacement'}
        error = ShsApiError('invalid snapshot (Committed owner removed: old-meter)', code='invalid_snapshot')
        self.h.client.submit_optimisation.side_effect = error
        await self.h.async_answer_replan('manual')
        self.assertNotIn('optimisation_pending_submission', self.rig.records.saved)
        self.assertEqual(self.rig.records.saved['optimisation_pending_job'], accepted)
        self.assertEqual(self.h.optimisation_plan, self.plan)
        self.assertEqual(self.h.last_optimisation_error, str(error))
        self.h.client.report_replan_failure.assert_awaited_once_with('manual', str(error))
        self.h.client.planning_submission_status.assert_not_awaited()

    async def test_forced_telemetry_only_exchange_retains_existing_accepted_job(self):
        accepted = await self.accept()
        self.h.client.submit_optimisation.return_value = {}
        await self.h.async_optimisation_push(force_plan=True)
        self.assertEqual(self.rig.records.saved['optimisation_pending_job'], accepted)
        self.assertNotIn('optimisation_pending_submission', self.rig.records.saved)
        self.h.client.planning_status.return_value = self.published()
        await self.h._poll_planning_job(accepted)
        self.assertEqual(self.h.optimisation_plan, self.candidate)

    async def test_lost_replacement_lookup_not_found_retains_existing_accepted_job(self):
        accepted = await self.accept()
        self.h._build_optimisation_snapshot.return_value = {'snapshot_id': 'new-snapshot'}
        self.h.client.submit_optimisation.side_effect = ShsApiError('reply lost', retryable=True)
        await self.h.async_optimisation_push(force_plan=True, replan_request_id='new-manual')
        submission = self.rig.records.saved['optimisation_pending_submission']
        self.assertEqual(self.rig.records.saved['optimisation_pending_job'], accepted)
        self.h.client.planning_submission_status.side_effect = ShsApiError('not accepted', code='planning_job_not_found')
        await self.h._poll_planning_job(submission)
        self.assertIn('optimisation_pending_submission', self.rig.records.saved)
        self.assertEqual(self.rig.records.saved['optimisation_pending_job'], accepted)
        self.h.client.planning_status.return_value = self.published()
        await self.h._poll_planning_job(accepted)
        self.assertEqual(self.h.optimisation_plan, self.candidate)

    async def test_missing_durable_job_is_terminal_and_retains_the_last_plan(self):
        pending = await self.accept()
        self.h.client.planning_status.side_effect = ShsApiError('accepted job is no longer available', code='planning_job_not_found')
        self.assertEqual(await self.h._poll_planning_job(pending), 0)
        self.assertEqual(self.h.optimisation_plan, self.plan)
        self.assertNotIn('optimisation_pending_job', self.rig.records.saved)
        self.assertIn('no longer available', self.h.last_optimisation_error)
        notice = self.failures()['pipeline:delivery']
        self.assertIn('Request a new plan', notice['next_step'])
        self.assertNotIn('retries delivery', notice['next_step'])

    async def test_fresh_inline_plan_clears_missing_job_notice_but_telemetry_does_not(self):
        pending = await self.accept()
        self.h.client.planning_status.side_effect = ShsApiError('job missing', code='planning_job_not_found')
        await self.h._poll_planning_job(pending)
        self.h.client.submit_optimisation.return_value = {}
        self.h._price_quarters = lambda *args: [{'start': self.rig.now.isoformat()}]
        await self.h.async_optimisation_push()
        self.assertIn('pipeline:delivery', self.failures())
        self.assertEqual(self.h.optimisation_plan, self.plan)
        self.h.client.submit_optimisation.return_value = self.published('replacement')
        await self.h.async_optimisation_push(force_plan=True, replan_request_id='replacement')
        self.assertNotIn('pipeline:delivery', self.failures())
        self.assertEqual(self.h.optimisation_plan, self.candidate)

    async def test_configuration_changed_during_solve_accepts_candidate(self):
        pending = await self.accept()
        self.rig.options['another_setting'] = True
        self.h.client.planning_status.return_value = self.published()
        await self.h._poll_planning_job(pending)
        self.assertEqual(self.h.optimisation_plan, self.candidate)
        ack = self.h.client.acknowledge_optimisation_plan.call_args.args
        self.assertEqual(ack[1], 'accepted')
        self.assertIsNone(ack[2])

    async def test_published_plan_survives_configuration_confirmation_failure(self):
        self.h._record_device_exchange.side_effect = ShsApiError('configuration unavailable')
        self.h.client.submit_optimisation.return_value = self.published()
        await self.h.async_optimisation_push(force_plan=True)
        self.assertEqual(self.h.optimisation_plan, self.candidate)
        self.assertIn('pipeline:configuration', self.failures())
        self.assertNotIn('pipeline:acceptance', self.failures())
        self.assertEqual(self.h.client.acknowledge_optimisation_plan.call_args.args[1], 'accepted')
        self.h._record_device_exchange.side_effect = None
        self.h._price_quarters = lambda *args: [{'start': self.rig.now.isoformat()}]
        self.h.client.submit_optimisation.return_value = {}
        await self.h.async_optimisation_push()
        self.assertNotIn('pipeline:configuration', self.failures())

    async def test_older_local_poll_cannot_install_after_new_acceptance(self):
        pending = await self.accept()
        stored = await self.h._store.async_load()
        stored['optimisation_pending_job']['job_id'] = 'new-job'
        await self.h._store.async_save(stored)
        self.h.client.planning_status.return_value = self.published()
        await self.h._poll_planning_job(pending)
        self.assertEqual(self.h.optimisation_plan, self.plan)
        self.assertEqual(self.rig.records.saved['optimisation_pending_job']['job_id'], 'new-job')
        self.h.client.acknowledge_optimisation_plan.assert_not_awaited()

    async def test_acceptance_is_journalled_even_if_configuration_processing_fails(self):
        self.h._record_device_exchange.side_effect = ShsApiError('configuration unavailable')
        await self.h.async_answer_replan('manual')
        self.assertEqual(self.rig.records.saved['optimisation_pending_job']['job_id'], 'job')
        self.assertEqual(self.rig.records.saved['answered_replan_request_id'], 'manual')
        self.h.client.report_replan_failure.assert_not_awaited()
        pending = (await self.h._store.async_read())['optimisation_pending_job']
        self.h.client.planning_status.return_value = self.published()
        await self.h._poll_planning_job(pending)
        self.assertEqual(self.h.optimisation_plan, self.candidate)
        self.assertEqual(self.h.client.acknowledge_optimisation_plan.call_args.args[1], 'accepted')
