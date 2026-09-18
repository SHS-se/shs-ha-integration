"""All-mode exports report observed execution without inventing passive work."""
from copy import deepcopy
from unittest.mock import AsyncMock, patch
import unittest
import test_controller as fixtures
from controller import ScheduledController
from controller_diagnostics import controller_diagnostics
from verification import VerificationJournal


class ControllerDiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        fixtures.ControllerTests.setUp(self)
        self.audit_store = fixtures.Store()
        self.journal = VerificationJournal(self.audit_store)
        self.controller.verification = self.journal
        self.controller.confirm = ScheduledController.confirm.__get__(self.controller)

    def panel(self, devices=None):
        return {'configuration': deepcopy(self.options), 'devices': devices or [],
                'meter_inventory': [], 'operation': deepcopy(self.coordinator.operational_status),
                'readiness': {'last_plan_error': None}}

    async def test_all_modes_and_unmapped_devices_without_invoking_control(self):
        modes = ('monitoring', 'planning', 'control_verification', 'controlling')
        devices = []
        for i in range(24):
            key = f'sensor.load_{i}'
            self.options['device_modes'][key] = modes[i % 4]
            self.states[key] = fixtures.State(i * 100, unit_of_measurement='W')
            devices.append({'key': key, 'name': f'Load {i}', 'included': False,
                            'permission': {'controller_id': 'device:' + key}})
        self.options['excluded_device_readings'] = ['sensor.load_4']
        panel = self.panel(devices)
        panel['meter_inventory'] = [{'key': 'sensor.unmapped', 'name': 'Unmapped'}]
        self.controller.async_tick = AsyncMock(side_effect=AssertionError('download cannot run controller'))
        report = controller_diagnostics(self.controller, panel)
        rows = {row['key']: row for row in report['current']['devices']}
        self.assertEqual(len(rows), 24)  # 24 - excluded + unmapped; mapping-only entries are separate
        self.assertNotIn('sensor.load_4', rows)
        self.assertEqual({row['mode'] for row in rows.values()}, set(modes))
        self.assertEqual(rows['sensor.load_0']['mode'], 'monitoring')
        self.assertIsNone(rows['sensor.load_0']['last_evaluated_at'])
        self.assertEqual(rows['sensor.load_0']['execution_status']['state'], 'monitoring')
        self.assertEqual({row['key'] for row in report['current']['unassigned_mappings']}, {'charger', 'pool'})
        self.assertEqual(report['current']['observations']['sensor.load_5']['state'], '500')
        self.assertEqual(report['current']['plan']['plan_id'], 'test')
        self.assertEqual(report['evaluations'], [])
        self.assertEqual(report['attempts'], [])
        self.assertEqual(self.calls, [])
        self.assertIsNone(self.audit_store.saved)
        self.controller.async_tick.assert_not_called()
        report['current']['configuration']['device_modes'].clear()
        self.assertTrue(self.options['device_modes'])

    async def test_live_and_simulated_commands_are_separate_and_persisted(self):
        self.options['device_modes'] = {'$battery': 'controlling', '$pool': 'control_verification', '$ev': 'planning'}
        await self.controller.async_start()
        export = self.journal.export()
        self.assertEqual({row['device'] for row in export['attempts']}, {'pool'})
        self.assertEqual({row['device'] for row in export['coverage']}, {'pool'})
        live = next(row for row in export['evaluations'] if row['device'] == 'battery')
        self.assertEqual(live['mode'], 'controlling')
        self.assertEqual(live['result']['state'], 'confirmed')
        self.assertEqual([(c['data']['entity_id'], c['value']) for c in live['commands'] if c['called']], self.calls)
        self.assertTrue(all(c['transport'] == 'accepted' for c in live['commands'] if c['called']))
        self.assertTrue(all(c['settings_confirmation'] == 'not_checked' for c in live['commands']))
        self.assertEqual(live['observations']['select.mode']['state'], 'Baseline')
        # The command no longer polls the select after HA completes its service.
        # Diagnostics retain the last actual read instead of inventing a report.
        self.assertEqual(live['final_observations']['select.mode']['state'], 'Baseline')
        simulated = next(row for row in export['evaluations'] if row['device'] == 'pool')
        self.assertEqual(simulated['commands'], [])
        passive = next(row for row in export['evaluations'] if row['device'] == 'ev')
        self.assertEqual(passive['mode'], 'planning')
        self.assertEqual(passive['commands'], [])
        reloaded = VerificationJournal(self.audit_store)
        await reloaded.load()
        self.assertEqual(reloaded.export()['evaluations'], export['evaluations'])

    async def test_failed_live_send_is_not_reported_as_delivery(self):
        self.options['device_modes']['$battery'] = 'controlling'
        self.hass.services.async_call = AsyncMock(side_effect=TimeoutError('service outcome unknown'))
        await self.controller.async_start()
        row = next(row for row in self.journal.evaluations if row['device'] == 'battery')
        self.assertEqual(row['result']['state'], 'fault')
        sent = [c for c in row['commands'] if c['called']]
        self.assertTrue(sent)
        self.assertTrue(all(c['transport'] == 'ambiguous' for c in sent))
        self.assertTrue(all(c['settings_confirmation'] != 'confirmed' for c in sent))
        self.assertTrue(all(c['error'] == 'service outcome unknown' for c in sent))
        self.assertEqual(self.journal.export()['coverage'], [])

    async def test_diagnostics_failure_does_not_restore_successful_live_control(self):
        self.options['device_modes']['$battery'] = 'controlling'
        await self.controller.async_start()
        self.journal.append = AsyncMock(side_effect=OSError('disk full'))
        self.calls.clear()
        await self.controller.async_tick()
        self.assertEqual(self.controller.status['battery']['state'], 'confirmed')
        self.assertEqual(self.states['select.mode'].state, 'Charge')
        self.assertEqual(self.calls, [])
        self.assertEqual(self.controller.diagnostics_error, 'disk full')
        self.assertEqual(self.controller.diagnostics_failed_evaluations, 3)
        self.assertEqual(controller_diagnostics(self.controller, self.panel())['current']['failed_evaluations_this_session'], 3)

    async def test_mode_exit_records_real_handover_without_fake_verification(self):
        self.states['switch.pool'].state = 'on'
        self.options['device_modes']['$pool'] = 'controlling'
        self.slot['pool_w'] = 0
        await self.controller.async_start()
        self.options['device_modes']['$pool'] = 'monitoring'
        await self.controller.async_tick()
        row = next(row for row in reversed(self.journal.evaluations) if row['device'] == 'pool')
        self.assertEqual(row['mode'], 'monitoring')
        self.assertTrue(any(c['called'] for c in row['commands']))
        self.assertEqual({c['phase'] for c in row['commands']}, {'handover'})
        self.assertIsNone(row['ownership_after'])
        self.assertEqual(self.journal.attempts, [])

    async def test_shutdown_and_startup_handover_are_recorded(self):
        self.options['device_modes']['$pool'] = 'controlling'
        self.slot['pool_w'] = 0
        await self.controller.async_start()
        ownership = deepcopy(self.store.saved)
        await self.controller.async_stop()
        row = next(row for row in reversed(self.journal.evaluations) if row['device'] == 'pool')
        self.assertEqual(row['trigger'], 'shutdown_handover')
        self.assertEqual({c['phase'] for c in row['commands']}, {'handover'})
        self.store.saved = ownership
        controller = ScheduledController(self.hass, self.coordinator, self.store, lambda: deepcopy(self.options), self.journal)
        await controller.async_start()
        row = next(row for row in self.journal.evaluations if row['trigger'] == 'startup_handover')
        self.assertEqual({c['phase'] for c in row['commands']}, {'handover'})
        self.assertIsNone(row['ownership_after'])

    async def test_export_filters_excluded_history_without_destroying_retained_evidence(self):
        self.options['device_modes']['$pool'] = 'control_verification'
        await self.controller.async_start()
        self.options['excluded_device_readings'] = ['pool_meter']
        panel = self.panel([{'key': 'pool_meter', 'system': 'pool', 'permission': {'controller_id': 'pool'}}])
        report = controller_diagnostics(self.controller, panel)
        self.assertEqual(report['attempts'], [])
        self.assertFalse(any(row['device'] == 'pool' for row in report['evaluations']))
        self.assertEqual(report['coverage'], [])
        self.assertTrue(self.journal.attempts)

    async def test_retention_keeps_both_collections_references_and_rolls_forward_v2(self):
        self.options['device_modes'] = {'$pool': 'control_verification', '$battery': 'controlling'}
        with patch('verification.MAX_GROUPS', 2):
            await self.controller.async_start()
            self.slot['pool_w'] = 0
            await self.controller.async_tick()
        report = self.journal.export()
        for row in report['attempts'] + report['evaluations']:
            self.assertIn(row['scope'], report['configurations'])
            self.assertIn(row['slot_id'], report['slots'])
        self.assertLessEqual(len(report['attempts']), 2)
        self.assertLessEqual(len(report['evaluations']), 2)
        saved = deepcopy(self.audit_store.saved)
        saved['schema_version'] = 2
        del saved['evaluations']
        self.audit_store.saved = saved
        upgraded = VerificationJournal(self.audit_store)
        await upgraded.load()
        self.assertEqual(upgraded.evaluations, [])
        self.assertEqual(upgraded.attempts, saved['attempts'])
        self.assertEqual(self.audit_store.saved['schema_version'], 4)

    async def test_verification_links_survive_grouping_and_sessions_have_separate_coverage(self):
        self.options['device_modes']['$pool'] = 'control_verification'
        await self.controller.async_start()
        await self.controller.async_tick()
        await self.controller.async_tick()
        attempts = {row['group_id']: row for row in self.journal.attempts}
        evaluations = [row for row in self.journal.evaluations if row['device'] == 'pool']
        self.assertTrue(any(row['count'] > 1 for row in evaluations))
        for row in evaluations:
            self.assertIn(row['verification_group_id'], attempts)
            self.assertEqual(attempts[row['verification_group_id']]['device'], 'pool')
        first_session = self.journal.session_id
        await self.journal.lifecycle('start', 'test', 'test restart')
        self.slot['pool_w'] = 0
        await self.controller.async_tick()
        panel = self.panel([{'key': 'pool', 'system': 'pool', 'name': 'Pool'}])
        report = controller_diagnostics(self.controller, panel)
        self.assertNotEqual(report['current_session']['session_id'], first_session)
        self.assertEqual(report['current_session']['verification_checks'], 1)
        self.assertEqual(report['historical_summary']['verification_checks'], 3)
        coverage = report['current_session']['coverage'][0]
        self.assertIn('defer', coverage['observed'])
        self.assertNotIn('heat', coverage['observed'])
        self.assertTrue(all(row['verification_link_status'] == 'retained' for row in report['evaluations']))
        self.options['pool_enabled'] = False
        report = controller_diagnostics(self.controller, self.panel([{'key': 'pool', 'system': 'pool'}]))
        self.assertEqual(report['current_session']['current_configuration_coverage'], [])

    async def test_passive_runtime_reason_names_the_mode(self):
        self.options['device_modes']['$ev'] = 'planning'
        await self.controller.async_start()
        status = self.controller.status['ev']
        self.assertEqual(status['state'], 'planning')
        self.assertIn('Planning only', status['reason'])
        self.assertNotIn('overridden', status['reason'])
