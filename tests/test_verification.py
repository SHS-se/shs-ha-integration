"""Verification shares command generation but never acquires physical ownership."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock
import unittest
import test_controller as fixtures
from controller import ScheduledController
from verification import VerificationJournal
from operating_modes import device_mode, planning_devices
from configuration_schema import resolve_configuration
from migration import migrate_options


class VerificationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        fixtures.ControllerTests.setUp(self)
        self.audit_store = fixtures.Store()
        self.journal = VerificationJournal(self.audit_store)
        self.controller.verification = self.journal
        # Use the real confirm implementation: it must skip physical feedback
        # only during verification, not rely on a test mock to do that.
        self.controller.confirm = ScheduledController.confirm.__get__(self.controller)

    async def test_shared_log_captures_pool_and_battery_without_writes(self):
        self.options['device_modes'] = {'$pool': 'control_verification', '$battery': 'control_verification'}
        before = {key: (value.state, dict(value.attributes)) for key, value in self.states.items()}
        await self.controller.async_start()
        self.assertEqual(self.calls, [])
        self.assertFalse(self.controller.records)
        self.assertIsNone(self.store.saved, 'verification must not persist restoration ownership')
        self.assertEqual(before, {key: (value.state, dict(value.attributes)) for key, value in self.states.items()})
        export = self.journal.export()
        self.assertEqual({row['device'] for row in export['attempts']}, {'pool', 'battery'})
        for row in export['attempts']:
            self.assertEqual(row['outcome'], 'verified', row)
            self.assertEqual(row['plan_id'], 'test')
            self.assertEqual(row['slot_start'], self.slot['start'])
            self.assertTrue(row['commands'])
            self.assertEqual({c['phase'] for c in row['commands']}, {'plan', 'handover'})
        battery = next(row for row in export['attempts'] if row['device'] == 'battery')
        mode = next(c for c in battery['commands'] if c['service'] == 'select_option' and c['phase'] == 'plan')
        self.assertEqual(mode['data'], {'entity_id': 'select.mode', 'option': 'Charge'})
        self.assertIn('export', next(c for c in export['coverage'] if c['device'] == 'battery')['missing'])
        await self.controller.async_tick()
        self.assertEqual(len(self.journal.attempts), 2, 'identical attempts in the same quarter are deduplicated')
        restored = VerificationJournal(self.audit_store)
        await restored.load()
        self.assertEqual(restored.attempts, self.journal.attempts)

    async def test_verified_commands_equal_live_service_calls(self):
        self.options['device_modes']['$battery'] = 'control_verification'
        await self.controller.async_start()
        dry = self.journal.attempts[0]
        expected = [(c['data']['entity_id'], c['value']) for c in dry['commands'] if c['phase'] == 'plan' and c['would_call']]
        self.options['device_modes']['$battery'] = 'controlling'
        await self.controller.async_tick()
        self.assertEqual(self.calls, expected)
        self.assertEqual(self.controller.status['battery']['state'], 'confirmed')

    async def test_stale_sources_and_invalid_modes_are_logged_as_blocked(self):
        self.options['device_modes']['$battery'] = 'control_verification'
        self.states['binary_sensor.charging'].last_reported = datetime.now(timezone.utc) - timedelta(minutes=3)
        await self.controller.async_start()
        row = self.journal.attempts[0]
        self.assertEqual(row['outcome'], 'blocked')
        self.assertIn('stale', row['reason'])
        self.assertFalse(row['commands'])
        self.assertFalse(self.journal.export()['coverage'][0]['observed'])
        self.assertEqual(self.calls, [])

    async def test_slot_expiry_stops_verification_without_restoration_calls(self):
        self.options['device_modes']['$pool'] = 'control_verification'
        await self.controller.async_start()
        self.coordinator.current_plan_slot = None
        await self.controller.async_tick()
        await self.controller.async_stop()
        self.assertEqual(self.calls, [])
        self.assertFalse(self.controller.records)

    async def test_leaving_live_control_restores_before_verifying(self):
        self.options['device_modes']['$pool'] = 'controlling'
        self.slot['pool_w'] = 0
        await self.controller.async_start()
        self.assertLess(float(self.states['number.start'].state), 29.5)
        self.options['device_modes']['$pool'] = 'control_verification'
        await self.controller.async_tick()
        self.assertEqual(float(self.states['number.start'].state), 29.5)
        self.assertFalse(self.controller.records)
        after = len(self.calls)
        await self.controller.async_tick()
        self.assertEqual(len(self.calls), after)

    async def test_audit_write_failure_is_visible_and_never_writes_hardware(self):
        self.options['device_modes']['$pool'] = 'control_verification'
        self.audit_store.async_save = AsyncMock(side_effect=OSError('disk full'))
        await self.controller.async_start()
        self.assertEqual(self.controller.status['pool']['state'], 'fault')
        self.assertIn('disk full', self.controller.status['pool']['reason'])
        self.assertEqual(self.calls, [])
        self.assertEqual(self.journal.attempts, [])

    async def test_pool_setpoint_water_mapping_has_system_ownership(self):
        self.options['device_modes']['$pool'] = 'control_verification'
        self.options['device_control_mappings']['pool'] = {'control_type': 'setpoint', 'temperature_entity_id': 'sensor.water'}
        self.coordinator.async_cached_device_configuration.return_value[1]['control_type'] = 'setpoint'
        await self.controller.async_start()
        self.assertEqual(self.controller.status['pool']['state'], 'verified')

    async def test_new_quarter_and_mapping_changes_have_separate_evidence(self):
        self.options['device_modes']['$pool'] = 'control_verification'
        await self.controller.async_start()
        self.slot['start'] = '2026-09-08T12:15:00+00:00'
        await self.controller.async_tick()
        self.assertEqual(len(self.journal.attempts), 2)
        self.states['switch.other_pool'] = fixtures.State('off')
        self.options['pool_permission_entity'] = 'switch.other_pool'
        await self.controller.async_tick()
        self.assertEqual(len(self.journal.export()['coverage']), 2)

    async def test_verifying_one_device_does_not_restore_another_live_controller(self):
        self.options['device_modes']['$battery'] = 'controlling'
        await self.controller.async_start()
        self.assertEqual(self.states['select.mode'].state, 'Charge')
        self.calls.clear()
        self.options['device_modes']['$pool'] = 'control_verification'
        await self.controller.async_tick()
        self.assertEqual(self.calls, [])
        self.assertEqual(self.states['select.mode'].state, 'Charge')
        self.assertIn('battery', self.controller.records)
        self.assertNotIn('pool', self.controller.records)

    async def test_ev_verifies_current_then_switch_and_handover_without_writes(self):
        self.options['device_modes']['$ev'] = 'control_verification'
        await self.controller.async_start()
        row = self.journal.attempts[0]
        self.assertEqual(row['outcome'], 'verified', row)
        commands = [c['data']['entity_id'] for c in row['commands'] if c['phase'] == 'plan']
        self.assertEqual(commands, ['number.current', 'switch.charge'])
        self.assertEqual(self.calls, [])
        self.slot['ev_target_current_a'] = 0
        await self.controller.async_tick()
        self.assertEqual(self.journal.export()['coverage'][0]['missing'], [])

    async def test_generic_permit_and_inhibit_use_shared_verification_log(self):
        import test_device_commands
        test_device_commands.DeviceExecutionTests.setUp(self)
        self.audit_store = fixtures.Store()
        self.journal = VerificationJournal(self.audit_store)
        self.controller.verification = self.journal
        self.controller.confirm = ScheduledController.confirm.__get__(self.controller)
        self.options['device_modes']['heater'] = 'control_verification'
        await self.controller.async_start()
        self.assertEqual(self.journal.attempts[0]['outcome'], 'verified', self.journal.attempts)
        self.assertEqual(self.states['switch.heater'].state, 'on')
        self.slot['device_commands']['heater']['permitted'] = True
        await self.controller.async_tick()
        self.assertEqual(self.journal.export()['coverage'][0]['missing'], [])
        self.assertEqual(self.calls, [])


class ModeTests(unittest.TestCase):
    def test_old_booleans_cannot_authorize_and_modes_derive_planning(self):
        options = resolve_configuration({'planning_mode': 'live', 'battery_control_enabled': True})
        self.assertEqual(options['planning_mode'], 'disabled')
        self.assertFalse(options['battery_control_enabled'])
        self.assertEqual(device_mode(options, 'battery'), 'monitoring')
        options = resolve_configuration({'device_modes': {'$pool': 'planning', '$battery': 'control_verification'}})
        self.assertEqual(options['planning_mode'], 'live')
        self.assertFalse(options['pool_control_enabled'])
        self.assertFalse(options['battery_control_enabled'])

    def test_monitoring_keeps_meter_but_removes_planning_role(self):
        devices = [{'key': 'pool', 'category': 'pool_heating', 'control_type': 'setpoint', 'planning_role': 'controllable'},
                   {'key': 'heater', 'category': 'heating', 'control_type': 'switch_schedule', 'planning_role': 'controllable'}]
        options = {'pool_water_temperature_entity': 'sensor.water', 'device_modes': {'heater': 'planning'},
                   'device_control_mappings': {'pool': {'temperature_entity_id': 'sensor.water'}}}
        result = planning_devices(devices, options)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]['planning_role'], 'base_load')
        self.assertIsNone(result[0]['control_type'])
        self.assertEqual(result[1]['planning_role'], 'controllable')
        self.assertEqual(devices[0]['planning_role'], 'controllable')

    def test_one_time_migration_preserves_permission_without_escalation(self):
        options = {'planning_mode': 'live', 'battery_control_enabled': False, 'pool_control_enabled': True,
                   'device_control_mappings': {'heater': {'control_type': 'switch_schedule', 'control_enabled': False}}}
        migrated, _ = migrate_options(options, source_version=11)
        self.assertEqual(migrated['device_modes']['$pool'], 'controlling')
        self.assertEqual(migrated['device_modes']['$battery'], 'planning')
        self.assertEqual(migrated['device_modes']['heater'], 'planning')
        self.assertNotIn('planning_mode', migrated)
        self.assertNotIn('control_enabled', migrated['device_control_mappings']['heater'])
        options['planning_mode'] = 'disabled'
        migrated, _ = migrate_options(options, source_version=11)
        self.assertEqual(set(migrated['device_modes'].values()), {'monitoring'})

    def test_monitoring_and_planning_are_independent_for_two_pool_meters(self):
        devices = [{'key': 'heater', 'category': 'pool_heating', 'control_type': 'setpoint', 'planning_role': 'controllable'},
                   {'key': 'pump', 'category': 'pool_heating', 'control_type': 'switch_schedule', 'planning_role': 'controllable'}]
        options = {'pool_water_temperature_entity': 'sensor.water', 'device_modes': {'$pool': 'planning'},
                   'device_control_mappings': {'heater': {'temperature_entity_id': 'sensor.water'}}}
        result = planning_devices(devices, options)
        self.assertEqual(result[0]['planning_role'], 'controllable')
        self.assertEqual(result[1]['planning_role'], 'base_load')


class JournalRetentionTests(unittest.IsolatedAsyncioTestCase):
    async def test_retention_prunes_old_coverage_and_reports_discarded_attempts(self):
        from unittest.mock import patch
        journal = VerificationJournal(fixtures.Store())
        row = {'device': 'pool', 'scope': 'old', 'configuration': {'old': True},
               'expected_operations': ['heat', 'defer', 'handover'], 'operations': ['heat'], 'outcome': 'verified'}
        with patch('verification.MAX_ATTEMPTS', 2):
            await journal.append(row)
            await journal.append({**row, 'scope': 'new', 'configuration': {}, 'operations': ['defer']})
            await journal.append({**row, 'scope': 'new', 'configuration': {}, 'operations': ['handover']})
        export = journal.export()
        self.assertEqual(export['retention']['discarded_attempts'], 1)
        self.assertNotIn('old', export['configurations'])
        self.assertEqual(export['coverage'][0]['missing'], ['heat'])
        self.assertEqual(export['coverage'][0]['covered'], 2)
