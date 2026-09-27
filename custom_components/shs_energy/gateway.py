"""HA source, socket and native-service composition for the app gateway."""
import asyncio
from contextlib import closing
from datetime import datetime, timezone
import json
import logging
from pathlib import Path

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.const import EVENT_STATE_CHANGED, EVENT_STATE_REPORTED, EVENT_CORE_CONFIG_UPDATE
from homeassistant.core import callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.json import json_bytes
from homeassistant.util.json import json_loads

from .configuration import (area_name_by_id, async_energy_dashboard_inventory, entity_area_id_by_id,
                            entity_display_name_by_id, resolved_options)
from .coordinator import RecorderSource
from .migration import mapped_entity_ids
from .shs_core.api_contract import INTEGRATION_VERSION
from .shs_core.battery_gateway import BatteryGateway
from .shs_core.battery_runtime import power
from .shs_core.command_transport import CommandTransport
from .shs_core.controller_inputs import ControllerInputs
from .shs_core.device_gateway import DeviceGateway
from .shs_core.gateway_journal import GatewayJournal, GatewayConflict
from .shs_core.gateway_service import GatewayService, AppConnection
from .shs_core.gateway_stream import GatewayCommands, GatewayOperations, GatewayRecord, GatewayStream
from .shs_core.native_commands import NativeExecutor

_LOGGER = logging.getLogger(__name__)
GATEWAYS = 'shs_energy_gateways'
SOCKETS = 'shs_energy_gateway_sockets'


def wire(value):
    return json_loads(json_bytes(value))


class HomeAssistantSource:
    def __init__(self, hass, entry):
        self.hass, self.entry = hass, entry
        self.history = RecorderSource(hass)
        self.service = None
        self.projection = None
        self.entities = set()
        self.closed = False
        self.update_lock = asyncio.Lock()
        self.last_context = None

    def report(self, entity, state=None, kind='state_report'):
        state = self.hass.states.get(entity) if state is None else state
        now = datetime.now(timezone.utc).isoformat()
        return wire(dict(entity_id=entity, state=state.state if state else None,
            attributes=dict(state.attributes) if state else {},
            last_changed=state.last_changed.isoformat() if state else now,
            last_updated=state.last_updated.isoformat() if state else now,
            last_reported=state.last_reported.isoformat() if state else now,
            event_id=state.context.id if state else None, kind=kind))

    def options(self):
        return resolved_options(self.hass, dict(self.entry.options))

    def context(self):
        h = self.hass
        registry = er.async_get(h)
        return wire(dict(options=dict(self.entry.options),
            home=dict(latitude=h.config.latitude, longitude=h.config.longitude, language=h.config.language,
                      timezone=h.config.time_zone, temperature_unit=h.config.units.temperature_unit),
            entity_ids=[s.entity_id for s in h.states.async_all()], entity_names=entity_display_name_by_id(h),
            area_names=area_name_by_id(h), entity_areas=entity_area_id_by_id(h),
            platforms={entity:item.platform for entity in self.entities if (item:=registry.async_get(entity))}))

    def physical_controls(self):
        entities = set()
        options = self.options()
        for key in ('battery_mode_entity', 'battery_charge_limit_entity', 'battery_discharge_limit_entity'):
            if options.get(key):
                entities.add(options[key])
        for record in self.service.physical.ownership.records.values():
            entities.update(record['originals'])
        return {entity:{key:value for key,value in self.report(entity).items() if key in ('state','attributes')}
                for entity in sorted(entities)}

    def invalidate(self):
        self.service.invalidate_configuration()

    async def refresh_configuration(self):
        async with self.update_lock:
            if self.closed:
                return
            self.entities = mapped_entity_ids(self.options())
            for record in self.service.physical.ownership.records.values():
                self.entities.update(record['originals'])
                self.entities.update(mapped_entity_ids(record['options']))
            # Queue the canonical configuration and its initial source values in
            # one callback turn before acknowledging the new revision.
            context = self.context()
            if context == self.last_context:
                self.service.configuration_pending = False
                return
            self.invalidate()
            self.last_context = context
            future = self.service.stream.capture('configuration', context)
            pending = [self.service.stream.capture('observation', self.report(entity)) for entity in sorted(self.entities)]
            self.service.configuration_revision = await future
            await asyncio.gather(*pending)
            self.service.configuration_pending = False

    async def request(self, operation, body):
        fields = {'credentials':set(), 'inventory':set(), 'admit':{'expected','admitted'},
            'statistics':{'start','end','entities','period','units','kinds'},
            'states':{'start','end','entities','with_attributes'}, 'forecast':{'entity'}}
        if operation not in fields or type(body) is not dict or set(body) != fields[operation]:
            raise ValueError('Unsupported source request')
        if operation == 'credentials':
            return dict(self.entry.data)
        if operation == 'inventory':
            return wire(await async_energy_dashboard_inventory(self.hass))
        if operation == 'admit':
            if dict(self.entry.options) != body['expected']:
                raise GatewayConflict('Home settings changed during admission')
            self.invalidate()
            self.hass.config_entries.async_update_entry(self.entry, options=body['admitted'])
            await self.refresh_configuration()
            return {'options':dict(self.entry.options), 'configuration_revision':self.service.configuration_revision}
        if operation == 'forecast':
            return wire(await self.history.hourly_forecast(body['entity']))
        start, end = (datetime.fromisoformat(body[key]) for key in ('start','end'))
        if operation == 'statistics':
            return wire(await self.history.statistics(start,end,set(body['entities']),body['period'],body['units'],set(body['kinds'])))
        return wire(await self.history.states(start,end,body['entities'],with_attributes=body['with_attributes']))

    def publish(self, value):
        if self.projection:
            self.projection.accept(value)

    def external_ready(self, device):
        # The app has already reserved unknown pending demand. HA independently
        # checks the current battery response at the final native boundary.
        battery = self.service.battery
        if battery.installation is None:
            return True
        options = battery.installation[3]
        try:
            watts, _ = power(self.report(options['battery_power_measurement_entity']),
                source=options['battery_power_measurement_entity'], now_ms=int(datetime.now(timezone.utc).timestamp()*1000), signed=True)
            mode = self.report(options['battery_mode_entity'])['state']
            limit = self.report(options['battery_charge_limit_entity'])
            ceiling = float(limit['state'])*(1000 if limit['attributes']['unit_of_measurement'] == 'kW' else 1)
            return mode != 'Command Charging (PV First)' or (ceiling == 0 and watts <= 100)
        except (ValueError, KeyError, TypeError):
            return False

    def attach(self):
        @callback
        def matches(data):
            return data.get('entity_id') in self.entities
        @callback
        def observe(event):
            entity = event.data['entity_id']
            kind = 'state_report' if event.event_type == EVENT_STATE_REPORTED else 'state_change'
            physical = self.service.physical
            physical.observation_changed.set()
            state = event.data.get('new_state')
            physical.ownership.runs.observe(lambda target:state if target==entity else self.hass.states.get(target),
                datetime.now(timezone.utc),entity=entity,received=kind=='state_change')
            if physical.ownership.runs.dirty:
                self.service.obligation_event.set()
            try:
                self.service.stream.capture('observation', self.report(entity, event.data.get('new_state'), kind),
                    ownership=physical.ownership.snapshot() if physical.ownership.runs.dirty else None)
            except Exception:
                self.service.revoke()
                _LOGGER.exception('SHS gateway observation persistence unavailable')
        @callback
        def metadata_changed(event):
            self.invalidate()
            self.entry.async_create_background_task(self.hass, self.refresh_configuration(), name='shs_gateway_configuration')
        for event in (EVENT_STATE_CHANGED, EVENT_STATE_REPORTED):
            self.entry.async_on_unload(self.hass.bus.async_listen(event, observe, event_filter=matches))
        for event in (EVENT_CORE_CONFIG_UPDATE, er.EVENT_ENTITY_REGISTRY_UPDATED):
            self.entry.async_on_unload(self.hass.bus.async_listen(event, metadata_changed))


async def open_gateway(hass, entry):
    path = Path(hass.config.path('.storage', f'shs_energy.gateway.{entry.entry_id}.sqlite'))
    journal = GatewayJournal(path)
    def read_identity():
        with closing(journal.connect(readonly=True)) as db:
            return json.loads(db.execute('SELECT seed FROM authority').fetchone()[0])['identity']
    identity = await hass.async_add_executor_job(read_identity)
    def core_digest():
        from hashlib import sha256
        root = Path(__file__).parent/'shs_core'
        files = {str(p.relative_to(root)):sha256(p.read_bytes()).hexdigest()
                 for p in sorted(root.rglob('*')) if p.is_file() and '__pycache__' not in p.parts and p.suffix != '.pyc'}
        return sha256(json.dumps(files,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()
    if identity['pair']['core_sha256'] != await hass.async_add_executor_job(core_digest):
        raise GatewayConflict('The installed companion core differs from the paired app')
    if identity['entry_id'] != entry.entry_id or identity['pair']['integration_version'] != INTEGRATION_VERSION:
        raise GatewayConflict('The installed companion differs from the seeded migration release')
    await hass.async_add_executor_job(journal.open)
    stream = GatewayStream(journal, hass.async_add_executor_job)
    stream.start()
    source = HomeAssistantSource(hass, entry)
    service = GatewayService(stream, identity, source)
    source.service = service
    async def send(domain, action, data):
        await hass.services.async_call(domain, action, data, blocking=True)
    commands = GatewayCommands(stream)
    native = NativeExecutor(CommandTransport(commands, commands.execute), hass.states.get,
                            lambda:hass.config.units.temperature_unit, send)
    registry = er.async_get(hass)
    inputs = ControllerInputs(hass.states.get, lambda:hass.config.units.temperature_unit,
        lambda entity:item.platform if (item:=registry.async_get(entity)) else None)
    service.physical = DeviceGateway(inputs, source.options, GatewayRecord(stream,'ownership'), native,
        authorize=service.authorize_device, before_external=source.external_ready,
        operations=GatewayOperations(stream, service.session))
    now = lambda:int(datetime.now(timezone.utc).timestamp()*1000)
    service.battery = BatteryGateway(stream,service.physical,source.options,source.report,now,
        authorize=service.authorize,session=service.session,context=service.context)
    try:
        await service.physical.load()
        service.physical.ownership.runs.configure(source.options(), [])
        await service.battery.open()
        source.attach()
        await source.refresh_configuration()
    except BaseException:
        await stream.close()
        await hass.async_add_executor_job(journal.close)
        raise
    service.obligation_task = entry.async_create_background_task(hass,service.maintain(),name='shs_physical_obligations')
    hass.data.setdefault(GATEWAYS,{})[entry.entry_id] = service
    return service


@websocket_api.require_admin
@websocket_api.websocket_command({vol.Required('type'):'shs_energy/gateway',vol.Required('operation'):str,vol.Required('body'):dict})
@websocket_api.async_response
async def websocket_gateway(hass, connection, msg):
    sockets = hass.data.setdefault(SOCKETS,{})
    peer = sockets.get(connection)
    try:
        if msg['operation'] == 'connect':
            if peer is not None:
                raise GatewayConflict('Socket already connected')
            entry = msg['body']['identity']['entry_id']
            service = hass.data.get(GATEWAYS,{}).get(entry)
            if service is None:
                raise GatewayConflict('The SHS gateway is not loaded for this entry')
            peer = sockets[connection] = AppConnection(service)
            @callback
            def disconnected():
                sockets.pop(connection,None)
                peer.disconnected()
                hass.async_create_task(peer.close())
            connection.subscriptions['shs_energy_gateway'] = disconnected
        if peer is None:
            raise GatewayConflict('Connect the paired SHS app first')
        result = await peer.request({key:msg[key] for key in ('id','operation','body')})
        connection.send_result(msg['id'],result['result'])
    except (ValueError, KeyError, TypeError, RuntimeError) as error:
        stale = peer is None or peer.closed or peer.service.connection is not peer
        connection.send_error(msg['id'],'shs_gateway_stale' if stale else 'shs_gateway_rejected',str(error))


def register_gateway(hass):
    websocket_api.async_register_command(hass,websocket_gateway)
