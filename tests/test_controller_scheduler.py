"""Exercise the event scheduler with real controllers and a virtual timer clock."""
import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch
import unittest

import test_controller as fixtures
from controller import ScheduledController
from controller_scheduler import ControllerScheduler
from verification import VerificationJournal


class SchedulerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        fixtures.ControllerTests.setUp(self)
        self.now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        for state in self.states.values():
            state.last_updated = state.last_reported = self.now
        owner = self

        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return owner.now

        self.clock_patch = patch('controller.datetime', Clock)
        self.clock_patch.start()
        self.addCleanup(self.clock_patch.stop)
        self.subscriptions = {}
        self.timers = {}
        self.next_timer = 0
        self.journal = VerificationJournal(fixtures.Store())
        self.controller.verification = self.journal
        service = self.hass.services.async_call

        async def clocked_service(domain, name, data, blocking):
            await service(domain, name, data, blocking)
            if data['entity_id'] in {'number.charge_limit', 'number.discharge_limit', 'select.mode'}:
                for entity in ('sensor.battery_power', 'binary_sensor.charging', 'binary_sensor.discharging'):
                    self.states[entity].last_reported = self.now

        self.hass.services.async_call = clocked_service
        self.options['device_modes'] = {'$battery': 'control_verification', '$pool': 'control_verification'}
        self.coordinator.optimisation_plan['valid_until'] = (self.now + timedelta(hours=2)).isoformat()
        self.scheduler = ControllerScheduler(self.controller, self.subscribe, self.at,
                                            asyncio.create_task, now=lambda: self.now)
        self.addCleanup(self.scheduler.close)

    def subscribe(self, entity, notify):
        self.subscriptions[entity] = notify
        return lambda: self.subscriptions.pop(entity, None)

    def at(self, when, action):
        self.next_timer += 1
        token = self.next_timer
        self.timers[token] = (when, action)
        return lambda: self.timers.pop(token, None)

    def advance(self, seconds):
        target = self.now + timedelta(seconds=seconds)
        while self.timers:
            token = min(self.timers, key=lambda key: self.timers[key][0])
            when, action = self.timers[token]
            if when > target:
                break
            self.now = when
            del self.timers[token]
            action(when)
        self.now = target

    def event(self, entity, kind='state_change'):
        state = self.states.get(entity)
        if state is not None:
            state.last_reported = self.now
        if entity in self.subscriptions:
            self.subscriptions[entity](entity, state, kind)

    async def drain(self):
        if self.scheduler.task is not None:
            await self.scheduler.task

    def evaluations(self, device):
        return self.controller.metrics.snapshot()['devices'][device]['completed']

    async def start_live_pool(self):
        self.options['device_modes'] = {'$pool': 'controlling'}
        self.slot['start'] = self.now.isoformat()
        self.slot['pool_w'] = 0
        await self.controller.async_start()
        self.calls.clear()

    async def test_live_plan_replacement_queues_reconciliation_without_handover(self):
        self.options['device_modes'] = {'$battery': 'controlling'}
        self.slot['start'] = self.now.isoformat()
        self.slot.update(battery_charge_w=0, battery_discharge_w=0,
                         battery_command=fixtures.battery_command('hold', 0, 0))
        await self.controller.async_start()
        self.calls.clear()
        execute = self.controller.execute_battery
        replaced = False

        async def revise(options, slot):
            nonlocal replaced
            if not replaced:
                replaced = True
                self.coordinator.current_plan_slot = {**deepcopy(slot), 'base_w': 1234}
                self.controller.check_authority()
            return await execute(options, slot)

        self.controller.execute_battery = revise
        await self.controller.async_tick()
        await self.drain()
        self.assertEqual(self.controller.status['battery']['state'], 'confirmed')
        self.assertEqual(self.calls, [])
        self.assertIn('plan_replaced', self.controller.metrics.triggers)
        self.assertIn('battery', self.controller.records)

    async def test_short_pool_temperature_gaps_do_not_restore_or_reapply(self):
        await self.start_live_pool()
        band = (self.states['number.start'].state, self.states['number.stop'].state)
        for seconds in (5, 6):
            self.states['sensor.water'].state = 'unavailable'
            self.event('sensor.water')
            await self.drain()
            self.assertEqual(self.controller.status['pool']['state'], 'limited')
            self.assertIn(('pool', 'temperature_gap'), self.scheduler.deadlines)
            self.advance(seconds)
            self.states['sensor.water'].state = '29'
            self.event('sensor.water')
            await self.drain()
            self.assertNotIn(('pool', 'temperature_gap'), self.scheduler.deadlines)
            self.assertEqual(self.controller.status['pool']['state'], 'scheduled')
        self.assertEqual(self.calls, [])
        self.assertEqual(band, (self.states['number.start'].state, self.states['number.stop'].state))

    async def test_pool_gap_deadline_is_not_renewed_and_restores_without_reports(self):
        await self.start_live_pool()
        self.states['sensor.water'].state = 'unavailable'
        self.event('sensor.water')
        await self.drain()
        deadline = self.scheduler.deadlines[('pool', 'temperature_gap')][0]
        self.advance(6)
        self.event('sensor.water')
        await self.drain()
        self.assertEqual(self.scheduler.deadlines[('pool', 'temperature_gap')][0], deadline)
        self.advance(9)
        await self.drain()
        self.assertEqual(self.states['number.start'].state, '29.5')
        self.assertEqual(self.states['number.stop'].state, '30')
        self.assertNotIn('pool', self.controller.records)
        count = len(self.calls)
        self.event('sensor.water')
        await self.drain()
        self.assertEqual(len(self.calls), count)
        self.assertNotIn(('pool', 'temperature_gap'), self.scheduler.deadlines)

    async def test_pool_gap_respects_freshness_slot_and_plan_expiry(self):
        for bound in ('freshness', 'slot', 'plan'):
            with self.subTest(bound=bound):
                fixtures.ControllerTests.setUp(self)
                self.options['device_modes'] = {'$pool': 'controlling'}
                for state in self.states.values():
                    state.last_reported = self.now
                self.slot['start'] = (self.now - timedelta(seconds=895) if bound == 'slot' else self.now).isoformat()
                self.slot['pool_w'] = 0
                self.coordinator.optimisation_plan['valid_until'] = (self.now + timedelta(seconds=5 if bound == 'plan' else 900)).isoformat()
                if bound == 'freshness':
                    self.states['sensor.water'].last_reported = self.now - timedelta(seconds=895)
                self.scheduler.close()
                self.scheduler = ControllerScheduler(self.controller, self.subscribe, self.at, asyncio.create_task, now=lambda: self.now)
                self.addCleanup(self.scheduler.close)
                await self.controller.async_start()
                self.calls.clear()
                self.states['sensor.water'].state = 'unavailable'
                self.event('sensor.water')
                await self.drain()
                self.assertEqual(self.scheduler.deadlines[('pool', 'temperature_gap')][0], self.now + timedelta(seconds=5))
                self.advance(5)
                await self.drain()
                self.assertNotIn('pool', self.controller.records)

    async def test_pool_gap_does_not_cover_changed_request_or_missing_actuator(self):
        await self.start_live_pool()
        self.states['sensor.water'].state = 'unavailable'
        self.event('sensor.water')
        await self.drain()
        self.slot['pool_w'] = 3300
        await self.controller.async_tick()
        self.assertNotIn('pool', self.controller.records)
        self.assertEqual(self.controller.status['pool']['state'], 'fault')
        self.assertNotIn(('pool', 'temperature_gap'), self.scheduler.deadlines)
        self.states['sensor.water'].state = '29'
        self.event('sensor.water')
        await self.drain()
        self.states['switch.pool'].state = 'unavailable'
        self.event('switch.pool')
        await self.drain()
        self.assertTrue(self.controller.records['pool']['restoration_pending'])
        self.assertNotIn(('pool', 'temperature_gap'), self.scheduler.deadlines)

    async def test_pool_gap_does_not_cover_bad_units(self):
        await self.start_live_pool()
        self.states['sensor.water'].attributes['unit_of_measurement'] = '°F'
        self.event('sensor.water')
        await self.drain()
        self.assertNotIn('pool', self.controller.records)
        self.assertNotIn(('pool', 'temperature_gap'), self.scheduler.deadlines)

    async def test_filter_gap_retains_raw_dependency_and_rejects_source_change(self):
        self.controller.entity_registry = SimpleNamespace(async_get=lambda entity:
            SimpleNamespace(platform='filter') if entity == 'sensor.water' else None)
        self.states['sensor.water'].attributes['entity_id'] = 'sensor.raw'
        self.states['sensor.raw'] = fixtures.State(29, unit_of_measurement='°C')
        self.states['sensor.raw'].last_reported = self.now
        await self.start_live_pool()
        self.states['sensor.water'].state = 'unavailable'
        self.event('sensor.water')
        await self.drain()
        self.assertIn('sensor.raw', self.subscriptions)
        self.assertEqual(self.calls, [])
        self.states['sensor.raw'].attributes['unit_of_measurement'] = '°F'
        self.event('sensor.raw')
        await self.drain()
        self.assertNotIn('pool', self.controller.records)

    async def test_pool_gap_ends_when_control_is_disabled(self):
        await self.start_live_pool()
        self.states['sensor.water'].state = 'unavailable'
        self.event('sensor.water')
        await self.drain()
        self.options['device_modes']['$pool'] = 'planning'
        await self.controller.async_tick()
        self.assertNotIn('pool', self.controller.records)
        self.assertNotIn(('pool', 'temperature_gap'), self.scheduler.deadlines)

    async def test_partial_write_cannot_enter_temperature_gap(self):
        from controller import ControlObservationError
        await self.start_live_pool()
        original = self.hass.services.async_call
        failed = False

        async def fail_after_write(domain, name, data, blocking):
            nonlocal failed
            await original(domain, name, data, blocking)
            if not failed:
                failed = True
                self.states['sensor.water'].state = 'unavailable'
                raise ControlObservationError('temperature lost during write', 'sensor.water', 'check source', unavailable=True)

        self.hass.services.async_call = fail_after_write
        self.slot['pool_w'] = 3300
        self.states['sensor.water'].state = '28'
        self.event('sensor.water')
        await self.drain()
        self.assertTrue(failed)
        self.assertNotIn('pool', self.controller.records)
        self.assertNotIn(('pool', 'temperature_gap'), self.scheduler.deadlines)
        self.assertEqual(self.states['number.start'].state, '29.5')

    async def test_stale_raw_source_cannot_hide_behind_missing_filter(self):
        self.controller.entity_registry = SimpleNamespace(async_get=lambda entity:
            SimpleNamespace(platform='filter') if entity == 'sensor.water' else None)
        self.states['sensor.water'].attributes['entity_id'] = 'sensor.raw'
        self.states['sensor.raw'] = fixtures.State(29, unit_of_measurement='°C')
        self.states['sensor.raw'].last_reported = self.now
        await self.start_live_pool()
        self.states['sensor.water'].state = 'unavailable'
        self.states['sensor.raw'].last_reported = self.now - timedelta(seconds=901)
        self.event('sensor.water')
        await self.drain()
        self.assertNotIn('pool', self.controller.records)
        self.assertNotIn(('pool', 'temperature_gap'), self.scheduler.deadlines)

    async def test_verification_cannot_seed_a_live_pool_gap(self):
        self.options['device_modes'] = {'$pool': 'control_verification'}
        self.slot['start'] = self.now.isoformat()
        await self.controller.async_start()
        self.assertIsNone(self.controller.pool_observation)
        self.options['device_modes']['$pool'] = 'controlling'
        self.states['sensor.water'].state = 'unavailable'
        self.event('sensor.water')
        await self.drain()
        self.assertNotIn(('pool', 'temperature_gap'), self.scheduler.deadlines)
        self.assertEqual(self.calls, [])

    async def test_quiet_controller_has_no_five_second_sweep_and_changes_are_scoped(self):
        await self.controller.async_start()
        self.advance(5)
        await self.drain()
        self.assertEqual(self.evaluations('pool'), 1)
        for n in range(10):
            self.states['sensor.water'].state = str(28 + n / 10)
            self.event('sensor.water')
        await self.drain()
        self.assertEqual(self.evaluations('pool'), 2)
        self.assertEqual(self.evaluations('battery'), 1)
        stats = self.controller.metrics.snapshot()['scheduling']
        self.assertEqual(stats['state_change_events'], 10)
        self.assertEqual(stats['coalesced_notifications'], 9)
        self.assertEqual(stats['device_dispatches'], 1)
        self.assertEqual(self.calls, [])

    async def test_unchanged_reports_extend_freshness_without_decisions_and_recover_staleness(self):
        self.options['device_modes']['$battery'] = 'planning'
        await self.controller.async_start()
        for _ in range(30):
            self.advance(1)
            self.event('sensor.water', 'state_report')
        await self.drain()
        self.assertEqual(self.evaluations('pool'), 1)
        expiry = self.scheduler.deadlines[('pool', 'freshness:sensor.water')][0]
        self.assertEqual(expiry, self.now + timedelta(seconds=900, milliseconds=1))
        self.advance(901)
        await self.drain()
        self.assertEqual(self.controller.status['pool']['state'], 'fault')
        self.assertIn('stale', self.controller.status['pool']['reason'])
        self.event('sensor.water', 'state_report')
        await self.drain()
        self.assertEqual(self.controller.status['pool']['state'], 'verified')
        self.assertEqual(self.calls, [])

    async def test_busy_events_are_coalesced_and_not_lost(self):
        await self.controller.async_start()
        async with self.controller.lock:
            self.states['sensor.water'].state = '28'
            self.event('sensor.water')
            self.event('sensor.water')
            await asyncio.sleep(0)
            self.assertEqual(self.evaluations('pool'), 1)
        await self.drain()
        self.assertEqual(self.evaluations('pool'), 2)
        self.assertEqual(self.controller.metrics.scheduling['queued_while_busy'], 2)

    async def test_event_arriving_during_evaluation_gets_a_followup(self):
        await self.controller.async_start()
        entered, release = asyncio.Event(), asyncio.Event()
        append = self.journal.append

        async def delayed(attempt, **kwargs):
            if not entered.is_set():
                entered.set()
                await release.wait()
            return await append(attempt, **kwargs)

        self.journal.append = delayed
        self.event('sensor.water')
        await entered.wait()
        self.states['sensor.water'].state = '27'
        self.event('sensor.water')
        release.set()
        await self.drain()
        self.assertEqual(self.evaluations('pool'), 3)
        self.assertEqual(self.evaluations('battery'), 1)

    async def test_expiry_and_slot_deadlines_work_without_cloud_updates(self):
        self.coordinator.optimisation_plan['valid_until'] = (self.now + timedelta(seconds=10)).isoformat()
        await self.controller.async_start()
        self.coordinator.current_plan_slot = None
        self.advance(11)
        await self.drain()
        self.assertEqual(self.controller.status['pool']['state'], 'idle')
        self.assertIn('plan_expiry', self.controller.metrics.triggers)
        self.advance(900)
        await self.drain()
        self.assertIn('slot_boundary', self.controller.metrics.triggers)

    async def test_filter_source_changes_replace_dependencies(self):
        self.controller.entity_registry = SimpleNamespace(async_get=lambda entity:
            SimpleNamespace(platform='filter') if entity == 'sensor.water' else None)
        self.states['sensor.water'].attributes['entity_id'] = 'sensor.raw_old'
        self.states['sensor.raw_old'] = fixtures.State(29, unit_of_measurement='°C')
        self.states['sensor.raw_old'].last_reported = self.now
        await self.controller.async_start()
        self.assertIn('sensor.raw_old', self.subscriptions)
        self.states['sensor.raw_new'] = fixtures.State(29, unit_of_measurement='°C')
        self.states['sensor.raw_new'].last_reported = self.now
        self.states['sensor.water'].attributes['entity_id'] = 'sensor.raw_new'
        self.event('sensor.water', 'registry')
        await self.drain()
        self.assertNotIn('sensor.raw_old', self.subscriptions)
        self.assertIn('sensor.raw_new', self.subscriptions)

    async def test_event_driven_confirmation_wakes_for_unchanged_report_and_times_out(self):
        await self.controller.async_start()
        self.controller.confirm = ScheduledController.confirm.__get__(self.controller)
        self.controller.check_authority = lambda: None
        ready = False
        task = asyncio.create_task(self.controller.confirm(lambda: ready, 'no response'))
        await asyncio.sleep(0)
        ready = True
        self.event('sensor.water', 'state_report')
        await asyncio.wait_for(task, 0.1)
        self.assertEqual(self.evaluations('pool'), 1)
        with patch('controller.CONFIRM_SECONDS', 0.01):
            with self.assertRaisesRegex(ValueError, 'no response'):
                await self.controller.confirm(lambda: False, 'no response')

    async def test_shutdown_removes_subscriptions_deadlines_and_pending_work(self):
        await self.controller.async_start()
        self.event('sensor.water')
        await self.controller.async_stop()
        await self.drain()
        self.assertFalse(self.subscriptions)
        self.assertFalse(self.timers)
        self.assertFalse(self.scheduler.pending)
        self.assertEqual(self.calls, [])

    def configure_heater(self, kind='permit_inhibit'):
        self.options['device_modes'] = {'heater': 'controlling'}
        self.states['switch.heater'] = fixtures.State('on')
        self.states['switch.heater'].last_changed = self.now
        self.options['device_control_mappings']['heater'] = {
            'control_type': kind, 'actuator_entity_ids': ['switch.heater'],
            'max_inhibit_slots': 2, 'minimum_on_seconds': 60, 'minimum_off_seconds': 60,
        }
        self.coordinator.optimisation_plan.update(schema_version=7, device_models=[{'key': 'heater', 'control_type': kind}])
        self.coordinator.async_cached_device_configuration.return_value = [{'key': 'heater', 'control_type': kind}]
        self.slot['device_commands'] = {'heater': ({'type': kind, 'permitted': False} if kind == 'permit_inhibit'
                                                  else {'type': kind, 'on_seconds': 0})}

    async def test_maximum_inhibit_is_enforced_at_its_deadline(self):
        self.configure_heater()
        await self.controller.async_start()
        self.assertEqual(self.states['switch.heater'].state, 'off')
        self.assertIn(('device:heater', 'maximum_inhibit'), self.scheduler.deadlines)
        self.advance(1800)
        await self.drain()
        self.assertEqual(self.states['switch.heater'].state, 'on')
        self.assertIn('maximum continuous inhibit', self.controller.status['device:heater']['reason'])

    async def test_omitted_timings_allow_initial_switching_transitions_and_restoration(self):
        self.configure_heater('switch_schedule')
        mapping = self.options['device_control_mappings']['heater']
        del mapping['minimum_on_seconds']
        mapping['minimum_off_seconds'] = None
        del self.states['switch.heater'].last_changed
        await self.controller.async_start()
        self.assertEqual(self.states['switch.heater'].state, 'off')
        self.slot['device_commands']['heater']['on_seconds'] = 900
        await self.controller.async_tick()
        self.assertEqual(self.states['switch.heater'].state, 'on')
        self.slot['device_commands']['heater']['on_seconds'] = 0
        await self.controller.async_tick()
        self.assertEqual(self.states['switch.heater'].state, 'off')
        self.options['device_modes']['heater'] = 'monitoring'
        await self.controller.async_tick()
        self.assertEqual(self.states['switch.heater'].state, 'on')
        self.assertNotIn('device:heater', self.controller.records)
        self.assertNotIn(('device:heater', 'minimum_run:switch.heater'), self.scheduler.deadlines)

    async def test_minimum_run_deadline_retries_without_a_sensor_event(self):
        self.configure_heater('switch_schedule')
        await self.controller.async_start()
        self.assertEqual(self.states['switch.heater'].state, 'on')
        self.assertIn(('device:heater', 'minimum_run:switch.heater'), self.scheduler.deadlines)
        self.advance(60)
        await self.drain()
        self.assertEqual(self.states['switch.heater'].state, 'off')
        self.assertEqual(self.controller.status['device:heater']['state'], 'commanded')

    async def test_failed_real_handover_alone_gets_a_timed_retry(self):
        self.states['switch.pool'].state = 'on'
        self.options['device_modes'] = {'$pool': 'controlling'}
        self.slot['pool_w'] = 0
        await self.controller.async_start()
        service = self.hass.services.async_call

        async def fail(*args, **kwargs):
            raise ValueError('service interrupted')

        self.hass.services.async_call = fail
        self.options['device_modes']['$pool'] = 'planning'
        self.scheduler.coordinator_updated()
        await self.drain()
        self.assertIn(('pool', 'restoration_retry'), self.scheduler.deadlines)
        self.hass.services.async_call = service
        self.advance(5)
        await self.drain()
        self.assertNotIn('pool', self.controller.records)
        self.assertNotIn(('pool', 'restoration_retry'), self.scheduler.deadlines)
        self.assertEqual(float(self.states['number.start'].state), 29.5)

    async def test_handover_waiting_for_minimum_runtime_uses_its_deadline_not_retries(self):
        self.configure_heater('switch_schedule')
        self.states['switch.heater'].last_changed = self.now - timedelta(minutes=10)
        await self.controller.async_start()
        self.options['device_modes']['heater'] = 'planning'
        self.scheduler.coordinator_updated()
        await self.drain()
        self.assertTrue(self.controller.records['device:heater']['restoration_pending'])
        self.assertNotIn(('device:heater', 'restoration_retry'), self.scheduler.deadlines)
        self.advance(60)
        await self.drain()
        self.assertEqual(self.states['switch.heater'].state, 'on')
        self.assertNotIn('device:heater', self.controller.records)

    async def test_removing_a_generic_device_removes_its_subscriptions(self):
        self.configure_heater()
        await self.controller.async_start()
        self.options['device_modes']['heater'] = 'planning'
        self.scheduler.coordinator_updated()
        await self.drain()
        self.scheduler.coordinator_updated()
        await self.drain()
        self.assertNotIn('switch.heater', self.subscriptions)
        self.assertFalse(any(key[0] == 'device:heater' for key in self.scheduler.deadlines))

    async def test_ha_adapter_filters_events_and_removes_listeners(self):
        import ast
        from pathlib import Path
        self.scheduler.close()
        listeners, unload = [], []

        def callback(function):
            function._hass_callback = True
            return function

        def listen(kind, action, event_filter=None):
            self.assertTrue(action._hass_callback)
            if event_filter is not None:
                self.assertTrue(event_filter._hass_callback)
            item = (kind, action, event_filter)
            listeners.append(item)
            return lambda: listeners.remove(item)

        def track_change(hass, entities, action):
            return listen('state_changed', action, callback(lambda data: data['entity_id'] in entities))

        self.hass.bus = SimpleNamespace(async_listen=listen)
        self.coordinator.async_add_control_listener = lambda action: lambda: None
        battery_listeners = []

        def add_battery_listener(action):
            self.assertTrue(action._hass_callback)
            battery_listeners.append(action)
            return lambda: battery_listeners.remove(action)

        self.coordinator.async_add_battery_listener = add_battery_listener
        entry = SimpleNamespace(async_on_unload=unload.append,
            async_create_background_task=lambda hass, work, name: asyncio.create_task(work))
        path = Path(__file__).parents[1] / 'custom_components/shs_energy/controller_events.py'
        tree = ast.parse(path.read_text())
        tree.body = [node for node in tree.body if not isinstance(node, (ast.Import, ast.ImportFrom))]
        namespace = {'ControllerScheduler': ControllerScheduler, 'callback': callback,
            'EVENT_STATE_REPORTED': 'state_reported', 'EVENT_CORE_CONFIG_UPDATE': 'core_config',
            'er': SimpleNamespace(EVENT_ENTITY_REGISTRY_UPDATED='registry'),
            'async_track_state_change_event': track_change,
            'async_track_point_in_utc_time': lambda hass, action, when: self.at(when, action)}
        exec(compile(tree, str(path), 'exec'), namespace)
        self.scheduler = namespace['attach_controller_events'](self.hass, entry, self.controller)
        self.scheduler.now = lambda: self.now
        await self.controller.async_start()

        def fire(kind, entity):
            data = {'entity_id': entity, 'new_state': self.states.get(entity)}
            for event_type, action, event_filter in list(listeners):
                if event_type == kind and (event_filter is None or event_filter(data)):
                    action(SimpleNamespace(data=data))

        fire('state_changed', 'sensor.unrelated')
        self.assertIsNone(self.scheduler.task)
        fire('state_reported', 'sensor.water')
        self.assertIsNone(self.scheduler.task)
        self.states['sensor.water'].state = '28'
        fire('state_changed', 'sensor.water')
        await self.drain()
        self.assertEqual(self.evaluations('pool'), 2)
        self.assertEqual(self.evaluations('battery'), 1)
        fire('core_config', '')
        await self.drain()
        self.assertEqual(self.evaluations('battery'), 2)
        self.assertEqual(len(battery_listeners), 1)
        for remove in unload:
            remove()
        self.assertFalse(listeners)
        self.assertFalse(battery_listeners)

    async def test_battery_status_refresh_publishes_without_waking_other_controllers(self):
        import ast
        from pathlib import Path
        from unittest.mock import AsyncMock
        source = Path(__file__).parents[1] / 'custom_components/shs_energy/coordinator.py'
        tree = ast.parse(source.read_text())
        cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'ShsStatusCoordinator')
        cls.bases = [ast.Name(id='Base', ctx=ast.Load())]
        cls.body = [node for node in cls.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and node.name in ('async_add_control_listener', 'async_update_listeners', 'async_battery_inputs_refresh',
                                      'async_add_battery_listener', 'async_update_battery_listeners')]

        class Base:
            def async_update_listeners(self):
                self.status_updates += 1

        namespace = {'Base': Base, 'resolved_options': lambda hass, options: options}
        exec(compile(ast.fix_missing_locations(ast.Module(body=[cls], type_ignores=[])), str(source), 'exec'), namespace)
        publisher = namespace['ShsStatusCoordinator']()
        publisher._control_listeners = set()
        publisher._battery_listeners = set()
        publisher.status_updates = 0
        publisher._battery_inputs_lock = asyncio.Lock()
        publisher.hass = self.hass
        publisher.entry = SimpleNamespace(options=self.options)
        publisher.async_battery_planned_devices = AsyncMock(return_value=[])
        publisher.battery_live_inputs = SimpleNamespace(sample=AsyncMock())
        publisher.battery_runtime = SimpleNamespace(refresh=AsyncMock())
        snapshots = []

        def snapshot():
            snapshots.append(True)
            return {'state': 'verified', 'reason': f'refresh {len(snapshots)}'}

        self.controller.battery_runtime = SimpleNamespace(snapshot=snapshot)
        shown = {'battery': 0, 'pool': 0}
        for device in shown:
            self.controller.add_listener(lambda device=device: shown.__setitem__(device, shown[device] + 1),
                                         lambda key, device=device: key == device)
        remove = publisher.async_add_control_listener(self.scheduler.coordinator_updated)
        publisher.async_add_battery_listener(self.controller.publish_battery_status)
        await self.controller.async_start()
        before, published = self.evaluations('pool'), dict(shown)
        for _ in range(12):
            await publisher.async_battery_inputs_refresh()
            await self.drain()
        self.assertEqual(publisher.battery_runtime.refresh.await_count, 12)
        # Each refresh republishes battery status alone: no entity round, no evaluation.
        self.assertEqual(publisher.status_updates, 0)
        self.assertEqual(self.evaluations('pool'), before)
        self.assertEqual(shown, {'battery': published['battery'] + 12, 'pool': published['pool']})
        self.assertEqual(self.controller.status['battery']['reason'], f'refresh {len(snapshots)}')
        publisher.async_update_listeners()
        await self.drain()
        self.assertEqual(publisher.status_updates, 1)
        self.assertEqual(self.evaluations('pool'), before + 1)
        remove()
        publisher.async_update_listeners()
        await self.drain()
        self.assertEqual(self.evaluations('pool'), before + 1)

    async def test_quiet_hour_only_evaluates_on_slots_despite_fresh_reports(self):
        self.options['device_modes']['$battery'] = 'planning'
        await self.controller.async_start()
        for _ in range(60):
            self.advance(60)
            self.event('sensor.water', 'state_report')
            await self.drain()
        self.assertEqual(self.evaluations('pool'), 5)  # Startup plus four quarters.
        self.assertEqual(self.controller.metrics.scheduling['state_report_events'], 60)
        self.assertNotIn('timer', self.controller.metrics.triggers)
        self.assertEqual(self.calls, [])

    async def test_shared_authority_failure_during_one_device_event_checks_all_owners(self):
        self.options['device_modes'] = {'$pool': 'controlling', '$battery': 'controlling'}
        await self.controller.async_start()
        self.assertEqual(set(self.controller.records), {'pool', 'battery'})
        self.coordinator.async_cached_device_configuration.side_effect = ValueError('bad cache')
        self.event('sensor.water')
        await self.drain()
        self.assertFalse(self.controller.records)
        self.assertEqual(self.controller.status['battery']['state'], 'disabled')
        self.assertEqual(self.controller.status['pool']['state'], 'disabled')

    async def test_observation_timer_samples_passive_devices_without_evaluating_controls(self):
        self.now = self.now.replace(minute=2)
        self.slot['start'] = self.now.isoformat()
        for state in self.states.values():
            state.last_reported = state.last_updated = self.now
        await self.controller.async_start()
        before = deepcopy(self.controller.metrics.snapshot()['devices'])
        count = len(self.journal.samples)
        self.advance(60)
        await self.scheduler.sample_task
        self.assertEqual(len(self.journal.samples), count + 1)
        self.assertEqual(self.controller.metrics.snapshot()['devices'], before)
        self.assertIn((None, 'observations'), self.scheduler.deadlines)
        self.scheduler.close()
        self.assertNotIn((None, 'observations'), self.scheduler.deadlines)
