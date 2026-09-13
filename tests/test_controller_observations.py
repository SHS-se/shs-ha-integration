"""Bounded passive evidence and honest comparisons to household plans."""
import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch
import unittest

import test_controller as fixtures
from controller_observations import diagnostic_inventory, observation_entities
from controller_diagnostics import controller_diagnostics
from verification import VerificationJournal


class ObservationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        fixtures.ControllerTests.setUp(self)
        self.at = datetime(2026, 9, 13, 12, 0, tzinfo=timezone.utc)
        self.audit_store = fixtures.Store()
        self.journal = VerificationJournal(self.audit_store)
        self.journal.session_id = 'session-one'
        self.controller.verification = self.journal
        self.options.update(entities_grid_import=['sensor.grid'], entities_total_consumption=['sensor.total'])
        self.devices = [{'key': 'sensor.load', 'statistic_id': 'sensor.load', 'name': 'Load',
                         'controller_id': 'device:sensor.load', 'mode': 'monitoring',
                         'mapping': {'control_type': 'switch_schedule', 'power': 'sensor.power'}}]
        for entity in ('sensor.grid', 'sensor.total', 'sensor.load'):
            self.states[entity] = fixtures.State(100, unit_of_measurement='kWh', state_class='total_increasing')
        self.states['sensor.power'] = fixtures.State(1, unit_of_measurement='kW')
        for value in self.states.values():
            value.last_reported = value.last_updated = self.at
        self.slot.update(start=self.at.isoformat(), grid_import_w=600, load_w=600)

    async def sample(self, seconds=0):
        self.at += timedelta(seconds=seconds)
        await self.journal.sample(self.options, self.devices, self.states.get, at=self.at,
            version='test', slot=self.slot, plan_id='plan-one')
        return self.journal.samples[-1]

    def report_counters(self, delta=.01):
        for entity in ('sensor.grid', 'sensor.total', 'sensor.load'):
            self.states[entity].state = str(float(self.states[entity].state) + delta)
            self.states[entity].last_reported = self.at + timedelta(seconds=60)

    async def test_passive_power_and_counter_averages_are_distinct_and_time_aligned(self):
        first = await self.sample()
        self.assertIsNone(first['household']['grid_import']['average_w'])
        self.assertEqual(first['device_power']['sensor.load']['value'], 1000)
        self.report_counters()
        second = await self.sample(60)
        grid = second['household']['grid_import']
        self.assertAlmostEqual(grid['average_w'], 600)
        self.assertAlmostEqual(grid['difference_w'], 0)
        self.assertEqual(grid['quality'], 'sampled_interval_average')
        self.assertEqual(second['interval_start'], first['at'])
        self.assertEqual(second['energy_intervals']['sensor.load']['start'], first['at'])
        self.assertEqual(self.journal.evaluations, [])
        self.assertEqual(self.calls, [])

    async def test_bad_counter_evidence_never_becomes_zero_or_a_spike(self):
        for case, expected in [('reset', 'counter_reset'), ('epoch', 'counter_reset'),
                               ('stale', 'stale'), ('unavailable', 'unavailable_or_invalid'),
                               ('unit', 'unsupported_unit'), ('unchanged', 'no_new_report'),
                               ('class', 'unsupported_counter')]:
            with self.subTest(case=case):
                self.setUp()
                await self.sample()
                self.report_counters()
                value = self.states['sensor.grid']
                if case == 'reset': value.state = '0'
                if case == 'epoch': value.attributes['last_reset'] = 'new-epoch'
                if case == 'stale': value.last_reported = self.at - timedelta(minutes=5)
                if case == 'unavailable': value.state = 'unavailable'
                if case == 'unit': value.attributes['unit_of_measurement'] = 'V'
                if case == 'unchanged': value.last_reported = self.at
                if case == 'class': value.attributes['state_class'] = 'measurement'
                row = await self.sample(60)
                self.assertEqual(row['energy_intervals']['sensor.grid']['quality'], expected)
                self.assertIsNone(row['household']['grid_import']['average_w'])
                self.assertIsNone(row['household']['grid_import']['difference_w'])

    async def test_gaps_sessions_and_configuration_changes_do_not_bridge_counters(self):
        for case in ('gap', 'session', 'configuration'):
            with self.subTest(case=case):
                self.setUp()
                await self.sample()
                self.report_counters()
                if case == 'session': self.journal.session_id = 'session-two'
                if case == 'configuration': self.devices[0]['mode'] = 'planning'
                row = await self.sample(180 if case == 'gap' else 60)
                self.assertIsNone(row['household']['grid_import']['average_w'])

    async def test_revised_invalid_and_crossed_slots_do_not_make_plan_comparisons(self):
        for case in ('revised', 'invalid', 'boundary'):
            with self.subTest(case=case):
                self.setUp()
                if case == 'boundary': self.slot['start'] = (self.at - timedelta(minutes=15)).isoformat()
                await self.sample()
                self.report_counters()
                if case == 'revised': self.slot['grid_import_w'] = 700
                if case == 'invalid': self.slot['start'] = 'broken'
                row = await self.sample(60)
                self.assertAlmostEqual(row['household']['grid_import']['average_w'], 600)
                self.assertIsNone(row['household']['grid_import']['planned_w'])
                self.assertFalse(row['plan_comparison_available'])

    async def test_multiple_meters_require_every_configured_source(self):
        self.options['entities_grid_import'].append('sensor.missing')
        await self.sample()
        self.report_counters()
        row = await self.sample(60)
        self.assertEqual(row['household']['grid_import']['quality'], 'incomplete_sources')
        self.assertIsNone(row['household']['grid_import']['average_w'])

    async def test_retention_reload_and_storage_failure_preserve_references(self):
        with patch('verification.MAX_SAMPLES', 2):
            await self.sample()
            self.slot['grid_import_w'] = 701
            await self.sample(60)
            self.devices[0]['mode'] = 'planning'
            await self.sample(60)
        self.assertEqual(len(self.journal.samples), 2)
        self.assertEqual(self.journal.discarded_samples, 1)
        before = self.journal.export()
        self.audit_store.async_save = AsyncMock(side_effect=OSError('disk full'))
        with self.assertRaises(OSError): await self.sample(60)
        self.assertEqual(self.journal.samples, before['samples'])
        restored = VerificationJournal(self.audit_store)
        await restored.load()
        self.assertEqual(restored.samples, before['samples'])
        for row in restored.samples:
            self.assertIn(row['slot_id'], restored.slots)
            self.assertIn(row['context_id'], restored.sample_contexts)

    def test_only_typed_fields_introduce_entity_references(self):
        self.options.update(discovery_evidence={'boiler_power_w': 'discovery_evidence.boiler_power_w'},
                            configuration_reviewed_at={'discovery_evidence.ev_max_current_a': 'now'},
                            grid_export_power_entity='sensor.missing_but_configured')
        self.devices[0]['mapping']['notes'] = 'discovery_evidence.pool_power_w'
        entities = observation_entities(self.options, self.devices)
        self.assertIn('sensor.missing_but_configured', entities)
        self.assertIn('sensor.power', entities)
        self.assertFalse(any(e.startswith('discovery_evidence.') for e in entities))

    def test_mapping_alias_candidates_are_not_extra_devices(self):
        mapping = {'control_type': 'variable_power', 'control_entity_id': 'number.charger', 'power': 'sensor.power'}
        self.options['device_control_mappings'] = {'sensor.car': mapping, 'sensor.old_car': mapping}
        rows, unassigned = diagnostic_inventory([{'key': 'sensor.car', 'category': 'ev_charging', 'control_type': 'variable_power'}], [], self.options)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['controller_id'], 'ev')
        self.assertEqual(unassigned[0]['shares_entities_with'], ['sensor.car'])
        self.assertEqual(unassigned[0]['status'], 'not_in_current_inventory')

    async def test_sampling_all_modes_is_independent_of_actuation_and_excludes_readings(self):
        self.controller.initialized = True
        devices = []
        for i in range(24):
            key = 'sensor.device_' + str(i)
            devices.append({'key': key, 'name': str(i)})
            self.options['device_modes'][key] = ('monitoring', 'planning', 'control_verification', 'controlling')[i % 4]
        self.options['excluded_device_readings'] = ['sensor.device_0']
        self.coordinator.async_cached_planning_configuration.return_value = {'devices': devices}
        self.controller.async_tick = AsyncMock(side_effect=AssertionError('sampling must not evaluate'))
        await self.controller.async_sample_diagnostics()
        self.assertIsNone(self.controller.diagnostics_sampling_error)
        sample = self.journal.samples[-1]
        context = self.journal.sample_contexts[sample['context_id']]
        rows = [d for d in context['devices'] if d['key'].startswith('sensor.device_')]
        self.assertEqual(len(rows), 23)
        self.assertEqual({d['mode'] for d in rows}, {'monitoring', 'planning', 'control_verification', 'controlling'})
        self.assertNotIn('sensor.device_0', sample['observations'])
        self.assertEqual(self.journal.evaluations, [])
        self.assertEqual(self.calls, [])
        self.controller.async_tick.assert_not_called()
        self.journal.sample = AsyncMock(side_effect=OSError('disk full'))
        await self.controller.async_sample_diagnostics()
        self.assertEqual(self.controller.diagnostics_failed_samples, 1)
        self.assertEqual(self.controller.diagnostics_sampling_error, 'disk full')
        self.assertEqual(self.calls, [])

    async def test_export_separates_sessions_and_filters_new_exclusions(self):
        await self.sample()
        self.journal.session_id = 'session-two'
        await self.sample(60)
        self.options['excluded_device_readings'] = ['sensor.load', 'sensor.grid']
        panel = {'configuration': self.options, 'devices': self.devices, 'meter_inventory': [],
                 'operation': {}, 'readiness': {}}
        report = controller_diagnostics(self.controller, panel)
        self.assertEqual(report['current_session']['observation_samples'], 1)
        self.assertEqual(report['historical_summary']['observation_samples'], 1)
        for sample in report['samples']:
            self.assertNotIn('sensor.load', sample['observations'])
            self.assertNotIn('sensor.load', sample['device_power'])
            self.assertNotIn('sensor.grid', sample['energy_intervals'])
            self.assertIsNone(sample['household']['grid_import']['average_w'])
        self.assertTrue(self.journal.samples[0]['observations']['sensor.load'])

    async def test_delayed_counter_report_does_not_become_a_false_power_spike(self):
        self.states['sensor.grid'].last_reported = self.at - timedelta(seconds=120)
        await self.sample()
        self.report_counters(delta=.03)
        sample = await self.sample(60)
        interval = sample['energy_intervals']['sensor.grid']
        self.assertAlmostEqual(interval['source_interval_average_w'], 600)
        self.assertEqual(interval['source_interval_seconds'], 180)
        self.assertEqual(interval['quality'], 'unaligned_source_reports')
        self.assertIsNone(sample['household']['grid_import']['average_w'])
        self.assertIsNone(sample['household']['grid_import']['difference_w'])

    async def test_aligned_but_lagging_sources_do_not_compare_against_the_wrong_slot(self):
        self.states['sensor.grid'].last_reported = self.at - timedelta(seconds=30)
        await self.sample()
        self.report_counters()
        self.states['sensor.grid'].last_reported = self.at + timedelta(seconds=30)
        sample = await self.sample(60)
        self.assertAlmostEqual(sample['household']['grid_import']['average_w'], 600)
        self.assertIsNone(sample['household']['grid_import']['planned_w'])
