"""Exercise the event scheduler with real controllers and a virtual timer clock."""
import asyncio
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

        async def delayed(attempt):
            if not entered.is_set():
                entered.set()
                await release.wait()
            await append(attempt)

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
        self.coordinator.async_add_listener = lambda action: lambda: None
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
        for remove in unload:
            remove()
        self.assertFalse(listeners)

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
