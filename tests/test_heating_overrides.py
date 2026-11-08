"""Manual targets use the native owner and retain normal handover semantics."""
import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, Mock

import test_gateway_metadata as metadata
from shs_core.device_operations import HeatingDeferred

from test_controller import State, Store
import test_device_commands as device_fixtures
from test_device_gateway import MemoryOperations
from test_execution_mode_select import load_adapter
from command_fixture import controller_inputs
from shs_core.device_gateway import DeviceGateway
from shs_core.device_port import device_intent
from shs_core.heating_overrides import HeatingOverrides, active_heating, heating_binding
from shs_core.grid_capacity import capacity_bound, dispatch_capacity, empty_capacity, finish_capacity, observe_capacity, prepare_capacity


OWNER = 'device:heater'


class HeatingEntityTimerTests(IsolatedAsyncioTestCase):
    async def test_timer_refresh_runs_on_home_assistants_event_loop(self):
        def callback(action):
            action.in_event_loop = True
            return action

        timers = []
        platform = SimpleNamespace(async_add_entities=AsyncMock())
        adapter = load_adapter('heating_entities.py', {
            'callback': callback, 'timedelta': timedelta,
            'async_get_current_platform': lambda: platform,
            'async_track_time_interval': lambda hass, action, interval: timers.append(action),
        })
        row = {'minutes': 60}
        entity = SimpleNamespace(row=row, async_write_ha_state=Mock())
        coordinator = SimpleNamespace(online=True, async_add_listener=Mock(),
            service=SimpleNamespace(heating_controls=lambda: {'zones': {OWNER: row}}))
        tasks = []
        def background(hass, work, name):
            task = asyncio.create_task(work, name=name)
            tasks.append(task)
            return task
        entry = SimpleNamespace(runtime_data=coordinator, async_on_unload=Mock(),
                                async_create_background_task=background)
        await adapter.setup_heating_entities(None, entry, lambda *args: [entity])
        self.assertTrue(getattr(timers[0], 'in_event_loop', False))
        row = {'minutes': 59}
        timers[0](datetime.now(timezone.utc))
        await tasks[0]
        self.assertEqual(entity.row, row)
        entity.async_write_ha_state.assert_called_once()


class HeatingOverridesTests(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        device_fixtures.DeviceExecutionTests.setUp(self)
        self.hass.config = SimpleNamespace(units=SimpleNamespace(temperature_unit='°C'))
        self.states['climate.heater'] = State('heat', temperature=20, min_temp=5, max_temp=35,
                                             target_temp_step=.5, hvac_modes=['off', 'heat'])
        self.options['device_control_mappings']['heater'].update(
            control_type='setpoint', actuator_entity_ids=['climate.heater'])
        self.models = [{'key':'heater', 'category':'heating', 'control_type':'setpoint'}]
        self.plan = {'device_models': self.models}
        self.clock = datetime.now(timezone.utc)
        self.book = HeatingOverrides(Store(), Mock(), now=lambda:self.clock)
        self.addCleanup(self.book.close)
        self.capacity = SimpleNamespace(
            make_heating_room=AsyncMock(return_value=False),
            capacity_intention=AsyncMock(side_effect=lambda intent:intent),
            capacity_condition=lambda *args:None,
            heating_cap=lambda owner:None,
            heating_cap_current=lambda cap:True,
            describe_import=lambda *args:{},
            admit_import=Mock(),
        )
        self.physical = DeviceGateway(controller_inputs(self.hass), self.controller.options, self.store,
            self.controller.native_executor, authorize=lambda intent:None, before_external=lambda *args, **kwargs:True,
            operations=MemoryOperations(), capacity=self.capacity, heating=self.book)
        await self.physical.load()

        async def thermostat(domain, service, data, blocking):
            self.calls.append(service)
            state = self.states[data['entity_id']]
            if service == 'set_hvac_mode':
                state.state = data['hvac_mode']
            else:
                state.attributes['temperature'] = data['temperature']
        self.hass.services.async_call = thermostat

    async def edit(self, field, value):
        return await self.book.change(dict(owner=OWNER, field=field, value=value), self.options,
            (heating_binding(self.options, OWNER), 5, 35), {OWNER})

    def intent(self, operation, row=None):
        slot = dict(start=datetime.now(timezone.utc).isoformat(),
                    device_commands={'heater': {'type':'setpoint', 'target_c':19, 'minimum_c':18, 'maximum_c':22}})
        request = device_intent(OWNER, operation, self.plan, slot, configuration_revision=1,
                                policy_revision=1, heating=row)
        return {**request, 'headroom_reserved': True}

    async def test_duration_edits_and_restart_use_one_absolute_end(self):
        first = (await self.edit('target', 22))['zones'][OWNER]
        self.assertEqual(datetime.fromisoformat(first['expires_at']) - self.clock, timedelta(minutes=60))
        self.clock += timedelta(minutes=5)
        newer = (await self.edit('target', 23))['zones'][OWNER]
        self.assertEqual(newer['expires_at'], first['expires_at'])
        latest = (await self.edit('minutes', 20))['zones'][OWNER]
        self.assertEqual(datetime.fromisoformat(latest['expires_at']) - self.clock, timedelta(minutes=20))
        restored = HeatingOverrides(self.book.store, Mock(), now=lambda:self.clock)
        self.addCleanup(restored.close)
        await restored.load()
        self.assertEqual(active_heating(restored.value, self.options, OWNER, self.clock), latest)
        self.clock += timedelta(minutes=20)
        self.assertIsNone(active_heating(restored.value, self.options, OWNER, self.clock))

    async def test_manual_setpoint_fences_old_target_and_plan_then_resumes_current_slot(self):
        old = (await self.edit('target', 22))['zones'][OWNER]
        latest = (await self.edit('target', 23))['zones'][OWNER]
        result = await self.physical.perform(self.intent('heat', old))
        self.assertEqual(result['error']['kind'], 'heating')
        result = await self.physical.perform(self.intent('apply'))
        self.assertEqual(result['error']['kind'], 'heating')
        self.assertEqual(self.calls, [])
        result = await self.physical.perform(self.intent('heat', latest))
        self.assertEqual(result['result']['state'], 'manual')
        self.assertEqual(self.states['climate.heater'].attributes['temperature'], 23)
        self.capacity.make_heating_room.assert_not_awaited()
        await self.physical.perform(self.intent('heat', latest))
        self.capacity.make_heating_room.assert_not_awaited()
        await self.edit('resume', None)
        result = await self.physical.perform(self.intent('apply'))
        self.assertIsNone(result['error'])
        self.assertEqual(self.states['climate.heater'].attributes['temperature'], 19)

    async def test_capacity_shortfall_reduces_load_and_retries_the_same_event(self):
        latest = (await self.edit('target', 22))['zones'][OWNER]
        self.physical.external_ready = Mock(side_effect=[False, True, True])
        result = await self.physical.perform(self.intent('heat', latest))
        self.capacity.make_heating_room.assert_awaited_once()
        self.assertEqual(result['result']['state'], 'manual')
        self.assertEqual(self.calls, ['set_temperature'])

    async def test_app_applies_override_with_no_slot_and_does_not_log_plan_execution(self):
        await self.edit('target', 24)
        self.coordinator.async_cached_device_configuration.return_value = self.models
        self.coordinator.current_plan_slot = None

        async def perform(owner, operation, plan, slot, *, heating=None):
            value = device_intent(owner, operation, plan, slot, configuration_revision=1,
                                  policy_revision=1, heating=heating)
            return await self.physical.perform(value)

        self.controller.devices = SimpleNamespace(
            heating=self.book.value, perform=perform,
            ownership=AsyncMock(side_effect=self.physical.ownership.snapshot),
            synchronize=AsyncMock(side_effect=lambda models:self.physical.ownership.snapshot()),
        )
        self.controller.record_execution = AsyncMock()
        await self.controller.async_start()
        self.assertEqual(self.controller.status[OWNER]['state'], 'manual')
        self.assertEqual(self.states['climate.heater'].attributes['temperature'], 24)
        self.controller.record_execution.assert_not_awaited()

    async def test_curtailment_retains_control_and_verification_restores_original_setting(self):
        self.capacity.heating_cap = lambda owner:{'thermostat':True}
        result = await self.physical.perform(self.intent('apply'))
        self.assertTrue(result['result']['heating_curtailed'])
        self.assertEqual(self.states['climate.heater'].state, 'off')
        self.states['climate.heater'].attributes.pop('temperature')
        self.assertIn(OWNER, self.physical.ownership.records)
        self.options['device_modes']['heater'] = 'control_verification'
        result = await self.physical.perform(self.intent('release'))
        self.assertIsNone(result['error'])
        self.assertEqual(self.states['climate.heater'].state, 'heat')
        self.assertEqual(self.states['climate.heater'].attributes['temperature'], 20)
        self.assertNotIn(OWNER, self.physical.ownership.records)


class HeatingCapacityTests(IsolatedAsyncioTestCase):
    asyncSetUp = metadata.MetadataTests.asyncSetUp

    def make_room(self):
        now = datetime.now(timezone.utc)
        self.options['device_control_mappings'] = {
            'heater': {'control_type':'setpoint', 'actuator_entity_ids':['climate.heater']},
            **{key: {'control_type':'switch_schedule', 'actuator_entity_ids':[f'switch.{key}']}
               for key in ('first', 'second')},
        }
        self.options['device_modes'] = {key:'controlling' for key in ('heater', 'first', 'second')}
        self.source.configuration.options = lambda: self.options
        revision = 1
        self.service.heating = SimpleNamespace(now=lambda:now, value={
            'order':['device:second', 'device:first'], 'zones': {OWNER:{
                'binding':heating_binding(self.options, OWNER), 'expires_at':(now + timedelta(minutes=60)).isoformat(),
                'target_c':22, 'revision':revision}},
        })
        reserved = {OWNER:{'logical':OWNER, 'possible_w':2000}}
        self.source.heating_effects = lambda:reserved
        self.source.refresh_capacity = AsyncMock()
        self.source.capacity_update = AsyncMock()
        self.service.authorize_device = Mock()
        self.service.import_limit_w = 15000
        self.source.discharge_credit = lambda *args:0
        intents = {}
        for key in ('first','second'):
            owner, entity = f'device:{key}', f'switch.{key}'
            self.physical.ownership.records[owner] = {'originals':{entity:'on'}}
            self.source.capacity['devices'][f'device@{entity}'] = {
                'settled':{'after_w':2500,'controls':{entity:'on'},'order':1}, 'effects':{}}
            intents[owner] = dict(request_id=key, device=owner, operation='apply', configuration_revision=1,
                                  policy_revision=1, parameters={'start':now.isoformat()}, headroom_reserved=False)
        self.source.capacity['coordination'] = {'intents':intents}
        self.physical.capacity_reduction_available = lambda *args:True
        self.physical.perform = AsyncMock()
        namespace = self.source.make_heating_room.__globals__
        namespace.update(timedelta=timedelta, active_heating=active_heating, HeatingDeferred=HeatingDeferred)
        return {'device':OWNER, 'parameters':{'revision':revision, 'target_c':22},
                'configuration_revision':1, 'policy_revision':1}

    async def test_ranked_reduction_only_after_headroom_is_exhausted(self):
        request = self.make_room()
        self.source.import_required = lambda **kwargs:14000
        await self.source.make_heating_room(request)
        self.physical.perform.assert_not_awaited()
        self.source.import_required = lambda **kwargs:17000
        await self.source.make_heating_room(request)
        self.assertEqual(self.physical.perform.await_args.args[0]['device'], 'device:second')
        caps = self.source.capacity_update.await_args.args[1]
        self.assertEqual(list(caps), ['device@switch.second'])
        self.assertEqual(caps['device@switch.second']['heating'], {OWNER:1})


    async def test_a_paused_thermostat_releases_only_measured_watts(self):
        capacity = empty_capacity()
        identity = 'device@climate.other'
        effect = dict(device=identity, logical='device:other', changed='climate.other', sensor='sensor.other',
                      before_w=2000, after_w=0, possible_w=0, controls={'climate.other':'off'},
                      signed=False, scale=1, overhead_w=0, response_tolerance_w=0)
        prepare_capacity(capacity, 'pause', effect, 10)
        dispatch_capacity(capacity, 'pause', 10)
        finish_capacity(capacity, 'pause', 'service_returned')
        observations = {'climate.other': (11, dict(entity_id='climate.other', state='off', attributes={})),
                        'sensor.other': (12, dict(state='2000', last_reported='2026-10-17T00:00:00Z',
                                   attributes={'unit_of_measurement':'W', 'state_class':'measurement'}))}
        observe_capacity(capacity, 12, observations.__getitem__)
        self.assertEqual(capacity_bound(capacity['devices'][identity]), 2000)
        observations['sensor.other'] = (13, {**observations['sensor.other'][1], 'state':'0'})
        observe_capacity(capacity, 13, observations.__getitem__)
        self.assertEqual(capacity_bound(capacity['devices'][identity]), 0)
