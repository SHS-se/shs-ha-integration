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
        await self.journal.flush()
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

    async def test_pool_stale_warning_carries_inspection_and_clears_on_fresh_data(self):
        self.options['device_modes']['$pool'] = 'control_verification'
        self.states['sensor.water'].last_reported = datetime.now(timezone.utc) - timedelta(minutes=16)
        await self.controller.async_start()
        status = self.controller.status['pool']
        self.assertEqual(status['state'], 'fault')
        self.assertTrue(status['retry_automatically'])
        self.assertEqual(status['fix'], {'kind': 'entity', 'entity_id': 'sensor.water'})
        self.assertIn('900 seconds', status['next_step'])
        self.assertIn(self.states['sensor.water'].last_reported.isoformat(), status['reason'])
        row = self.journal.export()['attempts'][0]
        self.assertEqual(row['fix'], status['fix'])
        self.assertEqual(row['reason'], status['reason'])
        self.states['sensor.water'].last_reported = datetime.now(timezone.utc)
        await self.controller.async_tick()
        self.assertEqual(self.controller.status['pool']['state'], 'verified')
        self.assertNotIn('fix', self.controller.status['pool'])
        self.assertEqual(self.calls, [])

    async def test_pool_limited_deferral_remains_visible_in_verification(self):
        self.options['device_modes']['$pool'] = 'control_verification'
        self.states['sensor.water'].state = '4'
        self.slot['pool_w'] = 0
        await self.controller.async_start()
        status = self.controller.status['pool']
        self.assertEqual(status['state'], 'limited')
        self.assertIn('lower limit', status['reason'])
        self.assertIn('cannot enforce', status['next_step'])
        self.assertEqual(self.calls, [])

    async def test_handover_verification_failure_remains_visible_after_successful_plan_commands(self):
        self.options['device_modes']['$pool'] = 'control_verification'
        self.controller.restore = AsyncMock(side_effect=ValueError('handover command rejected'))
        await self.controller.async_start()
        status = self.controller.status['pool']
        self.assertTrue(status['handover_pending'])
        self.assertEqual(status['reason'], 'handover command rejected')
        self.assertNotIn('handover', self.journal.attempts[0]['operations'])
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

    async def test_telemetry_changes_aggregate_but_command_and_failure_transitions_do_not(self):
        self.options['device_modes']['$battery'] = 'control_verification'
        await self.controller.async_start()
        self.audit_store.async_save = AsyncMock(wraps=self.audit_store.async_save)
        self.states['sensor.battery_power'].state = '0.1'
        await self.controller.async_tick()
        row = self.journal.attempts[0]
        self.assertEqual(len(self.journal.attempts), 1)
        self.assertEqual(row['count'], 2)
        self.assertEqual(row['observations']['sensor.battery_power']['state'], '0')
        self.assertEqual(row['last_observations']['sensor.battery_power']['state'], '0.1')
        self.audit_store.async_save.assert_not_awaited()
        self.assertNotIn('slot', row)
        self.assertEqual(self.journal.export()['slots'][row['slot_id']], self.slot)
        self.states['select.mode'].state = 'Charge'
        await self.controller.async_tick()
        self.assertEqual(len(self.journal.attempts), 2, 'would_call changed')
        self.states['sensor.battery_soc'].state = 'unavailable'
        await self.controller.async_tick()
        self.assertEqual(self.journal.attempts[-1]['outcome'], 'blocked')
        self.states['sensor.battery_soc'].state = '50'
        await self.controller.async_tick()
        self.assertEqual(self.journal.attempts[-1]['outcome'], 'verified')
        self.assertEqual(len(self.journal.attempts), 4, 'recovery must not merge across a failure')
        self.assertEqual(len(self.journal.slots), 1)
        self.assertEqual(self.calls, [])

    async def test_repeat_checkpoint_and_shutdown_persist_counts(self):
        from unittest.mock import patch
        self.options['device_modes']['$battery'] = 'control_verification'
        await self.controller.async_start()
        with patch('verification.monotonic', return_value=self.journal.last_saved + 61):
            await self.controller.async_tick()
        self.assertEqual(self.audit_store.saved['attempts'][0]['count'], 2)
        await self.controller.async_tick()
        self.assertEqual(self.audit_store.saved['attempts'][0]['count'], 2)
        await self.controller.async_stop()
        self.assertEqual(self.audit_store.saved['attempts'][0]['count'], 3)

    async def test_existing_journal_is_compacted_once_and_survives_reload(self):
        self.options['device_modes']['$battery'] = 'control_verification'
        await self.controller.async_start()
        original = deepcopy(self.journal.attempts[0])
        original['slot'] = self.journal.slots[original.pop('slot_id')]
        original.pop('count')
        original.pop('last_at')
        later = deepcopy(original)
        later['at'] = '2026-09-11T15:00:00+00:00'
        later['observations']['sensor.battery_power']['state'] = '0.1'
        self.audit_store.saved = {'attempts': [original, later],
                                 'configurations': self.journal.configurations, 'discarded_attempts': 7}
        migrated = VerificationJournal(self.audit_store)
        await migrated.load()
        self.assertEqual(len(migrated.attempts), 1)
        self.assertEqual(migrated.attempts[0]['count'], 2)
        self.assertEqual(migrated.attempts[0]['last_at'], later['at'])
        self.assertEqual(migrated.discarded, 7)
        self.assertEqual(self.audit_store.saved['schema_version'], 2)
        reloaded = VerificationJournal(self.audit_store)
        await reloaded.load()
        self.assertEqual(reloaded.attempts, migrated.attempts)
        self.assertEqual(reloaded.slots, migrated.slots)

    def configure_pool_filter(self):
        from types import SimpleNamespace
        self.options['device_modes']['$pool'] = 'control_verification'
        self.states['sensor.water'].attributes['entity_id'] = 'sensor.raw_water'
        self.states['sensor.water'].last_reported -= timedelta(hours=2)
        self.states['sensor.raw_water'] = fixtures.State(31, unit_of_measurement='°C')
        self.registry = {'sensor.water': SimpleNamespace(platform='filter')}
        self.controller.entity_registry = SimpleNamespace(async_get=self.registry.get)

    async def test_filter_uses_smoothed_value_and_logs_raw_freshness(self):
        self.configure_pool_filter()
        self.slot['pool_w'] = 0
        await self.controller.async_start()
        row = self.journal.attempts[-1]
        self.assertEqual(row['outcome'], 'verified', row)
        self.assertEqual(row['result']['water_temperature_c'], 29)
        self.assertLess(row['result']['stop_temperature_c'], 29)
        self.assertEqual(set(row['observations']) & {'sensor.water', 'sensor.raw_water'},
                         {'sensor.water', 'sensor.raw_water'})
        self.assertEqual(self.calls, [])

    async def test_filter_stale_source_blocks_and_recovers_without_changing_filter(self):
        self.configure_pool_filter()
        self.states['sensor.raw_water'].last_reported -= timedelta(minutes=16)
        await self.controller.async_start()
        status = self.controller.status['pool']
        self.assertEqual(status['state'], 'fault')
        self.assertEqual(status['fix']['entity_id'], 'sensor.raw_water')
        self.assertIn('sensor.raw_water is stale', status['reason'])
        self.states['sensor.raw_water'].last_reported = datetime.now(timezone.utc)
        await self.controller.async_tick()
        self.assertEqual(self.controller.status['pool']['state'], 'verified')

    async def test_filter_unavailable_entities_cannot_be_hidden(self):
        self.configure_pool_filter()
        await self.controller.async_start()
        for entity in ('sensor.water', 'sensor.raw_water'):
            with self.subTest(entity=entity):
                old = self.states[entity].state
                self.states[entity].state = 'unavailable'
                await self.controller.async_tick()
                self.assertEqual(self.controller.status['pool']['state'], 'fault')
                self.assertEqual(self.controller.status['pool']['fix']['entity_id'], entity)
                self.states[entity].state = old
        del self.states['sensor.raw_water']
        await self.controller.async_tick()
        self.assertEqual(self.controller.status['pool']['state'], 'fault')

    async def test_only_registered_filters_follow_source_attributes(self):
        self.configure_pool_filter()
        self.registry['sensor.water'].platform = 'template'
        await self.controller.async_start()
        self.assertIn('sensor.water is stale', self.controller.status['pool']['reason'])
        self.registry.clear()
        await self.controller.async_tick()
        self.assertIn('sensor.water is stale', self.controller.status['pool']['reason'])

    async def test_filter_chains_validate_sources_and_reject_cycles(self):
        from types import SimpleNamespace
        self.configure_pool_filter()
        self.registry['sensor.raw_water'] = SimpleNamespace(platform='filter')
        self.states['sensor.raw_water'].attributes['entity_id'] = 'sensor.actual_water'
        self.states['sensor.raw_water'].last_reported -= timedelta(hours=2)
        self.states['sensor.actual_water'] = fixtures.State(32, unit_of_measurement='°C')
        await self.controller.async_start()
        self.assertEqual(self.controller.status['pool']['state'], 'verified')
        self.states['sensor.raw_water'].attributes['entity_id'] = 'sensor.water'
        await self.controller.async_tick()
        self.assertIn('cycle', self.controller.status['pool']['reason'])
        self.states['sensor.raw_water'].attributes.pop('entity_id')
        await self.controller.async_tick()
        self.assertIn('no valid temperature source', self.controller.status['pool']['reason'])

    async def test_live_filter_source_failure_restores_and_recovers_in_same_slot(self):
        self.configure_pool_filter()
        self.options['device_modes']['$pool'] = 'controlling'
        self.slot['pool_w'] = 0
        await self.controller.async_start()
        self.assertLess(float(self.states['number.stop'].state), 29)
        self.states['sensor.raw_water'].last_reported -= timedelta(minutes=16)
        await self.controller.async_tick()
        self.assertEqual(self.controller.status['pool']['state'], 'fault')
        self.assertEqual(float(self.states['number.stop'].state), 30)
        self.states['sensor.raw_water'].last_reported = datetime.now(timezone.utc)
        await self.controller.async_tick()
        self.assertEqual(self.controller.status['pool']['state'], 'scheduled')
        self.assertLess(float(self.states['number.stop'].state), 29)

    async def test_lifecycle_events_link_attempts_and_distinguish_restart_from_reload(self):
        self.options['device_modes']['$pool'] = 'control_verification'
        await self.controller.async_start(reason='homeassistant_startup')
        start = self.journal.export()['lifecycle_events'][0]
        self.assertEqual(start['reason'], 'homeassistant_startup')
        self.assertEqual(start['event'], 'start')
        self.assertEqual(self.journal.attempts[0]['session_id'], start['session_id'])
        self.assertEqual(self.journal.attempts[0]['integration_version'], start['integration_version'])
        from types import SimpleNamespace
        await self.controller.async_stop(SimpleNamespace(event_type="homeassistant_stop"))
        await self.controller.async_stop()
        self.assertEqual(len(self.journal.events), 2)
        self.assertEqual(self.journal.events[-1]['reason'], 'homeassistant_stop')
        restored = VerificationJournal(self.audit_store)
        await restored.load()
        await restored.lifecycle('start', start['integration_version'], 'integration_load')
        self.assertFalse(restored.events[-1]['previous_session_missing_stop'])
        self.assertNotEqual(restored.session_id, start['session_id'])
        # Identical checks after a restart must form a separate session group.
        row = deepcopy(self.journal.attempts[0])
        row['configuration'] = self.journal.configurations[row['scope']]
        row['slot'] = self.journal.slots[row.pop('slot_id')]
        await restored.append(row)
        self.assertEqual(len(restored.attempts), 2)
        self.assertEqual(restored.attempts[-1]['session_id'], restored.session_id)
        interrupted = VerificationJournal(self.audit_store)
        await interrupted.load()
        await interrupted.lifecycle('start', start['integration_version'], 'integration_load')
        self.assertTrue(interrupted.events[-1]['previous_session_missing_stop'])

    async def test_lifecycle_retention_does_not_remove_control_evidence(self):
        from unittest.mock import patch
        self.options['device_modes']['$pool'] = 'control_verification'
        await self.controller.async_start()
        attempts = deepcopy(self.journal.attempts)
        with patch('verification.MAX_LIFECYCLE_EVENTS', 2):
            await self.journal.lifecycle('stop', 'test', 'integration_unload_or_setup_stop')
            await self.journal.lifecycle('start', 'test', 'integration_load')
        self.assertEqual([event['event'] for event in self.journal.events], ['stop', 'start'])
        self.assertEqual(self.journal.attempts, attempts)


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
               'at': '2026-09-11T14:00:00+00:00', 'slot': {'start': 'old'},
               'expected_operations': ['heat', 'defer', 'handover'], 'operations': ['heat'], 'outcome': 'verified'}
        with patch('verification.MAX_GROUPS', 2):
            await journal.append(row)
            await journal.append(row)
            await journal.append(row)
            await journal.append({**row, 'scope': 'new', 'slot': {'start': 'new'}, 'configuration': {}, 'operations': ['defer']})
            await journal.append({**row, 'scope': 'new', 'slot': {'start': 'new'}, 'configuration': {}, 'operations': ['handover']})
        export = journal.export()
        self.assertEqual(export['retention']['discarded_attempts'], 3)
        self.assertNotIn('old', export['configurations'])
        self.assertEqual(list(export['slots'].values()), [{'start': 'new'}])
        self.assertEqual(export['coverage'][0]['missing'], ['heat'])
        self.assertEqual(export['coverage'][0]['covered'], 2)
