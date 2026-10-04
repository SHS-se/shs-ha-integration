"""HA metadata events against the real battery fence, without installing HA."""
import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
from types import SimpleNamespace
import unittest

import test_battery_gateway as battery_fixtures
from gateway_fixture import IDENTITY
from gateway_wire import filter_sources
from test_execution_mode_select import load_adapter
from shs_core.controller_inputs import configured_entity_ids
from shs_core.gateway_journal import digest
from shs_core.gateway_service import GatewayService
from shs_core.source_admission import (validate_bindings, ordered, FACT_ATTRIBUTES, native_pool_idle, owned_device_sources,
    validate_pool_pause, temperature_available, temperature_metadata)

REGISTRY_UPDATED = 'entity_registry_updated'
CORE_CONFIG_UPDATE = 'core_config_updated'


class Hass:
    """The state machine, registries and bus the gateway source reads."""
    def __init__(self):
        self.config = SimpleNamespace(latitude=59, longitude=18, language='en', time_zone='Europe/Stockholm',
            units=SimpleNamespace(temperature_unit='°C'), internal_url='http://homeassistant.local:8123')
        self.names = {'switch.living_room_aircon':'Living room aircon'}
        self.areas = {}
        self.listeners = {}
        self.bus = SimpleNamespace(async_listen=self.listen)
        self.states = SimpleNamespace(get=lambda entity:None,
            async_all=lambda:[SimpleNamespace(entity_id=entity) for entity in self.names])

    def listen(self, event, handler, event_filter=None):
        self.listeners.setdefault(event, []).append(handler)
        return lambda:None

    def fire(self, event, **data):
        for handler in self.listeners[event]:
            handler(SimpleNamespace(event_type=event, data=data))


class MetadataTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await battery_fixtures.BatteryGatewayTests.asyncSetUp(self)
        self.hass = Hass()
        self.tasks = []
        def background(hass, work, name):
            self.tasks.append(asyncio.create_task(work, name=name))
        entry = SimpleNamespace(async_on_unload=lambda remove:None, async_create_background_task=background)
        passthrough = lambda function:function
        registry = SimpleNamespace(async_get=lambda entity:None)
        adapter = load_adapter('gateway.py', dict(asyncio=asyncio, datetime=datetime, timezone=timezone, json=json,
            logging=logging, Path=Path, callback=passthrough, vol=SimpleNamespace(Required=lambda key:key),
            websocket_api=SimpleNamespace(require_admin=passthrough, async_response=passthrough,
                websocket_command=lambda schema:passthrough),
            er=SimpleNamespace(async_get=lambda hass:registry, EVENT_ENTITY_REGISTRY_UPDATED=REGISTRY_UPDATED),
            EVENT_STATE_CHANGED='state_changed', EVENT_STATE_REPORTED='state_reported',
            EVENT_CORE_CONFIG_UPDATE=CORE_CONFIG_UPDATE,
            json_bytes=lambda value:json.dumps(value).encode(), json_loads=json.loads,
            entity_display_name_by_id=lambda hass:dict(hass.names), area_name_by_id=lambda hass:dict(hass.areas),
            entity_area_id_by_id=lambda hass:{}, RecorderSource=lambda hass:None,
            filter_sources=filter_sources, configured_entity_ids=configured_entity_ids))
        for key,value in dict(validate_bindings=validate_bindings,ordered=ordered,FACT_ATTRIBUTES=FACT_ATTRIBUTES,
                native_pool_idle=native_pool_idle,owned_device_sources=owned_device_sources,validate_pool_pause=validate_pool_pause,
                temperature_available=temperature_available,temperature_metadata=temperature_metadata).items():
            adapter.HomeAssistantSource.report.__globals__[key]=value
        self.source = adapter.HomeAssistantSource(self.hass, entry)
        self.service = GatewayService(self.stream, IDENTITY, self.source)
        self.service.physical, self.service.battery = self.physical, self.gateway
        self.source.service = self.service
        installed = dict(self.options, _observed_entities=[])
        self.source.configuration = SimpleNamespace(options=lambda:deepcopy(installed),
            value=dict(revision=1, digest=digest(installed)))
        self.source.attach()
        await self.source.refresh_configuration()

    async def test_retained_owned_controls_and_overrides_stay_ordered_until_release(self):
        self.source.install_bindings(dict(revision=1,pool_pause=None,
            bindings={'switch.new':['device'],'sensor.temperature':['pool_temperature']}))
        self.physical.ownership.records['pool']=dict(originals={'switch.old':{}},
            options={'pool_control_override_entity':'input_boolean.old',
                     'pool_water_temperature_entity':'sensor.temperature'})
        self.assertTrue(self.source.execution_source('switch.old'))
        self.assertTrue(self.source.execution_source('input_boolean.old'))
        self.physical.ownership.records.pop('pool')
        self.assertFalse(self.source.execution_source('switch.old'))
        self.assertFalse(self.source.execution_source('input_boolean.old'))

    def test_retained_owned_filter_keeps_raw_override_source_ordered(self):
        self.hass.states.get=lambda entity:SimpleNamespace(attributes={'entity_id':'sensor.raw'}) if entity=='sensor.old' else None
        registry=SimpleNamespace(async_get=lambda entity:SimpleNamespace(platform='filter') if entity=='sensor.old' else None)
        self.source.report.__globals__['er'].async_get=lambda hass:registry
        self.physical.ownership.records['pool']=dict(originals={'switch.old':{}},
            options={'pool_control_override_entity':'sensor.old'})
        self.source.install_bindings(dict(revision=1,pool_pause=None,bindings={'switch.new':['device']}))
        self.assertIn('sensor.raw',self.source.entities)
        self.assertTrue(self.source.execution_source('sensor.raw'))
        self.physical.ownership.records.pop('pool')
        self.assertFalse(self.source.execution_source('sensor.raw'))

    async def test_active_filter_rebinding_promotes_raw_source_in_the_refresh_itself(self):
        now=datetime.now(timezone.utc)
        states={entity:SimpleNamespace(state='30',attributes={'unit_of_measurement':'°C'},
            last_changed=now,last_updated=now,last_reported=now,context=SimpleNamespace(id='report'))
            for entity in ('sensor.filtered','sensor.a','sensor.b')}
        states['sensor.filtered'].attributes['entity_id']='sensor.a'
        self.hass.states.get=states.get
        registry=SimpleNamespace(async_get=lambda entity:SimpleNamespace(platform='filter')
            if entity=='sensor.filtered' else None)
        self.source.report.__globals__['er'].async_get=lambda hass:registry
        self.physical.pool_pause=None
        self.physical.observation_changed=asyncio.Event()
        self.source.install_bindings(dict(revision=1,pool_pause=None,
            bindings={'sensor.filtered':['pool_temperature']}))
        await self.source.refresh_configuration()
        states['sensor.filtered'].attributes['entity_id']='sensor.b'
        self.hass.fire('state_reported',entity_id='sensor.filtered',new_state=states['sensor.filtered'])
        await asyncio.gather(*self.tasks)
        self.assertTrue(self.source.execution_source('sensor.b'))
        with self.journal.connect(readonly=True) as db:
            self.assertIsNotNone(db.execute("SELECT payload FROM latest WHERE entity='sensor.b'").fetchone())
        self.hass.fire('state_reported',entity_id='sensor.b',new_state=states['sensor.b'])
        await self.stream.queue.join()
        self.assertGreater(self.source.fact_versions['sensor.b'],0)

    async def test_live_barrier_discards_frozen_reading_promoted_to_ordered_during_wait(self):
        from unittest.mock import patch
        self.source.install_bindings(dict(revision=1,pool_pause=None,bindings={'sensor.a':['reference']}))
        async def barrier(*args):
            self.source.bindings['sensor.a']=frozenset({'capture'})
            return {'through':10}
        with patch.object(self.source,'report',return_value={'state':'old'}), patch.object(self.stream,'call',side_effect=barrier):
            frame=await self.source.request('live',{'after':None})
        self.assertEqual(frame['rows'],{})
        self.assertEqual(frame['configuration_revision'],1)

    def test_new_policy_invalidates_declared_pool_pause_while_switch_stays_off(self):
        from datetime import timedelta
        now=datetime.now(timezone.utc)
        states={'switch.pool':SimpleNamespace(state='off',attributes={}),
                'sensor.temperature':SimpleNamespace(state='30',attributes={'unit_of_measurement':'°C'})}
        self.hass.states.get=states.get
        self.physical.pool_pause=None
        self.source.install_bindings(dict(revision=1,
            pool_pause=dict(entity='switch.pool',until=(now+timedelta(minutes=5)).isoformat()),
            pool_context={key:self.service.context()[key] for key in ('configuration_revision','policy_revision')},
            bindings={'sensor.temperature':['pool_temperature']}))
        self.assertFalse(self.source.execution_source('sensor.temperature'))
        self.service.policy_revision+=1
        self.assertTrue(self.source.execution_source('sensor.temperature'))

    async def test_quiet_filter_rebinding_refreshes_subscription_and_promotion_seeds_current_source(self):
        now=datetime.now(timezone.utc)
        states={entity:SimpleNamespace(state='1',attributes={'unit_of_measurement':'W'},
            last_changed=now,last_updated=now,last_reported=now,context=SimpleNamespace(id='report'))
            for entity in ('sensor.filtered','sensor.a','sensor.b')}
        states['sensor.filtered'].attributes['entity_id']='sensor.a'
        self.hass.states.get=states.get
        registry=SimpleNamespace(async_get=lambda entity:SimpleNamespace(platform='filter')
            if entity=='sensor.filtered' else None)
        self.source.report.__globals__['er'].async_get=lambda hass:registry
        self.source.install_bindings(dict(revision=1,pool_pause=None,bindings={'sensor.filtered':['reference']}))
        await self.source.refresh_configuration()
        states['sensor.filtered'].attributes['entity_id']='sensor.b'
        self.hass.fire('state_reported',entity_id='sensor.filtered',new_state=states['sensor.filtered'])
        await asyncio.gather(*self.tasks)
        self.assertIn('sensor.b',self.source.entities)
        await self.source.request('admission',dict(revision=1,pool_pause=None,bindings={'sensor.filtered':['capture']}))
        self.assertTrue(self.source.execution_source('sensor.b'))
        with self.journal.connect(readonly=True) as db:
            row=json.loads(db.execute("SELECT payload FROM latest WHERE entity='sensor.b'").fetchone()[0])
        self.assertEqual(row['state'],'1')

    async def test_reference_storm_is_filtered_before_encoding_and_required_battery_reports_keep_timestamps(self):
        now=datetime.now(timezone.utc)
        states={entity:SimpleNamespace(state='1',attributes={'unit_of_measurement':'W','ignored':object()},
            last_changed=now,last_updated=now,last_reported=now,context=SimpleNamespace(id='report'))
            for entity in ('sensor.power','sensor.reference')}
        self.hass.states.get=states.get
        self.service.physical.observation_changed=asyncio.Event()
        self.source.entities=set(states)
        self.source.install_bindings(dict(revision=1,pool_pause=None,
            bindings={'sensor.power':['capture'],'sensor.reference':['reference']}))
        from unittest.mock import patch
        with patch.object(self.source,'report',side_effect=AssertionError('filtered event was serialized')):
            for _ in range(100):
                self.hass.fire('state_reported',entity_id='sensor.reference',new_state=states['sensor.reference'])
        self.hass.fire('state_reported',entity_id='sensor.power',new_state=states['sensor.power'])
        await self.stream.queue.join()
        # The real native journal receives only the required report.
        with self.journal.connect(readonly=True) as db:
            row=json.loads(db.execute("SELECT payload FROM latest WHERE entity='sensor.power'").fetchone()[0])
            self.assertIsNone(db.execute("SELECT payload FROM latest WHERE entity='sensor.reference'").fetchone())
        self.assertEqual(row['last_reported'],now.isoformat())
        self.assertEqual(row['attributes'],{'unit_of_measurement':'W'})
        self.assertEqual(self.source.intake_counts['replaceable'],100)

    def current(self):
        return self.gateway.fence.snapshot()['grant_current']

    async def test_events_about_other_entities_keep_the_grant_and_the_battery_settings(self):
        # 3 October 2026: an air-conditioner switch reconnected, its registry
        # entries were updated, and five seconds later HA handed the battery back.
        revision, policy = self.service.configuration_revision, self.service.policy_revision
        self.hass.fire(REGISTRY_UPDATED, action='update', entity_id='switch.living_room_aircon')
        self.hass.config.internal_url = 'http://192.168.10.20:8123'
        self.hass.fire(CORE_CONFIG_UPDATE)
        self.assertFalse(self.service.configuration_pending)
        self.assertTrue(self.current())
        self.assertFalse(self.tasks)
        self.assertEqual((self.service.configuration_revision, self.service.policy_revision), (revision, policy))
        await self.gateway.maintain_obligation()
        self.assertFalse(self.calls)
        self.assertTrue(self.gateway.obligation['pending'])

    async def test_changed_metadata_refuses_commands_until_captured_and_keeps_the_battery_settings(self):
        revision, policy = self.service.configuration_revision, self.service.policy_revision
        self.hass.names['switch.living_room_aircon'] = 'Heat pump'
        self.hass.fire(REGISTRY_UPDATED, action='update', entity_id='switch.living_room_aircon')
        # No await has run: pending configuration and the new policy revision
        # refuse every earlier grant, route, step and device intention.
        self.assertTrue(self.service.configuration_pending)
        self.assertEqual(self.service.policy_revision, policy+1)
        self.assertEqual(len(self.tasks), 1)
        await self.tasks[0]
        self.assertFalse(self.service.configuration_pending)
        self.assertEqual(self.service.configuration_revision, revision+1)
        self.assertEqual(self.source.last_context['entity_names'], {'switch.living_room_aircon':'Heat pump'})
        # A renamed entity is no reason to hand the battery back to its baseline.
        self.assertTrue(self.current())
        await self.gateway.maintain_obligation()
        self.assertFalse(self.calls)

    async def test_settings_revision_revokes_the_writer_without_handing_the_battery_back(self):
        self.source.invalidate()
        self.assertTrue(self.service.configuration_pending)
        self.assertFalse(self.current())
        await self.gateway.maintain_obligation()
        self.assertFalse(self.calls)
        # Only settings that take the battery out of Controlling return it.
        self.options['device_modes']['$battery'] = 'monitoring'
        await self.gateway.maintain_obligation()
        self.assertIn(('select.mode','Maximum Self Consumption'),
            [(data['entity_id'],data.get('option',data.get('value'))) for _,_,data in self.calls])

    async def test_changed_home_and_new_entities_are_still_captured(self):
        for change in (lambda:setattr(self.hass.config, 'latitude', 60),
                       lambda:self.hass.names.update({'sensor.new':'New sensor'})):
            revision = self.service.configuration_revision
            change()
            self.hass.fire(CORE_CONFIG_UPDATE)
            self.assertTrue(self.service.configuration_pending)
            await self.tasks.pop()
            self.assertEqual(self.service.configuration_revision, revision+1)

    async def test_event_during_an_unsettled_capture_invalidates_again(self):
        self.service.invalidate_configuration()
        policy = self.service.policy_revision
        self.hass.fire(REGISTRY_UPDATED, action='update', entity_id='switch.living_room_aircon')
        self.assertEqual(self.service.policy_revision, policy+1)
        await self.tasks.pop()
        self.assertFalse(self.service.configuration_pending)

    async def test_unreadable_metadata_fences_instead_of_assuming_nothing_changed(self):
        def unreadable():
            raise RuntimeError('registry is loading')
        self.hass.states.async_all = unreadable
        policy = self.service.policy_revision
        self.hass.fire(REGISTRY_UPDATED, action='update', entity_id='switch.living_room_aircon')
        self.assertTrue(self.service.configuration_pending)
        self.assertEqual(self.service.policy_revision, policy+1)
        with self.assertRaises(RuntimeError):
            await self.tasks.pop()
