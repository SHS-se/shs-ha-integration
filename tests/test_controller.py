"""Exercise real controller sequencing with an in-memory HA service boundary."""
from copy import deepcopy
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import AsyncMock
from types import SimpleNamespace
import sys
import unittest

sys.path.insert(0, str(Path(__file__).parents[1] / 'custom_components' / 'shs_energy'))
from controller import ScheduledController, pool_band
from configuration_schema import resolve_configuration


def battery_command(operation, charge, discharge):
    return {'schema_version': 1, 'operation': operation,
            'charge_limit_w': charge, 'discharge_limit_w': discharge,
            'allow_grid_charge': operation == 'grid_charge',
            'allow_battery_export': operation == 'export'}


class State:
    def __init__(self, value, **attributes):
        self.state = str(value)
        self.attributes = attributes
        self.last_updated = self.last_reported = datetime.now(timezone.utc)


class Store:
    def __init__(self):
        self.saved = None
    async def async_load(self):
        return deepcopy(self.saved)
    async def async_save(self, value):
        self.saved = deepcopy(value)


class ControllerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.states = {
            'number.current': State(8, min=5, max=16, step=1, unit_of_measurement='A'),
            'switch.charge': State('off'),
            'binary_sensor.connected': State('on'),
            'sensor.ev_soc': State(30, unit_of_measurement='%'),
            'sensor.ev_target': State(80, unit_of_measurement='%'),
            'number.start': State(29.5, min=5, max=40, step=.1, unit_of_measurement='°C'),
            'number.stop': State(30, min=5, max=40, step=.1, unit_of_measurement='°C'),
            'sensor.water': State(29, unit_of_measurement='°C'),
            'switch.pool': State('off'),
            'number.charge_limit': State(8.8, min=0, max=100, step=.001, unit_of_measurement='kW'),
            'number.discharge_limit': State(9.6, min=0, max=100, step=.001, unit_of_measurement='kW'),
            'binary_sensor.charging': State('off'), 'binary_sensor.discharging': State('off'),
            'select.mode': State('Baseline', options=['Charge', 'Discharge', 'Hold', 'Baseline']),
            'sensor.battery_power': State(0, unit_of_measurement='kW'),
            'sensor.battery_soc': State(50, unit_of_measurement='%'),
            'switch.authority': State('on'),
            'sensor.authority': State('Remote'),
        }
        self.options = {
            'device_modes': {},
            **{f'{d}_control_enabled': False for d in ('battery', 'ev', 'pool')},
            **{f'{d}_enabled': True for d in ('battery', 'ev', 'pool')},
            'ev_charge_switch_entity': 'switch.charge',
            'ev_connected_entity': 'binary_sensor.connected',
            'ev_soc_entity': 'sensor.ev_soc', 'ev_target_soc_entity': 'sensor.ev_target',
            'device_control_mappings': {'charger': {'control_entity_id': 'number.current', 'control_type': 'variable_power', 'minimum_value': 5, 'maximum_value': 16}},
            'pool_start_temperature_entity': 'number.start', 'pool_stop_temperature_entity': 'number.stop',
            'pool_water_temperature_entity': 'sensor.water', 'pool_permission_entity': 'switch.pool',
            'pool_temperature_minimum': 24, 'pool_temperature_maximum': 32,
            'battery_mode_entity': 'select.mode', 'battery_mode_charge': 'Charge',
            'battery_mode_discharge': 'Discharge', 'battery_mode_idle': 'Hold', 'battery_mode_baseline': 'Baseline',
            'battery_charge_limit_entity': 'number.charge_limit', 'battery_discharge_limit_entity': 'number.discharge_limit',
            'battery_charging_entity': 'binary_sensor.charging', 'battery_discharging_entity': 'binary_sensor.discharging',
            'battery_power_measurement_entity': 'sensor.battery_power', 'battery_soc_entity': 'sensor.battery_soc',
            'battery_capacity_kwh': 18.08, 'battery_min_soc': .05, 'battery_charge_max_w': 8800, 'battery_discharge_max_w': 9600,
            'battery_export_enabled': True, 'battery_export_min_price_sek_per_kwh': 2.5,
            'battery_export_reserve_soc': .2, 'battery_discharge_efficiency': .95,
        }
        self.slot = {'start': '2026-09-08T12:00:00+00:00', 'ev_target_current_a': 10,
                     'ev_min_current_a': 0, 'ev_max_current_a': 16, 'pool_w': 3300,
                     'battery_charge_w': 2000, 'battery_discharge_w': 0,
                     'battery_command': battery_command('grid_charge', 2000, 0),
                     'binding': True, 'export_price_sek_per_kwh': 3,
                     'device_loads_w': {'charger': 6900}}
        self.coordinator = SimpleNamespace(current_plan_slot=self.slot, optimisation_plan={
            'plan_id': 'test', 'schema_version': 8, 'capabilities': dict.fromkeys(('battery', 'ev', 'pool'), True),
            'device_models': [{'key': 'charger', 'category': 'ev_charging', 'control_type': 'variable_power'}]})
        self.coordinator.async_cached_device_configuration = AsyncMock(return_value=[
            {"key": "charger", "control_type": "variable_power", "category": "ev_charging"},
            {"key": "pool", "control_type": "switch_schedule", "category": "pool_heating"}])
        self.coordinator.async_cached_planning_configuration = AsyncMock(return_value={"home": {"battery": {"included": True}}})
        self.calls = []
        self.store = Store()
        async def call(domain, service, data, blocking):
            self.assertTrue(blocking)
            self.assertIsNotNone(self.store.saved, 'journal must precede any write')
            entity = data['entity_id']
            value = data.get('value', data.get('option', service.removeprefix('turn_')))
            self.calls.append((entity, value))
            self.states[entity].state = str(value)
            if entity in ('number.charge_limit', 'number.discharge_limit', 'select.mode'):
                mode = self.states['select.mode'].state
                flow = float(self.states['number.charge_limit'].state) if mode == 'Charge' else -float(self.states['number.discharge_limit'].state) if mode == 'Discharge' else 0
                self.states['sensor.battery_power'].state = str(flow)
                self.states['binary_sensor.charging'].state = 'on' if flow > 0 else 'off'
                self.states['binary_sensor.discharging'].state = 'on' if flow < 0 else 'off'
                for sensor in ('sensor.battery_power', 'binary_sensor.charging', 'binary_sensor.discharging'):
                    self.states[sensor].last_reported = datetime.now(timezone.utc)
        self.hass = SimpleNamespace(states=SimpleNamespace(get=self.states.get), services=SimpleNamespace(async_call=call))
        self.controller = ScheduledController(self.hass, self.coordinator, self.store, lambda: deepcopy(self.options))
        # Timeouts are exercised as refusal rather than sleeping in a test.
        async def confirm(predicate, error):
            self.controller.check_authority()
            if not predicate():
                raise ValueError(error)
        self.controller.confirm = confirm

    async def test_live_battery_limits_block_requests_before_actuator_writes(self):
        self.options['battery_min_soc'] = 'sensor.cutoff'
        self.options['battery_charge_max_w'] = 'sensor.charge_rating'
        self.states['sensor.cutoff'] = State(60, unit_of_measurement='%')
        self.states['sensor.charge_rating'] = State(1, unit_of_measurement='kW')
        discharge = {**self.slot, 'battery_charge_w': 0, 'battery_discharge_w': 1000, 'battery_command': battery_command('supply_house', 0, 1000)}
        with self.assertRaisesRegex(ValueError, 'SOC protection'):
            await self.controller.execute_battery(self.options, discharge)
        self.states['sensor.cutoff'] = State(5, unit_of_measurement='%')
        with self.assertRaisesRegex(ValueError, 'exceeds the current rated power'):
            await self.controller.execute_battery(self.options, self.slot)
        self.assertEqual(self.calls, [])

    async def test_either_power_sign_uses_the_explicit_direction_observations(self):
        for reading in ('2', '-2'):
            self.states['sensor.battery_power'].state = reading
            self.states['binary_sensor.charging'].state = 'on'
            self.states['binary_sensor.discharging'].state = 'off'
            self.assertEqual(self.controller.battery_measurement(self.options), 2000)
            self.states['binary_sensor.charging'].state = 'off'
            self.states['binary_sensor.discharging'].state = 'on'
            self.assertEqual(self.controller.battery_measurement(self.options), -2000)
        self.states['binary_sensor.charging'].state = 'on'
        with self.assertRaisesRegex(ValueError, 'disagree'):
            self.controller.battery_measurement(self.options)

    async def test_sentinel_is_overwritten_and_never_restored(self):
        self.states['number.discharge_limit'].state = '4294967.295'
        self.options['device_modes']['$battery'] = 'controlling'
        await self.controller.async_start()
        self.assertEqual(self.controller.status['battery']['state'], 'confirmed')
        self.coordinator.current_plan_slot = None
        await self.controller.async_tick()
        self.assertEqual(float(self.states['number.charge_limit'].state), 8.8)
        self.assertEqual(float(self.states['number.discharge_limit'].state), 9.6)
        self.assertNotIn(('number.discharge_limit', 4294967.295), self.calls)

    async def test_all_operations_map_to_their_intended_modes_and_ceilings(self):
        self.options['device_modes']['$battery'] = 'controlling'
        await self.controller.async_start()
        for operation, charge, discharge, mode in (
            ('solar_charge', 2000, 0, 'Baseline'),
            ('supply_house', 0, 2000, 'Baseline'),
            ('hold', 0, 0, 'Hold'),
            ('self_consumption', 8800, 9600, 'Baseline'),
            ('grid_charge', 2000, 0, 'Charge'),
            ('export', 0, 2000, 'Discharge'),
        ):
            with self.subTest(operation=operation):
                self.slot.update(battery_charge_w=0 if operation == 'self_consumption' else charge,
                                 battery_discharge_w=0 if operation == 'self_consumption' else discharge,
                                 battery_command=battery_command(operation, charge, discharge))
                await self.controller.async_tick()
                self.assertIn(self.controller.status['battery']['state'], ('confirmed', 'limited'))
                self.assertEqual(self.states['select.mode'].state, mode)
                self.assertEqual(float(self.states['number.charge_limit'].state), charge / 1000)
                self.assertEqual(float(self.states['number.discharge_limit'].state), discharge / 1000)

    async def test_handover_uses_current_rated_sensor_values_after_restart(self):
        self.options['device_modes']['$battery'] = 'controlling'
        self.options.update(battery_charge_max_w='sensor.rated_charge',
                            battery_discharge_max_w='sensor.rated_discharge')
        self.states['sensor.rated_charge'] = State(8.8, unit_of_measurement='kW')
        self.states['sensor.rated_discharge'] = State(9600, unit_of_measurement='W')
        self.states['number.charge_limit'].state = '13'
        await self.controller.async_start()
        self.states['sensor.rated_charge'].state = '8.5'
        self.options['device_modes']['$battery'] = 'planning'
        other = ScheduledController(self.hass, self.coordinator, self.store, lambda: deepcopy(self.options))
        await other.async_start()
        self.assertEqual(self.states['select.mode'].state, 'Baseline')
        self.assertEqual(float(self.states['number.charge_limit'].state), 8.5)
        self.assertEqual(float(self.states['number.discharge_limit'].state), 9.6)
        self.assertFalse(other.records)

    async def test_ambiguous_old_watt_only_plan_cannot_operate_the_battery(self):
        self.options['device_modes']['$battery'] = 'controlling'
        del self.slot['battery_command']
        await self.controller.async_start()
        self.assertEqual(self.calls, [])
        self.assertIn('versioned battery operation', self.controller.status['battery']['reason'])

    async def test_rejected_limit_and_failed_physical_response_report_fault(self):
        self.options['device_modes']['$battery'] = 'controlling'
        original = self.hass.services.async_call
        async def refused(domain, service, data, blocking):
            if data.get('value') == 2:
                return
            await original(domain, service, data, blocking)
        self.hass.services.async_call = refused
        await self.controller.async_start()
        self.assertIn('did not accept', self.controller.status['battery']['reason'])
        self.assertEqual(self.states['select.mode'].state, 'Baseline')
        self.assertEqual(float(self.states['number.charge_limit'].state), 8.8)
        async def no_response(domain, service, data, blocking):
            await original(domain, service, data, blocking)
            self.states['sensor.battery_power'].state = '0'
            self.states['binary_sensor.charging'].state = 'off'
            self.states['binary_sensor.discharging'].state = 'off'
        self.hass.services.async_call = no_response
        self.slot.update(battery_charge_w=3000, battery_command=battery_command('grid_charge', 3000, 0))
        await self.controller.async_tick()
        self.assertIn('did not confirm the requested operation', self.controller.status['battery']['reason'])

    async def test_export_checks_live_permission_price_and_reserve(self):
        await self.controller.async_start()
        self.options['device_modes']['$battery'] = 'controlling'
        self.slot.update(battery_charge_w=0, battery_discharge_w=3000,
                         battery_command=battery_command('export', 0, 3000))
        for updates, message in (({'battery_export_enabled': False}, 'not permitted'),
                                 ({'battery_export_enabled': True, 'battery_export_min_price_sek_per_kwh': 4}, 'not permitted'),
                                 ({'battery_export_min_price_sek_per_kwh': 2.5, 'battery_export_reserve_soc': .8}, 'reserved charge')):
            self.options.update(updates)
            await self.controller.async_tick()
            self.assertEqual(self.calls, [])
            self.assertIn(message, self.controller.status['battery']['reason'])

    async def test_unchanged_battery_request_does_not_cycle_the_limits(self):
        self.options['device_modes']['$battery'] = 'controlling'
        await self.controller.async_start()
        self.calls.clear()
        await self.controller.async_tick()
        self.assertEqual(self.calls, [])
        self.assertEqual(self.controller.status['battery']['state'], 'confirmed')

    async def test_invalid_mode_is_refused_before_any_actuator_write(self):
        self.options['battery_mode_charge'] = 'binary_sensor.charging'
        self.options['device_modes']['$battery'] = 'controlling'
        await self.controller.async_start()
        self.assertEqual(self.calls, [])
        self.assertFalse(self.controller.records)
        self.assertEqual(self.controller.status['battery']['state'], 'fault')

    async def test_all_off_never_writes(self):
        await self.controller.async_start()
        self.assertEqual(self.calls, [])

    async def test_ev_current_precedes_start_and_zero_stops_without_invalid_current(self):
        self.options['device_modes']['$ev'] = 'controlling'
        await self.controller.async_start()
        self.assertEqual(self.calls, [('number.current', 10), ('switch.charge', 'on')])
        self.slot['ev_target_current_a'] = 0
        await self.controller.async_tick()
        self.assertEqual(self.calls[-1], ('switch.charge', 'off'))
        self.assertNotIn(('number.current', 0), self.calls)

    async def test_disable_restores_and_other_devices_remain_disabled(self):
        self.options['device_modes']['$ev'] = 'controlling'
        await self.controller.async_start()
        self.options['device_modes']['$ev'] = 'planning'
        await self.controller.async_tick()
        self.assertEqual(self.states['number.current'].state, '8.0')
        self.assertEqual(self.states['switch.charge'].state, 'off')
        self.assertFalse(self.controller.records)
        self.assertFalse(any('battery' in e or e == 'number.start' for e, _ in self.calls))

    async def test_disconnection_and_soc_completion_stop_charging(self):
        self.options['device_modes']['$ev'] = 'controlling'
        await self.controller.async_start()
        self.states['binary_sensor.connected'].state = 'off'
        await self.controller.async_tick()
        self.assertEqual(self.states['switch.charge'].state, 'off')
        self.states['binary_sensor.connected'].state = 'on'
        self.states['sensor.ev_soc'].state = '80'
        await self.controller.async_tick()
        self.assertEqual(self.states['switch.charge'].state, 'off')

    async def test_plan_expiry_restores_pool_without_recapturing_shifted_band(self):
        self.options['device_modes']['$pool'] = 'controlling'
        self.slot['pool_w'] = 0
        await self.controller.async_start()
        self.assertAlmostEqual(float(self.states['number.stop'].state), 28.9)
        self.assertAlmostEqual(float(self.states['number.start'].state), 28.4)
        self.slot['pool_w'] = 3300
        await self.controller.async_tick()
        self.assertEqual(float(self.states['number.start'].state), 29.5)
        self.coordinator.current_plan_slot = None
        await self.controller.async_tick()
        self.assertEqual(self.states['switch.pool'].state, 'off')
        self.assertFalse(self.controller.records)

    async def test_restart_recovers_journal_before_using_new_mapping(self):
        self.options['device_modes']['$pool'] = 'controlling'
        self.slot['pool_w'] = 0
        await self.controller.async_start()
        self.options['device_modes']['$pool'] = 'planning'
        self.options['rooms'] = {'office': {'temperature_entity_id': 'sensor.new'}}
        other = ScheduledController(self.hass, self.coordinator, self.store, lambda: resolve_configuration(self.options))
        await other.async_start()
        self.assertEqual(float(self.states['number.start'].state), 29.5)
        self.assertEqual(float(self.states['number.stop'].state), 30)
        self.assertFalse(other.records)

    async def test_battery_reversal_closes_ceiling_before_mode_and_positive_limit(self):
        self.options['device_modes']['$battery'] = 'controlling'
        await self.controller.async_start()
        self.calls.clear()
        self.slot.update(battery_charge_w=0, battery_discharge_w=3000, battery_command=battery_command('export', 0, 3000))
        await self.controller.async_tick()
        self.assertEqual(self.calls, [('number.charge_limit', 0), ('select.mode', 'Discharge'), ('number.discharge_limit', 3)])
        self.assertEqual(self.controller.status['battery']['state'], 'confirmed')

    async def test_battery_expiry_restores_both_normal_limits(self):
        self.options['device_modes']['$battery'] = 'controlling'
        await self.controller.async_start()
        self.calls.clear()
        self.coordinator.current_plan_slot = None
        await self.controller.async_tick()
        self.assertEqual(self.calls, [('number.charge_limit', 0), ('select.mode', 'Baseline'), ('number.charge_limit', 8.8), ('number.discharge_limit', 9.6)])

    async def test_failed_mode_confirmation_never_writes_new_power(self):
        self.options['device_modes']['$battery'] = 'controlling'
        await self.controller.async_start()
        self.calls.clear()
        original = self.hass.services.async_call
        async def refuse(domain, service, data, blocking):
            if data.get('option') == 'Discharge':
                return
            await original(domain, service, data, blocking)
        self.hass.services.async_call = refuse
        self.slot.update(battery_charge_w=0, battery_discharge_w=3000, battery_command=battery_command('export', 0, 3000))
        await self.controller.async_tick()
        self.assertNotIn(('number.discharge_limit', 3), self.calls)
        self.assertEqual(self.controller.status['battery']['state'], 'fault')
        self.assertEqual(self.states['select.mode'].state, 'Baseline')

    async def test_battery_soc_floor_restores(self):
        self.options['device_modes']['$battery'] = 'controlling'
        await self.controller.async_start()
        self.slot.update(battery_charge_w=0, battery_discharge_w=3000, battery_command=battery_command('export', 0, 3000))
        self.states['sensor.battery_soc'].state = '5'
        await self.controller.async_tick()
        self.assertIn('SOC protection', self.controller.status['battery']['reason'])

    async def test_invalid_current_fault_is_isolated_from_pool(self):
        self.options['device_modes'].update({'$pool': 'controlling', '$ev': 'controlling'})
        self.slot['ev_target_current_a'] = 19
        await self.controller.async_start()
        self.assertEqual(self.controller.status['ev']['state'], 'fault')
        self.assertEqual(self.controller.status['pool']['state'], 'scheduled')

    async def test_manual_override_releases_and_does_not_reapply(self):
        self.options['device_modes']['$ev'] = 'controlling'
        self.options['ev_control_override_entity'] = 'switch.override'
        self.states['switch.override'] = State('off')
        await self.controller.async_start()
        self.states['switch.override'].state = 'on'
        await self.controller.async_tick()
        count = len(self.calls)
        await self.controller.async_tick()
        self.assertEqual(len(self.calls), count)
        self.assertEqual(self.states['switch.charge'].state, 'off')

    async def test_stop_restores_and_blocks_future_ticks(self):
        self.options['device_modes']['$ev'] = 'controlling'
        await self.controller.async_start()
        await self.controller.async_stop()
        count = len(self.calls)
        await self.controller.async_tick()
        self.assertEqual(len(self.calls), count)
        self.assertEqual(self.states['number.current'].state, '8.0')


    async def test_configuration_change_during_command_prevents_start(self):
        self.options['device_modes']['$ev'] = 'controlling'
        original = self.hass.services.async_call
        async def disable(domain, service, data, blocking):
            await original(domain, service, data, blocking)
            if data['entity_id'] == 'number.current':
                self.options['device_modes']['$ev'] = 'planning'
        self.hass.services.async_call = disable
        await self.controller.async_start()
        self.assertNotIn(('switch.charge', 'on'), self.calls)
        self.assertEqual(self.states['number.current'].state, '8.0')

    async def test_failed_restoration_persists_and_retries_before_new_commands(self):
        self.options['device_modes']['$ev'] = 'controlling'
        await self.controller.async_start()
        self.states['number.current'].state = 'unavailable'
        self.coordinator.current_plan_slot = None
        await self.controller.async_tick()
        self.assertTrue(self.store.saved['records']['ev']['restoration_pending'])
        self.states['number.current'].state = '10'
        await self.controller.async_tick()
        self.assertFalse(self.store.saved['records'])
        self.assertEqual(self.states['number.current'].state, '8.0')

    async def test_stale_water_restores_without_affecting_ev(self):
        self.options['device_modes'].update({'$pool': 'controlling', '$ev': 'controlling'})
        await self.controller.async_start()
        self.states['sensor.water'].last_reported -= timedelta(minutes=3)
        await self.controller.async_tick()
        self.assertEqual(self.controller.status['pool']['state'], 'fault')
        self.assertIn('stale', self.controller.status['pool']['reason'])
        self.assertEqual(self.controller.status['ev']['state'], 'commanded')

    async def test_old_matching_power_is_not_confirmation_of_a_new_command(self):
        self.options['device_modes']['$battery'] = 'controlling'
        await self.controller.async_start()
        original = self.hass.services.async_call
        old_report = self.states['sensor.battery_power'].last_reported
        async def no_new_measurement(domain, service, data, blocking):
            await original(domain, service, data, blocking)
            self.states['sensor.battery_power'].last_reported = old_report
        self.hass.services.async_call = no_new_measurement
        self.slot.update(battery_charge_w=3000, battery_command=battery_command('grid_charge', 3000, 0))
        await self.controller.async_tick()
        self.assertEqual(self.controller.status['battery']['state'], 'fault')
        self.assertIn('did not confirm', self.controller.status['battery']['reason'])

    async def test_disable_reenable_clears_fault_latch(self):
        self.options['device_modes']['$ev'] = 'controlling'
        self.states['binary_sensor.connected'].state = 'unavailable'
        await self.controller.async_start()
        self.assertEqual(self.controller.status['ev']['state'], 'fault')
        self.options['device_modes']['$ev'] = 'planning'
        await self.controller.async_tick()
        self.states['binary_sensor.connected'].state = 'on'
        self.options['device_modes']['$ev'] = 'controlling'
        await self.controller.async_tick()
        self.assertEqual(self.controller.status['ev']['state'], 'commanded')

    def test_pool_clamp_preserves_width_and_never_invents_maximum_target(self):
        self.assertEqual(pool_band((29.5, 30), 29, True, 24, 32, .1), (29.5, 30))
        self.assertEqual(pool_band((29.5, 30), 20, False, 24, 32, .1), (24, 24.5))
        with self.assertRaises(ValueError):
            pool_band((20, 30), 29, False, 24, 32, .1)
    async def test_battery_website_exclusion_restores_even_with_cached_plan(self):
        self.options['device_modes']['$battery'] = 'controlling'
        await self.controller.async_start()
        self.assertIn('battery', self.controller.records)
        self.coordinator.async_cached_planning_configuration.return_value = {'home': {'battery': {'included': False}}}
        self.calls.clear()
        await self.controller.async_tick()
        self.assertEqual(self.states['select.mode'].state, 'Baseline')
        self.assertNotIn('battery', self.controller.records)
        self.assertEqual(self.controller.status['battery']['state'], 'disabled')
        self.calls.clear()
        await self.controller.async_tick()
        self.assertEqual(self.calls, [])

    async def test_vehicle_website_exclusion_restores_without_deleting_setup(self):
        self.options['device_modes']['$ev'] = 'controlling'
        before = deepcopy(self.options)
        await self.controller.async_start()
        self.coordinator.async_cached_device_configuration.return_value = []
        await self.controller.async_tick()
        self.assertEqual(self.states['switch.charge'].state, 'off')
        self.assertEqual(self.options, before)
        self.assertNotIn('ev', self.controller.records)


if __name__ == '__main__':
    unittest.main()
