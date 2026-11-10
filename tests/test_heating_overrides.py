"""Manual targets use the native owner and retain normal handover semantics."""
import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, Mock

import test_gateway_metadata as metadata
from shs_core.device_operations import ControlDeadlineError, HeatingDeferred

from test_controller import State, Store
import test_device_commands as device_fixtures
from test_device_gateway import MemoryOperations
from test_execution_mode_select import load_adapter, load_equipment
from command_fixture import controller_inputs
from shs_core.device_gateway import DeviceGateway
from shs_core.device_port import device_intent
from shs_core.gateway_service import GatewayService
from shs_core.heating_overrides import (HeatingOverrides, active_heating, heating_binding,
    heating_members, relay_heat, room_temperature)
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
            claim_import=Mock(return_value=True),
            clear_import_claim=Mock(),
            heating_cap=lambda owner:None,
            heating_cap_current=lambda cap:True,
            heating_effects=lambda:{},
            describe_import=lambda *args:{},
            admit_import=Mock(),
            lowers_heating_target=Mock(return_value=False),
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

    async def test_idle_thermostat_sets_back_during_charging_then_restores_planned_heat(self):
        native = self.states['climate.heater']
        native.attributes.update(current_temperature=19.6, hvac_action='idle', target_temp_step=1)
        slot = dict(start=datetime.now(timezone.utc).isoformat(), charge_setback=True,
                    device_commands={'heater': {'type':'setpoint', 'target_c':19, 'minimum_c':18, 'maximum_c':22}})
        def request():
            return device_intent(OWNER, 'apply', self.plan, slot, configuration_revision=1, policy_revision=1)

        self.capacity.lowers_heating_target.return_value = True
        self.physical.external_ready = Mock(return_value=False)
        result = await self.physical.perform(request())
        self.assertIsNone(result['error'])
        self.assertEqual(native.attributes['temperature'], 18)
        self.assertEqual(native.state, 'heat')
        self.assertEqual(result['ownership']['records'][OWNER]['charge_setback_c'], 18)
        self.assertEqual(result['result']['decision']['planned_target_c'], 19)
        self.physical.external_ready.assert_not_called()

        native.attributes.pop('current_temperature')
        self.assertEqual((await self.physical.perform(request()))['error']['kind'], 'observation')
        self.assertEqual(native.attributes['temperature'], 18)

        native.attributes['current_temperature'] = 18.5
        self.capacity.lowers_heating_target.return_value = False
        deferred = await self.physical.perform(request())
        self.assertTrue(deferred['reservation_required'])
        self.assertEqual(native.attributes['temperature'], 18)
        self.physical.external_ready.return_value = True
        result = await self.physical.perform(request())
        self.assertIsNone(result['error'])
        self.assertEqual(native.attributes['temperature'], 19)
        self.assertNotIn('charge_setback_c', result['ownership']['records'][OWNER])

    async def test_scheduled_relay_regulates_and_defers_or_sheds_without_manual_relief(self):
        self.options['device_control_mappings']['heater'].update(
            control_type='switch_schedule', actuator_entity_ids=['switch.heater'],
            room_area_id='room', temperature_entity_id='sensor.room')
        self.options['house_consumption_power_entity'] = 'sensor.house'
        self.models[0]['control_type'] = 'switch_schedule'
        self.states['switch.heater'] = State('off')
        self.states['sensor.room'] = State('19.9', unit_of_measurement='°C')
        self.capacity.battery_capacity_sources = lambda: ('sensor.battery',)
        self.capacity.scheduled_heating_over_limit = Mock(return_value=True)

        async def send(domain, service, data, blocking):
            self.calls.append(service)
            self.states['switch.heater'].state = service.removeprefix('turn_')
        self.hass.services.async_call = send

        def admit(device, entity, value, **kwargs):
            if self.capacity.scheduled_heating_over_limit.return_value:
                raise ControlDeadlineError('Waiting for house import capacity')
            return True
        self.physical.external_ready = admit
        slot = dict(start=datetime.now(timezone.utc).isoformat(),
                    device_commands={'heater': {'type':'room_relay', 'room_key':'room', 'target_c':20}})

        async def apply():
            intent = device_intent(OWNER, 'apply', self.plan, slot, configuration_revision=1, policy_revision=1)
            return await self.physical.perform({**intent, 'headroom_reserved': True})

        denied = await apply()
        self.assertEqual(denied['error']['kind'], 'deadline')
        self.assertEqual(self.calls, [])
        self.assertTrue({'sensor.room', 'sensor.house', 'sensor.battery'} <= set(denied['reads']))

        self.capacity.scheduled_heating_over_limit.return_value = False
        self.assertIsNone((await apply())['error'])
        self.assertEqual(self.calls, ['turn_on'])

        self.states['sensor.room'].state = '20.1'
        self.assertTrue((await apply())['result']['decision']['on'])
        self.capacity.scheduled_heating_over_limit.return_value = True
        self.assertTrue((await apply())['result']['heating_curtailed'])
        self.assertEqual(self.calls, ['turn_on', 'turn_off'])

        self.states['sensor.room'].state = '19.8'
        self.capacity.scheduled_heating_over_limit.return_value = False
        self.assertIsNone((await apply())['error'])
        self.assertEqual(self.calls, ['turn_on', 'turn_off', 'turn_on'])

        self.states['sensor.room'].state = 'unavailable'
        self.assertEqual((await apply())['error']['kind'], 'observation')
        self.assertEqual(self.states['switch.heater'].state, 'on')

        slot['device_commands']['heater']['room_key'] = 'different_room'
        self.assertEqual((await apply())['error']['kind'], 'plan')
        self.assertEqual(self.calls, ['turn_on', 'turn_off', 'turn_on'])
        self.capacity.make_heating_room.assert_not_awaited()

    async def test_running_native_thermostat_is_paused_and_resumes_at_the_room_target(self):
        self.capacity.battery_capacity_sources = lambda: ()
        self.capacity.scheduled_heating_over_limit = Mock(return_value=True)
        slot = dict(start=datetime.now(timezone.utc).isoformat(), device_commands={'heater': {
            'type': 'setpoint', 'target_c': 22.5, 'room_target_c': 22, 'minimum_c': 5, 'maximum_c': 35}})

        async def apply():
            request = device_intent(OWNER, 'apply', self.plan, slot, configuration_revision=1, policy_revision=1)
            return await self.physical.perform({**request, 'headroom_reserved': True})

        limited = await apply()
        self.assertTrue(limited['result']['heating_curtailed'])
        self.assertEqual(self.states['climate.heater'].state, 'off')
        self.capacity.scheduled_heating_over_limit.return_value = False
        self.assertIsNone((await apply())['error'])
        self.assertEqual(self.states['climate.heater'].state, 'heat')
        self.assertEqual(self.states['climate.heater'].attributes['temperature'], 22.5)
        self.capacity.make_heating_room.assert_not_awaited()

    async def test_writable_setpoint_reads_equipment_until_override_is_applied(self):
        native = {'state': 'heat', 'attributes': self.states['climate.heater'].attributes}
        source = SimpleNamespace(options=lambda: self.options, report=lambda entity: native)
        service = GatewayService(SimpleNamespace(listeners=set()), {}, source)
        service.active = True
        service.heating = self.book
        service.projection = {'execution_devices': [dict(key='heater', name='Heater', category='heating',
            planned=True, mode='controlling', permission={'controller_id': OWNER}, planned_target_c=19)]}

        equipment = load_equipment()
        heating = load_adapter('heating_entities.py', {'callback': lambda fn: fn,
            'equipment_device_info': equipment.equipment_device_info})
        number = load_adapter('number.py', {'HeatingEntity': heating.HeatingEntity, 'NumberEntity': object,
            'NumberDeviceClass': SimpleNamespace(TEMPERATURE='temperature', DURATION='duration'),
            'NumberMode': SimpleNamespace(BOX='box')})
        entry = SimpleNamespace(entry_id='home', data={'device_token_id': 'token'},
            runtime_data=SimpleNamespace(service=service, online=True))
        entity = number.HeatingNumber(entry, OWNER, service.heating_controls()['zones'][OWNER], 'target')
        self.assertEqual(entity.native_value, 20)
        self.assertEqual(entity.extra_state_attributes['planned_target_c'], 19)

        await entity.async_set_native_value(22)
        entity.row = service.heating_controls()['zones'][OWNER]
        self.assertEqual(entity.row['target_c'], 22)
        self.assertEqual(entity.native_value, 20)
        manual = active_heating(self.book.value, self.options, OWNER, self.clock)
        result = await self.physical.perform(self.intent('heat', manual))
        self.assertIsNone(result['error'])
        entity.row = service.heating_controls()['zones'][OWNER]
        self.assertEqual(entity.native_value, 22)

        native['state'] = 'unavailable'
        entity.row = service.heating_controls()['zones'][OWNER]
        self.assertIsNone(entity.native_value)

        self.options['device_control_mappings']['heater']['actuator_entity_ids'] = ['number.target']
        native.update(state='19.5', attributes={'min': 5, 'max': 35, 'step': .5})
        entity.row = service.heating_controls()['zones'][OWNER]
        self.assertEqual(entity.native_value, 19.5)

    async def test_mixed_room_override_shares_target_and_restores_the_relay_plan(self):
        room = 'room:bathroom'
        relay = 'device:rack'
        self.options['device_control_mappings']['heater']['room_area_id'] = 'bathroom'
        self.options['device_control_mappings']['rack'] = {
            'room_area_id': 'bathroom', 'temperature_entity_id': 'sensor.room',
            'control_type': 'switch_schedule', 'actuator_entity_ids': ['switch.rack']}
        self.options['device_modes']['rack'] = 'controlling'
        self.models.append(dict(key='rack', category='heating', control_type='switch_schedule'))
        self.states['sensor.room'] = State('20.5', unit_of_measurement='°C')
        self.states['switch.rack'] = State('off')
        send_thermostat = self.hass.services.async_call

        async def send(domain, service, data, blocking):
            if domain == 'switch':
                self.states['switch.rack'].state = service.removeprefix('turn_')
                self.calls.append(service)
            else:
                await send_thermostat(domain, service, data, blocking)
        self.hass.services.async_call = send

        def report(entity):
            state = self.states.get(entity)
            return dict(state=state.state, attributes=state.attributes) if state else None
        source = SimpleNamespace(options=lambda: self.options, report=report,
                                 area_names=lambda: {'bathroom': 'Bathroom'})
        service = GatewayService(SimpleNamespace(listeners=set()), {}, source)
        service.active = True
        service.heating = self.book
        service.projection = {'execution_devices': [
            dict(key=key, name=key, category='heating', planned=True, mode='controlling',
                 permission={'controller_id': owner})
            for key, owner in (('heater', OWNER), ('rack', relay))]}
        zones = service.heating_controls()['zones']
        self.assertEqual(list(zones), [room])
        self.assertEqual(zones[room]['entity_owner'], OWNER)
        await service.change_heating(dict(owner=room, field='minutes', value=1))
        await service.change_heating(dict(owner=room, field='target', value=22))
        manual = self.book.value['zones'][room]
        self.assertEqual(heating_members(self.book.value), {OWNER, relay})
        self.assertEqual(active_heating(self.book.value, self.options, relay, self.clock), manual)

        result = await self.physical.perform(self.intent('heat', manual))
        self.assertIsNone(result['error'])
        slot = dict(start=self.clock.isoformat(), device_commands={
            'rack': {'type': 'switch_schedule', 'on_seconds': 0}})
        def relay_intent(operation, heating=None):
            return device_intent(relay, operation, self.plan, slot, configuration_revision=1,
                                 policy_revision=1, heating=heating)

        result = await self.physical.perform(relay_intent('heat', manual))
        self.assertIsNone(result['error'])
        self.assertIn('sensor.room', result['reads'])
        self.assertEqual(self.states['switch.rack'].state, 'on')
        self.states['sensor.room'].state = 'unavailable'
        result = await self.physical.perform(relay_intent('heat', manual))
        self.assertEqual(result['error']['kind'], 'observation')
        self.assertEqual(self.states['switch.rack'].state, 'on')
        self.states['sensor.room'].state = '22.3'
        self.assertIsNone((await self.physical.perform(relay_intent('heat', manual)))['error'])
        self.assertEqual(self.states['switch.rack'].state, 'off')
        self.states['sensor.room'].state = '21.9'
        self.assertIsNone((await self.physical.perform(relay_intent('heat', manual)))['error'])
        self.assertEqual(self.states['switch.rack'].state, 'on')

        self.clock += timedelta(minutes=1)
        self.assertIsNone((await self.physical.perform(relay_intent('apply')))['error'])
        self.assertEqual(self.states['switch.rack'].state, 'off')
        self.assertIsNone(service.heating_controls()['zones'][room]['expires_at'])

    async def test_saved_device_override_moves_to_room_without_adding_members(self):
        legacy = (await self.edit('target', 22))['zones'][OWNER]
        self.options['device_control_mappings']['heater']['room_area_id'] = 'bathroom'
        self.options['device_control_mappings']['rack'] = {
            'room_area_id': 'bathroom', 'temperature_entity_id': 'sensor.room',
            'control_type': 'switch_schedule', 'actuator_entity_ids': ['switch.rack']}
        self.options['device_modes']['rack'] = 'controlling'
        reopened = HeatingOverrides(self.book.store, Mock(), now=lambda: self.clock)
        self.addCleanup(reopened.close)
        await reopened.load(self.options)
        saved = reopened.value['zones']['room:bathroom']
        self.assertEqual(saved['expires_at'], legacy['expires_at'])
        self.assertEqual(saved['bindings'], {OWNER: ['climate.heater']})
        self.assertIsNone(active_heating(reopened.value, self.options, 'device:rack', self.clock))
        await reopened.load(self.options)
        self.assertEqual(reopened.value['zones'], {'room:bathroom': saved})

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

    async def test_expiry_lowers_target_without_waiting_for_battery_charging_to_stop(self):
        await self.edit('minutes', 1)
        manual = (await self.edit('target', 22))['zones'][OWNER]
        self.physical.external_ready = Mock(side_effect=lambda device, *, entity, value,
                priority=False, require_battery_release=True: priority or not require_battery_release)
        self.capacity.lowers_heating_target.side_effect = lambda entity, value: (
            value <= self.states[entity].attributes['temperature'])
        result = await self.physical.perform(self.intent('heat', manual))
        self.assertIsNone(result['error'])
        self.clock += timedelta(minutes=1)

        scheduled = {**self.intent('apply'), 'headroom_reserved': False}
        result = await self.physical.perform(scheduled)
        self.assertIsNone(result['error'])
        self.assertFalse(result['reservation_required'])
        self.assertEqual(result['deadlines'], [])
        self.assertEqual(self.states['climate.heater'].attributes['temperature'], 19)

        self.states['climate.heater'].attributes['temperature'] = 18
        result = await self.physical.perform({**scheduled, 'request_id': 'raise'})
        self.assertTrue(result['reservation_required'])
        self.assertEqual([row['name'] for row in result['deadlines']], ['battery_headroom'])
        self.assertEqual(self.states['climate.heater'].attributes['temperature'], 18)

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

    async def test_room_relay_reserves_capacity_only_while_calling_for_heat(self):
        owner = 'device:rack'
        self.options['device_control_mappings'] = {'rack': {
            'room_area_id': 'bathroom', 'temperature_entity_id': 'sensor.room',
            'control_type': 'switch_schedule', 'actuator_entity_ids': ['switch.rack']}}
        self.options['device_modes'] = {'rack': 'controlling'}
        self.source.configuration.options = lambda: self.options
        now = datetime.now(timezone.utc)
        self.service.heating = SimpleNamespace(now=lambda: now, value={'zones': {'room:bathroom': {
            'bindings': {owner: heating_binding(self.options, owner)}, 'target_c': 22, 'revision': 1,
            'expires_at': (now + timedelta(minutes=1)).isoformat()}}})
        self.rows.update({'sensor.room': {'state': '21', 'attributes': {'unit_of_measurement': '°C'}},
                          'switch.rack': {'state': 'off', 'attributes': {}}})
        self.source.report = self.rows.get
        effect = {'logical': owner, 'possible_w': 750}
        self.source.describe_import = Mock(return_value=effect)
        namespace = self.source.heating_effects.__globals__
        namespace.update(active_heating=active_heating, heating_binding=heating_binding,
                         heating_members=heating_members, relay_heat=relay_heat, room_temperature=room_temperature)
        self.assertEqual(self.source.heating_effects(), {owner: effect})
        self.source.describe_import.assert_called_once_with(owner, 'switch.rack', 'on')
        self.rows['sensor.room']['state'] = '23'
        self.assertEqual(self.source.heating_effects(), {})

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
