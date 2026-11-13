"""HA source, socket and native-service composition for the app gateway."""
import asyncio
from contextlib import closing
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import json
import logging
from pathlib import Path
from time import perf_counter, thread_time

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.components.energy import async_get_manager
from homeassistant.config_entries import SIGNAL_CONFIG_ENTRY_CHANGED
from homeassistant.const import EVENT_STATE_CHANGED, EVENT_STATE_REPORTED, EVENT_CORE_CONFIG_UPDATE
from homeassistant.core import callback
from homeassistant.helpers import area_registry as ar, device_registry as dr, entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.json import json_bytes
from homeassistant.util.json import json_loads

from .configuration import (area_name_by_id, entity_area_id, entity_area_id_by_id,
                            entity_display_name_by_id, resolved_options)
from .recorder_source import RecorderSource
from .shs_core.household_ports import HouseholdReadError, ConfigurationChanged
from .gateway_wire import ProjectionAssembly, filter_sources
from .shs_wire.protocol import admit, offer
from .shs_core.controller_inputs import configured_entity_ids
from .shs_core.api_contract import INTEGRATION_VERSION
from .shs_core.battery_gateway import BatteryGateway
from .shs_core.battery_native_adapter import SigenAdapter
from .shs_core.native_readings import power
from .shs_core.grid_capacity import (battery_released, required_import, import_limit,
    empty_capacity, capacity_bound, commanded_controls, _current, EV_WATTS_PER_AMP, device_power_bound, observe_power_peak, observe_setting_peak, merge_power_peaks, capacity_key, house_meter_errors, HouseMeterError, GridCapacityConfigurationError, battery_import_bound, battery_discharge_ceiling, CHARGING_MODES)
from .shs_core.device_commands import actuator_targets
from .shs_core.device_controls import pool_control_mapping
from .shs_core.device_draw import DrawBinding, HISTORY_WINDOW, REFRESH_INTERVAL, learn_device_draw
from .shs_core.discovery import dashboard_meters
from .shs_core.operating_modes import device_mode
from .shs_core.battery_mode import promised_credit_w
from .shs_core.battery_supply import proportional_supply
from .shs_core.command_transport import CommandTransport
from .shs_core.controller_inputs import ControllerInputs
from .shs_core.device_gateway import DeviceGateway, DeviceIntent
from .shs_core.device_operations import ControlDeadlineError, ControlObservationError, PowerHistoryError, HeatingDeferred
from .shs_core.gateway_journal import AdmissionPending, ConfigurationPending, GatewayJournal, GatewayConflict, digest
from .shs_core.gateway_service import GatewayService, AppConnection
from .shs_core.heating_overrides import (HeatingOverrides, active_heating, heating_binding,
    heating_members, relay_heat, room_temperature)
from .shs_core.gateway_stream import GatewayCommands, GatewayOperations, GatewayRecord, GatewayStream
from .shs_core.execution_configuration import ExecutionConfiguration
from .shs_core.source_admission import (validate_bindings, ordered, FACT_ATTRIBUTES, native_pool_idle, owned_device_sources,
    validate_pool_pause, temperature_available, temperature_metadata)
from .shs_core.native_commands import NativeExecutor, finite
from .shs_core.native_numbers import canonical_metadata, watts as native_watts
from .shs_core.grid_capacity import capped_intention, ev_current_ceiling
from .shs_core.connection_health import RetryBackoff
from .shs_core import run_priority
from .shs_core.run_priority import valid_run
from math import isfinite

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
        self.configuration = None
        self.admission = None
        self.bindings = {}
        self.pool_metadata = {}
        self.pool_unavailable = set()
        self.live_revision = 0
        self.live_changes = {}
        self.fact_versions = {}
        self.capacity = empty_capacity()
        self.import_claims = {}
        self.heating_waiting = set()
        self.power_peaks = {}
        self.power_entities = set()
        self.power_groups = {}
        self.power_settings = set()
        self.power_history_loaded = set()
        self.power_history_pending = False
        self.draw_bindings = set()
        self.device_draws = {}
        self.draw_refresh = asyncio.Event()
        self.capacity_entities = set()
        self.capacity_refresh_pending = False
        # Live run priority (docs/run-priority.md): the battery's published
        # run, carried delivery, and the decision shared by one burst.
        self.charge_run = None
        self.run_progress = {}
        self.run_progress_saved = None
        self.priority_cache = None
        self.priority_last = None
        self.priority_pending = {}
        self.priority_passes = 0
        self.observed_serial = 0
        self.intake_counts = {'ordered':0, 'replaceable':0, 'bytes':0}

    def report(self, entity, state=None, kind='state_report', *, compact=False):
        state = self.hass.states.get(entity) if state is None else state
        now = datetime.now(timezone.utc).isoformat()
        attributes = ({key:value for key,value in state.attributes.items() if not compact or key in FACT_ATTRIBUTES}
                      if state else {})
        return wire(dict(entity_id=entity, state=state.state if state else None,
            attributes=attributes,
            last_changed=state.last_changed.isoformat() if state else now,
            last_updated=state.last_updated.isoformat() if state else now,
            last_reported=state.last_reported.isoformat() if state else now,
            event_id=state.context.id if state else None, kind=kind))

    def options(self):
        options=self.configuration.options()
        if self.configuration.value['revision']==0:
            return resolved_options(self.hass,options)  # One-time adoption source.
        return {key:value for key,value in options.items() if key!='_observed_entities'}

    def area_names(self):
        return area_name_by_id(self.hass)

    def context(self, entities=None):
        h = self.hass
        registry = er.async_get(h)
        entities = self.entities if entities is None else entities
        return wire(dict(options=self.configuration.options(),
            configuration_authority={**{key:self.configuration.value[key] for key in ('revision','digest')},
                'verification_revisions':self.configuration.value.get('verification_revisions', {})},
            home=dict(latitude=h.config.latitude, longitude=h.config.longitude, language=h.config.language,
                      timezone=h.config.time_zone, temperature_unit=h.config.units.temperature_unit),
            observed_entities=sorted(entities), entity_ids=[s.entity_id for s in h.states.async_all()], entity_names=entity_display_name_by_id(h),
            area_names=area_name_by_id(h), entity_areas=entity_area_id_by_id(h),
            platforms={entity:item.platform for entity in entities if (item:=registry.async_get(entity))}))

    def sources(self):
        """Configured, owned and filter-derived entities, read without subscribing."""
        entities = (set(self.configuration.options()['_observed_entities']) if self.configuration.value['revision'] else configured_entity_ids(self.options()))
        entities.update(self.bindings)
        entities.update(self.capacity_entities)
        entities.update(self.power_history_sources())
        for record in self.service.physical.ownership.records.values():
            entities.update(record['originals'])
            entities.update(configured_entity_ids(record['options']))
        registry = er.async_get(self.hass)
        return filter_sources(entities, self.hass.states.get,
            lambda entity: item.platform if (item := registry.async_get(entity)) else None)

    def install_bindings(self, value):
        self.admission = value
        bindings = validate_bindings(value['bindings'])
        registry = er.async_get(self.hass)
        for entity, uses in tuple(bindings.items()):
            derived = filter_sources({entity}, self.hass.states.get,
                lambda key: item.platform if (item := registry.async_get(key)) else None)
            for source in derived:
                bindings[source] = bindings.get(source, frozenset()) | uses
        self.bindings = bindings
        self.entities = self.sources()
        self.pool_metadata = {entity:temperature_metadata(self.hass.states.get(entity))
                              for entity,uses in bindings.items() if 'pool_temperature' in uses}
        self.pool_unavailable = {entity for entity in self.pool_metadata
                                 if not temperature_available(self.hass.states.get(entity))}

    def pool_idle(self):
        context = self.service.context()
        declared = (self.admission['pool_pause'] if self.admission is not None
                    and self.admission['pool_pause'] is not None
                    and self.admission['pool_context'] == {key:context[key]
                        for key in ('configuration_revision','policy_revision')} else None)
        return native_pool_idle(self.service.physical.pool_pause,
            declared, context, self.hass.states.get,self.service.physical.ownership,
            datetime.now(timezone.utc))

    def owned_sources(self):
        entities = set()
        for record in self.service.physical.ownership.records.values():
            entities.update(owned_device_sources(record))
        registry = er.async_get(self.hass)
        return filter_sources(entities, self.hass.states.get,
            lambda key: item.platform if (item := registry.async_get(key)) else None)

    def execution_source(self, entity, *, owned=None):
        # Prior durable input schemas drain normally until the explicit sealed
        # admission installation for the current canonical configuration.
        if self.admission is None or self.admission['revision'] != self.configuration.value['revision']:
            return True
        uses = self.bindings.get(entity, frozenset())
        # Ownership outlives configuration edits until explicit handover retires
        # it. Keep its former controls and override sources in the ordered plane.
        if entity in self.capacity_entities or entity in (self.owned_sources() if owned is None else owned):
            uses = uses | {'device'}
        thermal = 'pool_temperature' in uses
        valid = (not self.pool_unavailable and all(temperature_available(self.hass.states.get(source))
                     and temperature_metadata(self.hass.states.get(source)) == self.pool_metadata[source]
                     for source in self.pool_metadata) if thermal else True)
        return ordered(uses, pool_idle=valid and self.pool_idle() if thermal else False)

    def metadata_unchanged(self):
        """Whether a registry or core event leaves the settled context as it is.

        Those events fire for every entity in Home Assistant. Only a context
        the app has not received needs its commands refused and a new capture.
        """
        if self.service.configuration_pending:
            return False
        try:
            return self.context(self.sources()) == self.last_context
        except Exception:
            return False  # Fence first; the refresh then reports the failure.

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

    async def capture_configuration(self):
        # Retire ownership before acknowledging the new mode, even if the next
        # edit returns the device to Controlling before the app evaluates it.
        async with self.service.physical.lock:
            options = self.options()
            await self.service.physical.abandon_verification(options)
            await self.service.battery.abandon_verification(options)
        await self.refresh_configuration()
        self.service.obligation_event.set()

    async def refresh_configuration(self):
        async with self.update_lock:
            if self.closed:
                return
            if self.admission is not None:
                self.install_bindings(self.admission)
            self.entities = self.sources()
            self.collect_power_history()
            # Queue the canonical configuration and its initial source values in
            # one callback turn before acknowledging the new revision.
            context = self.context()
            if context == self.last_context:
                self.service.configuration_pending = False
                return
            # A settings installation has already revoked the battery writer.
            self.service.invalidate_context()
            self.last_context = context
            future = self.service.stream.capture('configuration', context)
            pending = [self.service.stream.capture('observation', self.report(entity, compact=True))
                       for entity in sorted(self.entities) if self.execution_source(entity)]
            self.service.configuration_revision = await future
            await asyncio.gather(*pending)
            if self.service.heating is not None:
                await self.service.heating.retire(self.options())
            self.service.configuration_pending = False

    async def request(self, operation, body):
        fields = {'credentials':set(),
            'admission':{'revision','bindings','pool_pause'}, 'live':{'after'},
            'catalog':set(), 'device_room':{'device_key','meters','area_id'},
            'battery_capacity':set(), 'planning_power_bounds':{'mappings'},
            'planning_device_draws':{'mappings'},
            'configure':{'expected_revision','revision','options','digest'},
            'statistics':{'start','end','entities','period','units','kinds'},
            'states':{'start','end','entities','with_attributes'}, 'forecast':{'entity'}}
        if operation == 'battery_capacity' and type(body) is dict and set(body) == {'charge_run'}:
            published = body['charge_run']
            if (type(published) is not dict or set(published) != {'run', 'want_w'}
                    or not valid_run(published['run']) or (published['run'] or {}).get('kind') != 'charge'
                    or published['want_w'] is not None and (type(published['want_w']) not in (int, float)
                        or not isfinite(published['want_w']) or published['want_w'] < 0)):
                raise ValueError('Invalid battery charging run')
            self.charge_run = published
        elif operation not in fields or type(body) is not dict or set(body) != fields[operation]:
            raise ValueError('Unsupported source request')
        elif operation == 'battery_capacity':
            self.charge_run = None
        if operation == 'credentials':
            return dict(self.entry.data)
        if operation in ('planning_power_bounds', 'planning_device_draws'):
            # Planning and final dispatch share the same retained electrical
            # evidence. A quiet/currently limited sensor is not an ON rating.
            mappings = body['mappings']
            if mappings != self.options().get('device_control_mappings', {}):
                raise GatewayConflict('Electrical capture belongs to another device configuration')
            if operation == 'planning_device_draws':
                return {key:asdict(draw) for key,mapping in mappings.items()
                        if (draw := self.device_draw(mapping)) is not None}
            await self.refresh_capacity()
            if mappings != self.options().get('device_control_mappings', {}):
                raise GatewayConflict('Device configuration changed during electrical capture')
            return {key:self.mapped_power_bound(mapping) for key,mapping in mappings.items()}
        if operation == 'battery_capacity':
            async with self.service.physical.lock:
                await self.refresh_capacity()
                house = self.house_power()
                claims = self.current_import_claims()
                # Size this battery against the same native/device reservations
                # as dispatch. Its own envelope is supplied by the new target;
                # older actuator sets remain independent reserved demand.
                limit = self.house_import_limit()
                external = self.import_required(exclude_device=self.battery_capacity_key(),
                    reserved=[*self.heating_effects().values(), *claims], house_w=house)
                result = {'external_import_w': external, 'import_limit_w': limit,
                        'native_load_bounds':self.native_load_bounds(), 'observed_house_w':house,
                        'grid_charge_hold':any(claim['hold_charge'] for claim in claims)}
                try:
                    decision = self.priority_pass()
                except (AdmissionPending, ControlObservationError, ValueError, KeyError, TypeError):
                    decision = None  # The measured budget above still sizes the charge.
                if decision is not None and 'battery' in decision['granted']:
                    # This battery's part of the shared budget. It holds its
                    # charge while a less pressing load's reduction is in
                    # flight, and takes more only from confirmed capacity.
                    scheduled_external = max(0.0, limit - decision['granted']['battery'])
                    # Optional increases must also fit the native reservations
                    # used by final dispatch. Keep the scheduled share intact.
                    spare = min(decision['battery_spare_w'], max(0.0, scheduled_external - external))
                    result.update(external_import_w=scheduled_external - spare, priority_shared=True)
                return result
        if operation == 'admission':
            validate_bindings(body['bindings'])
            validate_pool_pause(body['pool_pause'])
            if body['revision'] != self.configuration.value['revision']:
                raise GatewayConflict('Source bindings belong to another configuration')
            previous = {entity for entity in self.entities if self.execution_source(entity)}
            context = self.service.context()
            sealed = {**body,'pool_context':{key:context[key] for key in ('configuration_revision','policy_revision')}}
            await self.service.stream.call('save_record','source_admission',sealed)
            if body['revision'] != self.configuration.value['revision']:
                raise GatewayConflict('Configuration changed during source admission')
            self.install_bindings(sealed)
            context = self.last_context
            await self.refresh_configuration()
            if self.last_context == context:
                # Role promotion must deliver the current value even when no
                # state report fires after installation.
                pending = [self.service.stream.capture('observation', self.report(entity, compact=True))
                           for entity in sorted(self.entities-previous) if self.execution_source(entity)]
                await asyncio.gather(*pending)
            return {}
        if operation == 'live':
            after = body['after']
            if after is not None and (type(after) is not int or not 0 <= after <= self.live_revision):
                raise ValueError('Invalid live reading revision')
            # Freeze selected values before the queue barrier in this loop turn.
            revision = self.live_revision
            configuration_revision = self.configuration.value['revision']
            owned = self.owned_sources()
            rows = {entity:self.report(entity) for entity in self.entities
                    if (after is None or self.live_changes.get(entity,0) > after)
                    and not self.execution_source(entity, owned=owned)}
            versions = {entity:self.fact_versions.get(entity,0) for entity in rows}
            snapshot = await self.service.stream.call('snapshot',self.service.session())
            # Ownership or a filter binding may have changed across the barrier.
            owned = self.owned_sources() if rows else ()
            rows = {entity:row for entity,row in rows.items() if not self.execution_source(entity, owned=owned)
                    and versions[entity] == self.fact_versions.get(entity,0)}
            return dict(through=snapshot['through'],revision=revision,configuration_revision=configuration_revision,rows=rows,
                        counts={**self.intake_counts,**self.service.stream.metrics})
        if operation in ('catalog', 'device_room'):
            manager = await async_get_manager(self.hass)
            preferences = manager.data or manager.default_preferences()
            meters = dashboard_meters(preferences)
            registry = er.async_get(self.hass)
            if operation == 'device_room':
                selected = meters.get(body['device_key'])
                if not selected or list(selected) != body['meters']:
                    raise ValueError('The Energy Dashboard meters changed. Refresh settings and choose the room again.')
                area = body['area_id']
                if not isinstance(area, str) or area not in self.area_names():
                    raise ValueError('Choose an existing Home Assistant room.')
                if any(set(selected).intersection(other) for key, other in meters.items()
                       if key != body['device_key']):
                    raise ValueError('A meter is shared with another Energy Dashboard device. Assign its room in Home Assistant.')
                entries = [registry.async_get(entity) for entity in selected]
                if any(entry is None for entry in entries):
                    raise ValueError('Each configured Energy Dashboard meter needs a Home Assistant entity to assign its room.')
                changed = []
                try:
                    for entry in entries:
                        if entry.area_id != area:
                            registry.async_update_entity(entry.entity_id, area_id=area)
                            changed.append(entry)
                except Exception:
                    for entry in reversed(changed):
                        registry.async_update_entity(entry.entity_id, area_id=entry.area_id)
                    raise
                return {}
            attributes = {'friendly_name','unit_of_measurement','device_class','state_class',
                'min','max','step','options','hvac_modes','min_temp','max_temp','target_temp_step'}
            states = {}
            for state in self.hass.states.async_all():
                selected = {key:value for key,value in state.attributes.items() if key in attributes}
                # Discovery needs presence of a PV forecast, not its full history.
                if state.attributes.get('watts'):
                    selected['watts'] = {'available':True}
                states[state.entity_id] = dict(entity_id=state.entity_id,state=state.state,
                    attributes=selected,last_updated=state.last_updated.isoformat())
            return wire(dict(context=self.context(),states=states,
                preferences=preferences, meter_areas={
                    entity: entity_area_id(self.hass, entity)
                    for entity in {entity for pair in meters.values() for entity in pair}
                    if registry.async_get(entity) is not None
                }))
        if operation == 'configure':
            result = await self.configuration.install(body)
            marker = {'settings_owner':'app'}
            data = {key:value for key,value in self.entry.data.items() if key != 'device_token'}
            if dict(self.entry.options) != marker or data != dict(self.entry.data):
                self.hass.config_entries.async_update_entry(self.entry, options=marker, data=data)
            return result
        if operation == 'forecast':
            return wire(await self.history.hourly_forecast(body['entity']))
        start, end = (datetime.fromisoformat(body[key]) for key in ('start','end'))
        if operation == 'statistics':
            rows = await self.history.statistics(start,end,set(body['entities']),body['period'],body['units'],set(body['kinds']))
            started, cpu = perf_counter(), thread_time()
            value = wire(rows)
            _LOGGER.debug('Recorder statistics wire conversion: period=%s rows=%s wall_ms=%.1f cpu_ms=%.1f',
                         body['period'], sum(len(values) for values in rows.values()),
                         (perf_counter()-started)*1000, (thread_time()-cpu)*1000)
            return value
        return wire(await self.history.states(start,end,body['entities'],with_attributes=body['with_attributes']))

    async def publish(self, value):
        if self.projection:
            await self.projection.accept(value)

    def install_capacity(self, value):
        if value['revision'] >= self.capacity['revision']:
            self.capacity = value
            descriptors = [descriptor for row in value['devices'].values()
                for descriptor in [*row['effects'].values(), *([row['settled']] if row.get('settled') else [])]]
            self.capacity_entities = {entity for descriptor in descriptors
                for entity in [*descriptor['controls'], descriptor['sensor']] if entity}
            self.entities.update(self.capacity_entities)

    async def refresh_capacity(self):
        value = await self.service.stream.call('load_record', 'grid_capacity')
        if value is not None:
            self.install_capacity(value)
        merge_power_peaks(self.power_peaks, await self.service.stream.call('load_record', 'power_peaks') or {})

    def power_history_sources(self):
        option_sets = [self.options(), *(record['options'] for record in
            self.service.physical.ownership.records.values() if record.get('options'))]
        return {sensor for options in option_sets
            for mapping in options.get('device_control_mappings', {}).values()
            if isinstance(sensor := mapping.get('power'), str) and '.' in sensor}

    def sum_power_members(self, source):
        """Current leaf meters of a native SUM helper, including nested sums."""
        if not isinstance(source, str):
            return None
        registry = er.async_get(self.hass)

        def visit(entity, ancestors):
            item = registry.async_get(entity)
            if not item or item.platform != 'group' or not item.config_entry_id:
                return None
            entry = self.hass.config_entries.async_get_entry(item.config_entry_id)
            if not entry or entry.options.get('type') != 'sum':
                return None
            if entity in ancestors:
                return ()
            leaves = []
            for reference in entry.options.get('entities', ()):
                member = er.async_resolve_entity_id(registry, reference)
                if member is None:
                    return ()
                nested = visit(member, ancestors | {entity})
                if nested == ():
                    return ()
                leaves.extend((member,) if nested is None else nested)
            return tuple(leaves)
        return visit(source, set())

    def power_setting_sources(self):
        """(Power sensor, native power setting) pairs whose peaks are kept per setting value."""
        option_sets = [self.options(), *(record['options'] for record in
            self.service.physical.ownership.records.values() if record.get('options'))]
        return {(sensor, setting) for options in option_sets
            for mapping in options.get('device_control_mappings', {}).values()
            if isinstance(sensor := mapping.get('power'), str) and '.' in sensor
            and isinstance(setting := mapping.get('power_setting_entity_id'), str) and '.' in setting}

    def mapped_power_bound(self, mapping):
        if draw := self.device_draw(mapping):
            return draw.startup_w
        setting = mapping.get('power_setting_entity_id')
        state = (self.report(setting) or {}).get('state') if isinstance(setting, str) else None
        return device_power_bound(mapping, self.power_peaks, state,
                                  members=self.sum_power_members(mapping.get('power')))

    @staticmethod
    def draw_binding(mapping):
        meter = mapping.get('power')
        if (mapping.get('control_type') != 'setpoint' or mapping.get('power_setting_entity_id')
                or not isinstance(meter, str) or not meter.startswith('sensor.')):
            return None
        targets = actuator_targets(mapping)
        if len(targets) == 1 and targets[0].startswith('climate.'):
            return DrawBinding(targets[0], meter)
        return None

    def device_draw(self, mapping):
        binding = self.draw_binding(mapping)
        draw = self.device_draws.get(binding)
        if draw is not None:
            attrs = (self.report(binding.actuator) or {}).get('attributes', {})
            modes = set(attrs.get('hvac_modes') or ())
            if 'heat' in modes and modes <= {'heat', 'off'} and self.sum_power_members(binding.meter) is None:
                return draw
        return None

    def refresh_draw_bindings(self, groups):
        mappings = self.options().get('device_control_mappings', {})
        group_members = {member for members in groups.values() if members for member in members}
        bindings = {binding for mapping in mappings.values()
                    if (binding := self.draw_binding(mapping)) is not None
                    and groups[binding.meter] is None and binding.meter not in group_members
                    and all(actuator_targets(other) == [binding.actuator]
                            for other in mappings.values() if other.get('power') == binding.meter)}
        if bindings != self.draw_bindings:
            self.draw_bindings = bindings
            self.device_draws = {binding:draw for binding,draw in self.device_draws.items() if binding in bindings}
            self.draw_refresh.set()

    async def learn_device_draws(self):
        """Rebuild from retained Recorder evidence outside the command path."""
        while not self.closed:
            self.draw_refresh.clear()
            bindings = self.draw_bindings
            learned = {}
            end = datetime.now(timezone.utc)
            for binding in sorted(bindings, key=lambda item: (item.actuator, item.meter)):
                try:
                    rows = await self.history.states(end-HISTORY_WINDOW, end,
                        [binding.actuator, binding.meter], with_attributes=True)
                    draw = await self.hass.async_add_executor_job(learn_device_draw, binding,
                        rows.get(binding.actuator, ()), rows.get(binding.meter, ()))
                    if draw is not None:
                        learned[binding] = draw
                except Exception:
                    _LOGGER.exception('Device power attribution history unavailable for %s; will retry', binding.actuator)
            if self.closed:
                return
            if not self.draw_refresh.is_set() and self.device_draws != learned:
                self.device_draws = learned
                _LOGGER.info('Learned device power from heating cycles: %s',
                    {binding.actuator:(draw.startup_w, draw.running_w, draw.cycles)
                     for binding,draw in learned.items()})
                self.wake_heating_capacity()
            try:
                await asyncio.wait_for(self.draw_refresh.wait(), REFRESH_INTERVAL.total_seconds())
            except TimeoutError:
                pass

    def metered_thermostat(self, owner, entity, sensor):
        if (not owner or not entity.startswith('climate.') or not sensor
                or device_mode(self.options(), owner) != 'controlling'
                or self.sum_power_members(sensor) is not None):
            return False
        record = self.service.physical.ownership.records.get(owner, {})
        target = record.get('last_commands', {}).get(entity)
        if target is None or set(record['originals']) != {entity}:
            return False
        native = self.report(entity)
        if not native or native['state'] != 'heat':
            return False
        attrs = native['attributes']
        try:
            power(self.report(sensor), source=sensor)
            return (attrs.get('hvac_action') in ('heating', 'idle')
                    and finite(attrs['temperature']) == target)
        except (KeyError, TypeError, ValueError):
            return False

    def observed_device_bound(self, effect):
        if self.metered_thermostat(effect['logical'], effect['changed'], effect['sensor']):
            return 0.0
        return effect['before_w']

    def reserved_power_bound(self, row):
        """Use current member bounds for settled loads and preserve issued effects."""
        settled = row.get('settled', {})
        source = settled.get('sensor')
        owner = settled.get('owner') or settled.get('logical')
        limit = None
        targets = settled.get('controls', {})
        if len(targets) == 1 and self.metered_thermostat(owner, next(iter(targets)), source):
            # Only the enabled peak is released. Actual draw remains in the
            # meter partition, and capacity_bound retains every pending effect.
            limit = 0.0
        elif owner and settled.get('after_w') and (
                self.sum_power_members(source) is not None
                or len(targets) == 1 and DrawBinding(next(iter(targets)), source) in self.device_draws):
            record = self.service.physical.ownership.records.get(owner, {})
            options = record.get('options', self.options())
            mappings = options.get('device_control_mappings', {})
            limits = [self.mapped_power_bound(mapping) for key, mapping in mappings.items()
                      if mapping.get('power') == source
                      and (not owner.startswith('device:') or key == owner.removeprefix('device:'))
                      and set(actuator_targets(mapping)) == set(settled['controls'])]
            if limits and all(value is not None for value in limits):
                limit = max(limits)
        return capacity_bound(row, settled_limit=limit)

    def observe_setting_peaks(self, row):
        raised = False
        for sensor, setting in self.power_settings:
            if row and sensor == row['entity_id']:
                raised |= observe_setting_peak(self.power_peaks, row, setting, (self.report(setting) or {}).get('state'))
        return raised

    def collect_power_history(self):
        if self.closed:
            return
        roots = self.power_history_sources()
        groups = {source: self.sum_power_members(source) for source in roots}
        self.refresh_draw_bindings(groups)
        if self.power_groups != groups:
            self.power_groups = groups
            self.wake_heating_capacity()

        def leaves(source):
            members = groups[source]
            return (source,) if members is None else members
        self.power_entities = {leaf for source in roots for leaf in leaves(source)}
        self.power_settings = {(leaf, setting) for source, setting in self.power_setting_sources()
                               for leaf in leaves(source)}
        sources = lambda: self.power_entities | self.power_settings
        if self.power_history_pending or not sources() - self.power_history_loaded:
            return
        self.power_history_pending = True
        async def collect():
            retries = RetryBackoff()
            try:
                while not self.closed and (pending := sources() - self.power_history_loaded):
                    entities = {item for item in pending if isinstance(item, str)}
                    settings = pending - entities
                    # Subscribe/observe before the async Recorder read, so a live
                    # high-to-low transient cannot be lost during the scan.
                    for entity in entities | {sensor for sensor, _ in settings}:
                        row = self.report(entity)
                        if row:
                            observe_power_peak(self.power_peaks, {**row, 'entity_id':entity})
                            self.observe_setting_peaks({**row, 'entity_id':entity})
                    # Persist these observations before waiting on Recorder;
                    # a failed scan must not lose a positive startup sample.
                    await self.service.stream.call('save_record', 'power_peaks', self.power_peaks)
                    try:
                        history = await self.history.power_peaks(entities) if entities else {}
                        if settings:
                            history = {**history, **await self.history.setting_power_peaks(settings)}
                    except Exception:
                        _LOGGER.exception('Could not read device peak power history; retrying')
                        await asyncio.sleep(retries.next())
                        continue
                    if self.closed:
                        return
                    updated = merge_power_peaks(self.power_peaks, history)
                    await self.service.stream.call('save_record', 'power_peaks', self.power_peaks)
                    self.power_history_loaded.update(pending)
                    if updated:
                        self.wake_heating_capacity()
                    _LOGGER.info('Device historical power peaks (W): %s',
                        {key:self.power_peaks[key] for key in sorted(self.power_peaks)
                         if key in entities or key.partition('@')[0] in {sensor for sensor, _ in settings}})
                    retries.reset()
            finally:
                self.power_history_pending = False
        self.entry.async_create_background_task(self.hass, collect(), name='shs_device_power_history')

    def wake_heating_capacity(self):
        if self.heating_waiting:
            self.heating_waiting.clear()
            self.service.wake_heating_capacity()

    def requires_command(self, entity, value):
        # A stale equality cannot cancel an accepted, still outstanding effect.
        for device, row in self.capacity['devices'].items():
            settled = row.get('settled', {})
            partial = entity in settled.get('controls', {}) and entity not in settled.get('acknowledgements', settled.get('controls', {}))
            if not partial and not any(effect['order'] > settled.get('order', -1)
                       for effect in row['effects'].values()):
                continue
            controls = commanded_controls(self.capacity, device)
            if entity in controls and str(controls[entity]) != str(value):
                try:
                    if float(controls[entity]) == float(value):
                        continue
                except (ValueError, TypeError):
                    pass
                return True
        return False

    def lowers_heating_target(self, entity, value):
        state = self.report(entity)
        if not state or state['state'] != 'heat':
            return False
        try:
            if value > finite(state['attributes'].get('temperature')):
                return False
            # The live target can lag an earlier accepted write. Check pending
            # settings too, including when this command has just been prepared.
            for row in self.capacity['devices'].values():
                settled_order = row.get('settled', {}).get('order', -1)
                for effect in row['effects'].values():
                    controls = effect['controls']
                    if effect['order'] > settled_order and entity in controls and value > finite(controls[entity]):
                        return False
        except (TypeError, ValueError):
            return False
        return True

    def power_source(self, mapping, key):
        sensor = mapping.get('power')
        if isinstance(sensor, str) and sensor.startswith('sensor.'):
            return sensor
        row = self.report(key) if key.startswith('sensor.') else None
        attributes = (row or {}).get('attributes', {})
        return key if attributes.get('unit_of_measurement') in ('W','kW') and attributes.get('state_class') == 'measurement' else None

    def import_power_source(self, mappings):
        sensors = {self.power_source(mapping, key) for key, mapping in mappings}
        if len(sensors) > 1:
            # Counter aliases may retain different meters. Resolve the explicitly
            # admitted physical source, never whichever happens to report first.
            sensors = {sensor for sensor in sensors if 'capture' in self.bindings.get(sensor, ())}
        if len(sensors) != 1:
            raise ValueError('Import accounting requires one admitted power source')
        return sensors.pop()

    def mark_dispatch(self, command):
        self.service.stream.mark_dispatch(command.id)

    def describe_stop(self, entity):
        # OFF cannot add house demand and needs no unrelated current/rating
        # reading. Retain any previous peak until its physical response arrives.
        for device, row in self.capacity['devices'].items():
            descriptors = [*row['effects'].values(), *([row['settled']] if row.get('settled') else [])]
            for previous in sorted(descriptors,key=lambda d:d['order'],reverse=True):
                if entity in previous['controls']:
                    previous = {key:value for key,value in previous.items() if key != 'sum_power_w'}
                    bound = self.reserved_power_bound(row)
                    return {**previous,'changed':entity,
                        'controls':{**commanded_controls(self.capacity,device),entity:'off'},
                        'acknowledgements':{entity:'off'},
                        'before_w':bound,'commanded_before_w':bound,
                        'after_w':0.0,'possible_w':0.0}
        return None

    def describe_command(self, command):
        action = command.action
        if action.service == 'turn_off' or action.service == 'set_hvac_mode' and action.value == 'off':
            return self.describe_stop(action.entity)
        value = action.service.removeprefix('turn_') if action.service in ('turn_on', 'turn_off') else action.value
        effect = self.describe_import(command.device, action.entity, value)
        if action.service == 'set_temperature' and self.lowers_heating_target(action.entity, value):
            return {**effect, 'owner': command.device} if effect is not None else None
        if effect is not None and effect['logical'] != 'battery':
            required = self.import_required(effect=effect)
            effect = {**effect, 'support_w':max(0.0,required-self.house_import_limit()),
                      'owner':command.device}
        return effect

    def describe_import(self, device, entity, value):
        options = self.options()
        battery = self.service.battery
        base = dict(device=device, logical=device, changed=entity, signed=False, scale=1.0, overhead_w=0.0,
                    response_tolerance_w=0.0)
        if battery.installation is not None and entity in {
                battery.installation[3][key] for key in ('battery_mode_entity',
                    'battery_charge_limit_entity', 'battery_discharge_limit_entity')}:
            _, catalog, conversion, installed = battery.installation
            battery.surface(installed)  # Validate current native units and metadata.
            keys = ('battery_mode_entity', 'battery_charge_limit_entity', 'battery_discharge_limit_entity')
            controls = {installed[key]:(self.report(installed[key])['state'] if key == 'battery_mode_entity'
                        else float(canonical_metadata(self.report(installed[key])['state'],self.report(installed[key])['attributes']))) for key in keys}
            identity = capacity_key('battery', controls)
            commanded = {**controls, **commanded_controls(self.capacity, identity)}
            value = value if entity == installed['battery_mode_entity'] else float(canonical_metadata(value,self.report(entity)['attributes']))
            expected = {**commanded, entity:value}
            def envelope(values):
                charge = self.report(installed['battery_charge_limit_entity'])
                ceiling = native_watts(values[installed['battery_charge_limit_entity']],charge['attributes'])
                return battery_import_bound(values[installed['battery_mode_entity']],ceiling,catalog.charge_max_w,conversion)
            return {**base, 'device':identity, 'logical':'battery', 'before_w':envelope(controls), 'commanded_before_w':envelope(commanded),
                'after_w':envelope(expected),
                'possible_w':max(envelope(expected),envelope({**controls,entity:value})),
                'controls':expected, 'sensor':installed['battery_power_measurement_entity'],
                'signed':True, 'scale':1/conversion.grid_charge.gain,
                'overhead_w':conversion.grid_charge.overhead_w, 'response_tolerance_w':100.0,
                'native_model':{'mode':installed['battery_mode_entity'], 'charge':installed['battery_charge_limit_entity'],
                    'charge_max_w':catalog.charge_max_w, 'charge_unit':self.report(installed['battery_charge_limit_entity'])['attributes']['unit_of_measurement']}}
        record = self.service.physical.ownership.records.get(device, {})
        options = record.get('options', options)
        mappings = options.get('device_control_mappings', {})
        if device == 'ev':
            if not self.service.physical.restoring:
                # The captured options belong to explicit handover. A meter
                # change keeps ownership, but import checks use the admitted
                # current mapping just as scheduled device execution does.
                options = self.options()
                mappings = options.get('device_control_mappings', {})
            switch = options['ev_charge_switch_entity']
            ev = [(key,m) for key,m in mappings.items() if m.get('control_type') == 'variable_power'
                  and m.get('control_entity_id') and (not record or m.get('control_entity_id') in record['originals'])]
            currents = {mapping['control_entity_id'] for _, mapping in ev}
            if len(currents) != 1:
                raise ValueError('EV import accounting requires one reviewed current control')
            current = currents.pop()
            sensor = self.import_power_source(ev)
            if self.report(switch)['state'] == 'off' and entity == switch and value == 'off':
                return {**base,'device':capacity_key('ev',(switch,current)),
                    'before_w':0.0,'commanded_before_w':0.0,'after_w':0.0,'possible_w':0.0,
                    'controls':{switch:'off'},'sensor':sensor}
            if self.report(current)['attributes'].get('unit_of_measurement') != 'A':
                raise ValueError('EV current control must report amperes')
            controls = {switch:self.report(switch)['state'], current:float(canonical_metadata(self.report(current)['state'],self.report(current)['attributes']))}
            if controls[switch] not in ('on','off'):
                raise ValueError('EV charging switch is unavailable for house import accounting')
            identity = capacity_key('ev', controls)
            commanded = {**controls, **commanded_controls(self.capacity, identity)}
            expected = {**commanded, entity:value}
            def demand(values):
                return _current(values[current])*EV_WATTS_PER_AMP if values[switch] == 'on' else 0.0
            return {**base, 'device':identity, 'before_w':demand(controls), 'commanded_before_w':demand(commanded), 'after_w':demand(expected),
                    'possible_w':max(demand(expected),demand({**controls,entity:value})),
                    'controls':expected, 'sensor':sensor}
        matches = [(key,m) for key,m in mappings.items() if entity in actuator_targets(m)
                   and (not device.startswith('device:') or key == device.removeprefix('device:'))]
        if len(matches) > 1 and len({tuple(sorted(actuator_targets(m))) for _,m in matches}) == 1:
            sensor = self.import_power_source(matches)
            matches = [max((item for item in matches if self.power_source(item[1],item[0]) == sensor),
                           key=lambda item:self.mapped_power_bound(item[1]) or 0)]
        if len(matches) != 1:
            if value == 'off':
                return None
            raise ValueError(f'{device}: no unambiguous reviewed power mapping for {entity}')
        key, mapping = matches[0]
        rated = self.mapped_power_bound(mapping)
        if rated is None:
            if value == 'off':
                return None
            raise PowerHistoryError(key, mapping)
        controls = {target:(self.report(target) or {}).get('state') for target in actuator_targets(mapping)}
        identity = capacity_key('device:'+key, controls)
        commanded = {**controls, **commanded_controls(self.capacity, identity)}
        expected = {**commanded, entity:value}
        def demand(values):
            return rated if any(state != 'off' for state in values.values()) else 0.0
        return {**base, 'device':identity, 'before_w':demand(controls), 'commanded_before_w':demand(commanded), 'after_w':demand(expected),
                    'possible_w':max(demand(expected),demand({**controls,entity:value})),
                'controls':expected, 'sensor':self.power_source(mapping,key),
                **({'draw_power_w': rated} if self.device_draw(mapping) is not None else {}),
                **({'sum_power_w': rated} if self.sum_power_members(mapping.get('power')) is not None else {})}

    def check_command(self, command):
        action = command.action
        if action.service == 'turn_off' or action.service == 'set_hvac_mode' and action.value == 'off':
            return
        if action.service == 'set_temperature' and self.lowers_heating_target(action.entity, action.value):
            return
        effect = self.describe_command(command)
        if effect is None:
            return
        prepared = self.capacity['devices'].get(effect['device'], {}).get('effects', {}).get(command.id, effect)
        reducing = action.service == 'turn_off' or (
            effect['logical'] in ('ev','battery') and effect['possible_w'] <= effect['before_w']
            and effect['after_w'] <= prepared['commanded_before_w'])
        if (effect['logical'] == 'battery' and action.entity == self.service.battery.installation[3]['battery_mode_entity']
                and action.value in CHARGING_MODES and effect['possible_w'] > 0):
            reducing = False
        if reducing and effect['logical'] == 'battery':
            installed = self.service.battery.installation[3]
            mode, ceiling = installed['battery_mode_entity'],installed['battery_discharge_limit_entity']
            before = battery_discharge_ceiling(self.report(mode)['state'],_current(self.report(ceiling)['state']))
            after = battery_discharge_ceiling(effect['controls'][mode],effect['controls'][ceiling])
            reducing = after >= before
        if reducing:
            return
        if (effect['logical'] == 'battery' and effect['after_w'] > 0
                and any(claim['hold_charge'] for claim in self.current_import_claims())):
            raise ControlDeadlineError('A device is waiting for battery charging to release grid capacity')
        self.admit_import(effect)

    def battery_capacity_key(self, installed=None):
        options = self.options() if installed is None else installed
        return capacity_key('battery', [options[key] for key in ('battery_mode_entity',
            'battery_charge_limit_entity', 'battery_discharge_limit_entity')])

    def current_import_claims(self, *, exclude_owner=None):
        now = datetime.now(timezone.utc)
        context = self.service.context()
        self.import_claims = {owner:row for owner,row in self.import_claims.items()
            if row['context'] == context and (row['end'] is None or now < row['end'])}
        return [row['effect'] for owner,row in self.import_claims.items() if owner != exclude_owner]

    def clear_import_claim(self, owner):
        if self.import_claims.pop(owner, None) is not None:
            self.priority_cache = None

    def claim_import(self, intent, entity, value):
        """Retain only feasible, described demand that is waiting for charging to yield."""
        battery = self.service.battery
        can_release_battery = (battery.installation is not None
            and device_mode(self.options(), 'battery') == 'controlling'
            and battery.fence.snapshot()['grant_current'])
        effect = self.describe_import(intent.device, entity, value)
        if effect is None:
            return False
        excluded_battery = self.battery_capacity_key() if can_release_battery else None
        reserved = [*self.heating_effects().values(), *self.current_import_claims(exclude_owner=intent.device)]
        limit = self.house_import_limit()
        if not can_release_battery or self.import_required(effect=effect, exclude_device=excluded_battery,
                                                           reserved=reserved) > limit:
            if intent.operation != 'apply' or intent.device == 'ev':
                return False
            now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
            ev = self.ev_candidate(now_ms)
            if ev is None or max(ev['present_w'], ev['want_w'], ev['draw_w']) <= ev['scheduled_w']:
                return False
            # Reserve a scheduled start that fits after optional EV current
            # yields. Native admission still waits for the measured reduction.
            house_without_ev = max(0.0, self.house_power() - ev['draw_w'])
            required = self.import_required(effect=effect, exclude_device=excluded_battery,
                exclude_devices=(ev['identity'],), reserved=reserved, house_w=house_without_ev)
            if required + ev['scheduled_w'] > limit:
                return False
        end = (datetime.fromisoformat(intent.parameters['start']) + timedelta(minutes=15)
               if intent.operation == 'apply' else None)
        # Every described scheduled start needs only its incremental watts.
        # Keep the full-release handshake for explicit heating overrides.
        claim = {'context':self.service.context(), 'end':end,
                 'effect':{**effect, 'hold_charge':intent.operation != 'apply'}}
        if self.import_claims.get(intent.device) != claim:
            self.import_claims[intent.device] = claim
            self.priority_cache = None
        return True

    def battery_capacity_sources(self):
        battery = self.service.battery
        if battery.installation is None:
            return ()
        installed = battery.installation[3]
        return tuple(installed[key] for key in ('battery_power_measurement_entity',
            'battery_mode_entity', 'battery_discharge_limit_entity', 'battery_charge_limit_entity'))

    def measured_control_power(self, entity, *, signed=False):
        report = self.report(entity)
        try:
            return power(report, source=entity, signed=signed)[0]
        except (TypeError, ValueError, KeyError) as error:
            raise ControlObservationError(str(error), entity,
                'Wait for the power reading to return; control resumes automatically.',
                unavailable=not report or report.get('state') in ('unknown', 'unavailable')) from error

    # ---- Live run priority (user requirement, 10 November 2026) ----
    # The car, the house battery and eligible heaters share the measured
    # import budget by how much each whole run can still give up. This
    # replaces the earlier charger-first relief rule; docs/run-priority.md.

    def ev_candidate(self, now_ms, own=None):
        """The charging car, from its executing request or its accepted intention."""
        options = self.options()
        value = self.capacity.get('coordination', {}).get('intents', {}).get('ev')
        run = None
        current = False
        if value and value.get('operation') == 'apply':
            start = int(datetime.fromisoformat(value['parameters']['start']).timestamp() * 1000)
            current = start <= now_ms < start + run_priority.QUARTER_MS
            run = value['parameters'].get('run') if current else None
        if own is None:
            # Another owner's decision may count on the car only while its own
            # executor could act on the result right now.
            if device_mode(options, 'ev') != 'controlling' or not current or value['parameters']['current_a'] <= 0:
                return None
            try:
                intent = DeviceIntent.parse(value)
                self.service.authorize_device(intent)
                physical = self.service.physical
                if '$ev' in options.get('excluded_device_readings', ()) or not options.get('ev_enabled', True):
                    return None
                for override in (options.get('control_override_entity'), options.get('ev_control_override_entity')):
                    if override and physical.state(override).state != 'off':
                        return None
                record = physical.ownership.records.get('ev', {})
                entity, minimum = self.flexible_ev(options, record)
                switch = options['ev_charge_switch_entity']
                physical.actuator_state(entity)
                ev_current_ceiling(intent.parameters['current_a'] * EV_WATTS_PER_AMP,
                    self.report(entity)['attributes'], minimum)
                if physical.actuator_state(switch).state != 'on':
                    return None
                # These inputs are required by execute_ev before it can reduce.
                if physical.state(options['ev_connected_entity']).state not in ('on', 'off', 'true', 'false', 'connected', 'disconnected'):
                    return None
                physical.fraction(options['ev_soc_entity'])
                physical.fraction(options['ev_target_soc_entity'])
                own = dict(entity=entity, switch=switch, requested=intent.parameters['current_a'], minimum=minimum)
                return self._ev_candidate(own, run, now_ms)
            except (AdmissionPending, ControlObservationError, ValueError, KeyError, TypeError):
                return None  # Missing evidence cannot promise that the car will yield.
        return self._ev_candidate(own, run, now_ms)

    def _ev_candidate(self, own, run, now_ms):
        entity, switch = own['entity'], own['switch']
        native = self.report(entity)
        effect = self.describe_import('ev', switch, 'on')
        draw = self.measured_control_power(effect['sensor'])
        observed = float(canonical_metadata(native['state'], native['attributes']))
        issued = finite(commanded_controls(self.capacity, effect['device'], issued_only=True).get(entity, observed))
        # An issued boost belongs to the run until priority reclaims it.
        wanted = max(own['requested'], min(issued, own.get('maximum', issued)))
        step = finite(native['attributes'].get('step') or 1)
        candidate = dict(owner='ev', kind='ev', identity=effect['device'], draw_w=draw, metered=True,
            want_w=wanted * EV_WATTS_PER_AMP, step_w=step * EV_WATTS_PER_AMP,
            scheduled_w=own['requested'] * EV_WATTS_PER_AMP,
            floor_w=own['minimum'] * EV_WATTS_PER_AMP, present_w=effect['before_w'],
            sources=(effect['sensor'], entity, switch),
            key=('ev', native['state'], self.report(switch)['state'], draw, own['requested'], own['minimum'], own.get('maximum')))
        if run is not None:
            # Only SHS's own reductions build a shortfall. A car that takes
            # less than an untrimmed setting allows is limiting itself.
            delivered = draw if observed < own['requested'] else max(draw, run_priority.planned_now(run, now_ms))
            progress = self.run_progress['ev'] = run_priority.advance_delivery(
                self.run_progress.get('ev'), run, now_ms, delivered)
            candidate['outlook'] = run_priority.charge_outlook(run, now_ms, shortfall_wh=progress['shortfall_wh'])
        return candidate

    def battery_candidate(self, now_ms):
        """Only a current writer charging on command can reduce measured battery charge."""
        battery = self.service.battery
        if battery.installation is None or self.active_battery_target() is None:
            return None
        _, _, conversion, installed = battery.installation
        mode = self.report(installed['battery_mode_entity'])['state']
        if mode != 'Command Charging (PV First)':
            return None
        sensor = installed['battery_power_measurement_entity']
        measured = self.measured_control_power(sensor, signed=True)
        draw = conversion.grid_charge.input(measured) if measured > 0 else 0.0
        descriptor = self.describe_import('battery', installed['battery_mode_entity'], mode)
        published = self.charge_run or {}
        run, want = published.get('run'), published.get('want_w')
        if want is None:
            want = max(draw, descriptor['before_w'])
        limit = self.report(installed['battery_charge_limit_entity'])['state']
        candidate = dict(owner='battery', kind='battery', identity=descriptor['device'], draw_w=draw, metered=False,
            want_w=want, step_w=0, floor_w=0.0, present_w=max(draw, descriptor['before_w']),
            sources=(sensor, installed['battery_mode_entity'], installed['battery_charge_limit_entity']),
            key=('battery', mode, limit, draw, want))
        soc_entity = self.options().get('battery_soc_entity')
        if run is not None and soc_entity:
            try:
                soc = self.report(soc_entity)
                fraction = finite(soc['state']) / 100
                if soc['attributes'].get('unit_of_measurement') == '%' and 0 <= fraction <= 1:
                    candidate['outlook'] = run_priority.charge_outlook(
                        run, now_ms, stored_wh=fraction * run['capacity_wh'])
                    candidate['sources'] += (soc_entity,)
            except (KeyError, TypeError, ValueError):
                pass  # Unknown state of charge leaves the battery without stated slack.
        return candidate

    def heat_candidates(self, now_ms):
        """Scheduled room thermostats and relays that draw now or wait to resume."""
        options = self.options()
        if not options.get('grid_power_entity'):
            return []  # Heating yields to measured import only.
        found = []
        for owner, value in self.capacity.get('coordination', {}).get('intents', {}).items():
            if not owner.startswith('device:') or value.get('operation') != 'apply':
                continue
            try:
                candidate = self._heat_candidate(owner, value, options, now_ms)
            except (AdmissionPending, GatewayConflict, ControlObservationError, ValueError, KeyError, TypeError):
                continue  # A room with missing evidence holds; it cannot stop healthy owners.
            if candidate is not None:
                found.append(candidate)
        return found

    def _heat_candidate(self, owner, value, options, now_ms):
        key = owner.removeprefix('device:')
        parameters = value['parameters']
        start = int(datetime.fromisoformat(parameters['start']).timestamp() * 1000)
        if not start <= now_ms < start + run_priority.QUARTER_MS or device_mode(options, owner) != 'controlling':
            return None
        command = parameters['commands'][key]
        thermostat = command['type'] == 'setpoint' and 'room_target_c' in command
        if not thermostat and command['type'] != 'room_relay':
            return None
        mapping = options['device_control_mappings'][key]
        if key in options.get('excluded_device_readings', ()):
            return None
        for override in (options.get('control_override_entity'), mapping.get('control_override_entity')):
            if override and (self.report(override) or {}).get('state') != 'off':
                return None
        heating = self.service.heating
        if heating is not None and active_heating(heating.value, options, owner, heating.now()):
            return None  # An explicit room request is not scheduled heat.
        if self.heating_cap(owner) is not None:
            return None  # Already yielding to a room's explicit request.
        self.service.authorize_device(DeviceIntent.parse(value))
        entity = actuator_targets(mapping)[0]
        sensor = self.power_source(mapping, key)
        native = self.report(entity)
        if not sensor or not native or native['state'] in (None, 'unknown', 'unavailable'):
            return None
        draw = power(self.report(sensor), source=sensor)[0]
        record = self.service.physical.ownership.records.get(owner, {})
        attrs = native['attributes']
        if thermostat:
            if not entity.startswith('climate.') or native['state'] != 'heat':
                return None
            temperature = finite(attrs['current_temperature'])
            running = attrs.get('hvac_action') == 'heating'
            pause = record.get('heating_pause')
            waiting = bool(pause) and pause.get('entity') == entity and temperature < finite(pause['target_c'])
        else:
            room = mapping['temperature_entity_id']
            observed = self.report(room)
            temperature = room_temperature(room, observed['state'], observed['attributes'],
                                           self.hass.config.units.temperature_unit)
            running = native['state'] == 'on'
            waiting = not running and relay_heat(temperature, command['target_c'], native['state']) == 'on'
        if not running and not waiting:
            return None  # An idle heater only contributes its measured draw.
        want = draw if running and draw > 0 else self.mapped_power_bound(mapping)
        if not want:
            return None
        identity = self.describe_import(owner, entity, native['state'])['device']
        return dict(owner=owner, kind='heat', identity=identity, draw_w=draw, metered=True, want_w=want,
            step_w=0, floor_w=want, present_w=draw if running else 0.0, running=running,
            slack_h=run_priority.heat_slack_h(parameters.get('run'), now_ms, temperature),
            sources=(sensor, entity), key=(owner, native['state'], attrs.get('hvac_action'), attrs.get('temperature'),
                                         temperature, draw, bool(waiting)))

    def priority_pass(self, ev=None):
        """One decision over every load that can act now; None when there is none.

        The reserved demand of everything else is the costly part. Owners
        evaluated on the same readings share it, so a burst of meter reports
        costs one pass however many devices it wakes. Missing required
        readings raise, which holds every setting.
        """
        options = self.options()
        limit = self.house_import_limit()
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        candidates = [candidate for candidate in (self.ev_candidate(now_ms, ev), self.battery_candidate(now_ms),
                      *self.heat_candidates(now_ms)) if candidate is not None]
        if not candidates:
            return None
        house = self.measured_control_power(options['house_consumption_power_entity'])
        grid_entity = options.get('grid_power_entity')
        grid = self.measured_control_power(grid_entity, signed=True) if grid_entity else None
        owners = {candidate['owner'] for candidate in candidates}
        self.current_import_claims()  # Retire lapsed claims.
        claims = [row['effect'] for owner, row in self.import_claims.items() if owner not in owners]
        key = (self.observed_serial, limit, house, self.capacity['revision'], len(claims),
               tuple(candidate['key'] for candidate in candidates))
        if self.priority_cache is not None and self.priority_cache[0] == key:
            reserved = self.priority_cache[1]
        else:
            reserved = self.import_required(exclude_devices=[candidate['identity'] for candidate in candidates],
                reserved=[*self.heating_effects().values(), *claims],
                house_w=max(0.0, house - sum(c['draw_w'] for c in candidates if c['metered'])))
            reserved -= self.discharge_credit(import_after_w=reserved + sum(c['want_w'] for c in candidates))
            self.priority_cache = (key, reserved)
            self.priority_passes += 1
        other = reserved
        if grid is not None:
            # The grid meter also catches demand ahead of the gross-house
            # reading. Neither meter replaces the other.
            other = max(other, grid - sum(c['draw_w'] for c in candidates))
        budget = limit - other

        # Recovery capacity the plan leaves unused is spent once across runs.
        outlooks = {c['owner']: c['outlook'] for c in candidates if 'outlook' in c}
        for owner, slack_wh in run_priority.share_recovery(outlooks).items():
            candidate = next(c for c in candidates if c['owner'] == owner)
            candidate['slack_wh'] = slack_wh
            candidate['slack_h'] = run_priority.charge_slack_h(slack_wh, max(candidate['draw_w'], candidate['want_w']))
        previous = self.priority_last or {'granted': {}, 'wants': {}}
        wants = {c['owner']: c['want_w'] for c in candidates}
        self.priority_pending = {owner: row for owner, row in self.priority_pending.items() if owner in owners}
        for candidate in candidates:
            candidate.setdefault('slack_h', 0.0)
            owner = candidate['owner']
            candidate['trimmed'] = (owner in previous['granted']
                and previous['granted'][owner] < previous['wants'][owner] - 1e-6)
            waiting = self.priority_pending.get(owner)
            if waiting and run_priority.responded(candidate['draw_w'], waiting['granted_w']):
                del self.priority_pending[owner]
                waiting = None
            if waiting and now_ms - waiting['since_ms'] > run_priority.RESPONSE_MS:
                # The reduction never arrived. Keep asking for it, and let
                # another run absorb what this load measurably still takes.
                candidate.update(fixed=True, want_w=candidate['draw_w'], step_w=0, floor_w=0.0)
        shares, left = run_priority.share_budget(budget, candidates)
        granted = {}
        present = {c['owner']: max(c['present_w'], c['draw_w']) for c in candidates}
        present_w = sum(present.values())
        battery_spare_w = 0.0
        for candidate in candidates:
            owner, share = candidate['owner'], shares[candidate['owner']]
            if candidate.get('fixed'):
                granted[owner] = self.priority_pending[owner]['granted_w']
                continue
            room = limit - other - (present_w - present[owner])
            if share > candidate['present_w']:
                # An increase is funded only by capacity that is measurably
                # free: a reduction still in flight has not released its watts.
                # Until its meter shows the drop, a load still holds the
                # greater of its setting and its measured draw.
                share = run_priority.step_down(max(candidate['present_w'], min(share, room)), candidate['step_w'])
                if share < candidate['floor_w']:
                    share = 0.0
            granted[owner] = share
            ceiling = share
            if owner == 'battery' and 'outlook' in candidate:
                # The battery runtime decides whether it can use this spare
                # power within its accepted target. It may use only what is
                # left after the scheduled allocations and present loads.
                battery_spare_w = max(0.0, min(left, room - share))
                ceiling += battery_spare_w
            if run_priority.responded(candidate['draw_w'], ceiling):
                self.priority_pending.pop(owner, None)
            else:
                # A command in flight: no further adjustment while it lands.
                since = self.priority_pending.get(owner, {}).get('since_ms', now_ms)
                self.priority_pending[owner] = {'granted_w': ceiling, 'since_ms': since}
        previous_material = previous.get('material_since_ms', {})
        material_since = {c['owner']: min(previous_material.get(c['owner'], now_ms), now_ms) for c in candidates
                          if run_priority.material_capacity_shortfall(c, granted[c['owner']])}
        self.priority_last = dict(at_ms=now_ms, budget_w=budget, left_w=max(0.0, left), granted=granted,
            wants=wants, battery_spare_w=battery_spare_w,
            slack_h={c['owner']: c['slack_h'] for c in candidates},
            material_since_ms=material_since,
            running={c['owner']: c.get('running', True) for c in candidates},
            over_limit=grid is not None and grid > limit,
            sources=sorted({source for c in candidates for source in c['sources'] if source}))
        self._retain_run_progress(now_ms)
        return self.priority_last

    def _retain_run_progress(self, now_ms):
        """Persist carried delivery once a quarter, never on every meter report."""
        marker = (now_ms // run_priority.QUARTER_MS, tuple(sorted((owner, row['id']) for owner, row in self.run_progress.items())))
        if marker == self.run_progress_saved or not self.run_progress:
            return
        self.run_progress_saved = marker
        self.entry.async_create_background_task(self.hass,
            self.service.stream.call('save_record', 'run_progress', dict(self.run_progress)),
            name='shs_run_progress')

    def priority_sources(self):
        """Readings whose change can alter the shared decision."""
        options = self.options()
        sources = {options.get('house_consumption_power_entity'), options.get('grid_power_entity'),
                   *self.battery_capacity_sources(), *(self.priority_last or {}).get('sources', ())}
        return sorted(source for source in sources if source)

    def scheduled_heat_shed(self, owner):
        """Whether live priority leaves this scheduled heater without power now.

        A pause starts only on measured import above the limit. It lasts
        until the heater's share returns from capacity that is measurably free.
        """
        if not self.options().get('grid_power_entity'):
            return False
        try:
            decision = self.priority_pass()
        except ControlObservationError:
            # Without the readings there is no evidence of an overload, so a
            # running heater follows its schedule. One already paused holds
            # its pause until they return.
            if self.service.physical.ownership.records.get(owner, {}).get('heating_pause'):
                raise
            return False
        if decision is None or owner not in decision['granted'] or decision['granted'][owner] > 0:
            return False
        return decision['over_limit'] or not decision['running'][owner]

    def priority_must_move(self, owner):
        """Work that no longer fits its authorised windows goes to the replan path."""
        last = self.priority_last
        since = last.get('material_since_ms', {}).get(owner) if last else None
        # An EV's material deficit is already based on carried delivery. A
        # room's thermal outlook is instantaneous, so allow the response window.
        return since is not None and (owner == 'ev' or last['at_ms'] - since >= run_priority.RESPONSE_MS)

    def priority_recheck(self, owner):
        last = self.priority_last
        since = last.get('material_since_ms', {}).get(owner) if last else None
        if since is not None and owner != 'ev' and not self.priority_must_move(owner):
            return datetime.fromtimestamp((since + run_priority.RESPONSE_MS) / 1000, timezone.utc)
        return None

    def priority_condition(self, owner, result):
        """Execution evidence for a limited run that cannot finish where the plan put it."""
        if not self.priority_must_move(owner):
            return None
        if owner == 'ev':
            current = result.get('requested_current_a') or 0
            if current > 0:
                return {'status': 'capacity_limited', 'cause': 'house_import_capacity',
                        'maximum_current_a': int(current)}
        return {'status': 'temporarily_absent', 'cause': 'house_import_capacity'}

    def active_battery_target(self):
        battery = self.service.battery
        target = self.capacity.get('coordination', {}).get('withdrawal')
        if (target and battery.fence.snapshot()['grant_current'] and target['context'] == battery.context()
                and battery.now() < target['valid_until_ms']):
            return target
        return None

    def admit_import(self, effect):
        limit = self.house_import_limit()
        reserved = self.heating_effects()
        claims = self.current_import_claims() if effect and effect['logical'] == 'battery' else []
        demand = self.import_required(effect=effect, reserved=[*reserved.values(), *claims])
        credit = self.discharge_credit(effect, demand)
        required = max(0.0, demand-credit)
        if required > limit:
            if reserved and effect and effect.get('logical') not in reserved:
                # A valid room request keeps its place while other equipment reduces.
                raise HeatingDeferred('Making room for a heating override')
            detail = f' after {credit:.0f} W battery discharge' if credit else ''
            raise ControlDeadlineError(f'Waiting for house import capacity: {required:.0f} W required{detail}, {limit:.0f} W house limit')

    def discharge_credit(self, effect=None, import_after_w=0.0):
        """AC battery supply admission credits, as the plan's grid balance does.

        Measured discharge counts (user requirement, 17 October 2026). While the
        connected app publishes a capped instruction's promise, the discharge it
        promises for the import being admitted counts too (17 October, evening).
        Only the controlled battery earns either, never beyond the discharge
        ceiling of its native, journalled or proposed settings: a pending stop
        or charging setting withdraws credit before the meter responds.
        """
        battery = self.service.battery
        options = self.options()
        if battery.installation is None or device_mode(options, 'battery') != 'controlling':
            return 0.0
        _, _, conversion, installed = battery.installation
        sensor = installed['battery_power_measurement_entity']
        measured, _ = power(self.report(sensor), source=sensor, signed=True)
        mode, limit = installed['battery_mode_entity'], installed['battery_discharge_limit_entity']
        controls = {key:(self.report(key)['state'] if key == mode else _current(self.report(key)['state']))
                    for key in (mode, installed['battery_charge_limit_entity'], limit)}
        row = self.capacity['devices'].get(capacity_key('battery', controls), {})
        settings = [controls, *(d['controls'] for d in [*row.get('effects', {}).values(),
                    *([row['settled']] if row.get('settled') else [])])]
        if effect is not None and effect['logical'] == 'battery':
            settings += [effect['controls'], {**controls, effect['changed']:effect['controls'][effect['changed']]}]
        withdrawal = self.active_battery_target()
        matching_withdrawal = withdrawal is not None and set(withdrawal['controls']) == set(controls)
        if matching_withdrawal:
            settings.append(withdrawal['controls'])
        ceiling = min(battery_discharge_ceiling(values[mode], native_watts(values[limit],self.report(limit)['attributes']))
                      for values in settings)
        if matching_withdrawal and 'route_support_w' in withdrawal:
            ceiling = min(ceiling, withdrawal['route_support_w'])
        credit = conversion.discharge.output(min(max(0.0, -measured), ceiling))
        offer = self.service.current_battery_cover()
        if offer is None or datetime.now(timezone.utc).timestamp() * 1000 >= offer['valid_until_ms']:
            return credit
        house_entity = options.get('house_consumption_power_entity')
        solar_entity = options.get('solar_production_power_entity')
        if not house_entity or not solar_entity:
            return credit
        try:
            house, _ = power(self.report(house_entity), source=house_entity)
            pv, _ = power(self.report(solar_entity), source=solar_entity)
            bounds = self.native_load_bounds() if offer['include_base'] else {}
            loads = sum(max(power(self.report(meter), source=meter)[0], bounds.get(meter,0)) for meter in offer['meters'])
        except ValueError:
            return credit  # The observed discharge does not depend on the promised load partition.
        eligible = min(house, max(0.0, house - loads if offer['include_base'] else loads))
        bound = proportional_supply(house, pv, eligible).house_supply_bound_w
        return max(credit, promised_credit_w(import_after_w=max(0.0, import_after_w),
            import_target_w=offer['import_target_w'], eligible_bound_w=bound,
            discharge_ceiling_ac_w=min(offer['discharge_ac_max_w'], conversion.discharge.output(ceiling))))

    async def capacity_update(self, operation, value):
        self.install_capacity(await self.service.stream.call('coordinate_capacity',operation,value))

    async def capacity_intention(self, value):
        """Called under the existing device lock, after current owner authorization."""
        await self.refresh_capacity()
        if value['operation'] in ('heat', 'curtail'):
            return value
        await self.capacity_update('intention',value)
        if value['operation'] != 'apply':
            return value
        caps = self.capacity.get('coordination',{}).get('caps',{})
        cap = next((c for c in caps.values() if c['owner'] == value['device']),None)
        if cap is None:
            return value
        if cap.get('heating') and not self.heating_cap_current(cap):
            await self.capacity_update('remove_cap', cap['device'])
            return value
        if cap.get('heating') and value['device'] in self.heating_effects():
            # A newer explicit user request outranks an existing scheduled cap.
            return value
        owner = value['device']
        if cap.get('thermostat'):
            resume = self.capacity_resume_effect(cap)
            if resume is not None:
                try:
                    self.admit_import(resume)
                except ControlDeadlineError:
                    pass
                else:
                    await self.capacity_update('remove_cap',cap['device'])
            return value
        if owner == 'ev':
            entity, low, _ = self.service.physical.ev_mapping(self.options())
            effect = self.describe_import(owner,entity,value['parameters']['current_a'])
            if effect['device'] != cap['device']:
                return value
            resume = self.capacity_resume_effect(cap)
            try:
                if resume is None:
                    raise ControlDeadlineError('Waiting for original-demand capacity evidence')
                self.admit_import(resume)
            except ControlDeadlineError:
                pass
            else:
                await self.capacity_update('remove_cap',cap['device'])
                return value
            return capped_intention(value,cap['maximum_w'],native=self.report(entity)['attributes'],logical_minimum_a=low)
        # Non-variable owners use their existing stop/permission semantics;
        # active hot-water cycles and protected handovers still decide there.
        record = self.service.physical.ownership.records.get(owner)
        if record is None or set(record['originals']) != set(cap['resume_controls']):
            return value
        parameters = value['parameters']
        if owner == 'pool':
            requested = parameters['pool_permitted']
        else:
            command = parameters['commands'][owner.removeprefix('device:')]
            requested = command['type'] == 'room_relay' or command.get('permitted') or command.get('on_seconds',0) > 0
        if requested:
            effect = self.capacity_resume_effect(cap)
            if effect is not None:
                try:
                    self.admit_import(effect)
                except ControlDeadlineError:
                    pass
                else:
                    await self.capacity_update('remove_cap',cap['device'])
                    return value
        return capped_intention(value,cap['maximum_w'])

    def heating_cap_current(self, cap):
        heating = self.service.heating
        options = self.options()
        now = heating.now()
        if cap['owner'] not in heating.value['order']:
            return False
        return any((active := active_heating(heating.value, options, owner, now))
                   and active['revision'] == revision
                   for owner, revision in cap.get('heating', {}).items())

    def heating_cap(self, owner):
        return next((cap for cap in self.capacity.get('coordination', {}).get('caps', {}).values()
                     if cap['owner'] == owner and cap.get('heating')), None)

    def heating_effects(self):
        heating = self.service.heating
        if heating is None:
            return {}
        options, now = self.options(), heating.now()
        effects = {}
        for owner in heating_members(heating.value):
            try:
                if row := active_heating(heating.value, options, owner, now):
                    entity, *sensors = heating_binding(options, owner)
                    native = self.report(entity) or {}
                    if native.get('state') in (None, 'unknown', 'unavailable'):
                        continue
                    record = self.service.physical.ownership.records.get(owner, {})
                    if entity.startswith('climate.') and native['state'] != 'heat':
                        if native['state'] != 'off' or record.get('heating_suspended') != entity:
                            continue
                    if record and set(record['originals']) != {entity}:
                        continue
                    mapping = options['device_control_mappings'][owner.removeprefix('device:')]
                    for override in (options.get('control_override_entity'), mapping.get('control_override_entity')):
                        if override and (self.report(override) or {}).get('state') != 'off':
                            break
                    else:
                        value = row['target_c']
                        if sensors:
                            sensor = sensors[0]
                            observed = self.report(sensor)
                            temperature = room_temperature(sensor, observed['state'], observed['attributes'],
                                                           self.hass.config.units.temperature_unit)
                            value = relay_heat(temperature, value, native['state'])
                            if value == 'off':
                                continue
                        effects[owner] = self.describe_import(owner, entity, value)
            except (ValueError, KeyError, TypeError, ControlObservationError):
                # A room with missing physical evidence cannot suppress healthy owners.
                continue
        return effects

    def heating_request_current(self, request):
        heating = self.service.heating
        active = active_heating(heating.value, self.options(), request['device'], heating.now())
        parameters = request['parameters']
        return bool(active and (active['revision'], active['target_c']) == (parameters['revision'], parameters['target_c'])
                    and request['device'] in self.heating_effects())

    @staticmethod
    def flexible_ev(options, record):
        switch = options['ev_charge_switch_entity']
        candidates = {(mapping['control_entity_id'], mapping['minimum_value'])
                      for mapping in options.get('device_control_mappings', {}).values()
                      if mapping.get('control_type') == 'variable_power'
                      and mapping.get('control_entity_id') in record['originals']}
        if len(candidates) != 1 or switch not in record['originals']:
            raise ValueError('EV has no current reduction control')
        return candidates.pop()

    async def make_heating_room(self, request):
        """Use spare capacity before asking ranked owners for physical relief."""
        from uuid import uuid4

        owner_request = request['device']
        self.heating_waiting.discard(owner_request)
        if not self.heating_request_current(request):
            return False
        physical = self.service.physical
        reductions = []
        battery_needed = False
        async with physical.lock:
            await self.refresh_capacity()
            if not self.heating_request_current(request):
                return False
            reserved = self.heating_effects()
            effect = reserved[request['device']]
            try:
                demand = self.import_required(effect=effect, reserved=reserved.values())
                deficit = demand - self.discharge_credit(effect, demand) - self.house_import_limit()
            except (ControlObservationError, ValueError, KeyError, TypeError):
                return False  # The room's own execution reports the actual missing evidence.
            if deficit <= 0:
                return False
            self.heating_waiting.add(owner_request)
            options = self.options()
            heating = self.service.heating
            now = heating.now()
            coordination = self.capacity.get('coordination', {})
            caps = {}
            for owner in heating.value['order']:
                if deficit <= 0:
                    break
                if (active_heating(heating.value, options, owner, now)
                        or device_mode(options, owner) != 'controlling'):
                    continue
                if owner == 'battery':
                    battery = self.service.battery
                    if battery.installation is not None:
                        installed = battery.installation[3]
                        mode = installed['battery_mode_entity']
                        try:
                            descriptor = self.describe_import(owner, mode, self.report(mode)['state'])
                        except (ControlObservationError, ValueError, KeyError, TypeError):
                            continue
                        possible = max(descriptor['before_w'], self.reserved_power_bound(self.capacity['devices'].get(descriptor['device'], {})))
                        if possible > 0:
                            battery_needed = True
                            deficit -= possible
                    continue
                value = coordination.get('intents', {}).get(owner)
                try:
                    if (value is None or value['operation'] != 'apply'
                            or not physical.capacity_reduction_available(owner, value)):
                        continue
                except (ControlObservationError, ValueError, KeyError, TypeError):
                    continue
                record = physical.ownership.records.get(owner)
                if not record:
                    continue
                identity = capacity_key(owner, record['originals'])
                row = self.capacity['devices'].get(identity, {})
                bound = self.reserved_power_bound(row)
                if bound <= 0:
                    continue
                thermostat = False
                if owner.startswith('device:'):
                    key = owner.removeprefix('device:')
                    mapping = options.get('device_control_mappings', {}).get(key, {})
                    kind = mapping.get('control_type')
                    if kind not in ('setpoint', 'switch_schedule', 'permit_inhibit'):
                        continue
                    if kind == 'setpoint':
                        entity = next(iter(record['originals']), '')
                        native = self.report(entity) or {}
                        sensor = self.power_source(mapping, key)
                        thermostat = (entity.startswith('climate.') and sensor and (native.get('state') == 'heat'
                            or native.get('state') == 'off' and record.get('heating_suspended') == entity))
                        if not thermostat:
                            continue
                        try:
                            if native['state'] == 'heat' and power(self.report(sensor), source=sensor)[0] <= 0:
                                continue
                        except (ValueError, KeyError, TypeError):
                            continue
                previous = coordination.get('caps', {}).get(identity)
                maximum = 0.0
                if owner == 'ev':
                    try:
                        entity, low = self.flexible_ev(options, record)
                        maximum = ev_current_ceiling(max(0.0, bound - deficit),
                            self.report(entity)['attributes'], low) * EV_WATTS_PER_AMP
                    except (ControlObservationError, ValueError, KeyError, TypeError):
                        continue
                if previous is not None:
                    maximum = min(maximum, previous['maximum_w'])
                caps[identity] = dict(device=identity, owner=owner, maximum_w=maximum,
                    withdrawal='heating', resume_controls=commanded_controls(self.capacity, identity),
                    requested_w=max(bound, (previous or {}).get('requested_w', 0)),
                    heating={owner_request:request['parameters']['revision']},
                    thermostat=bool(thermostat))
                reductions.append(dict(request_id=uuid4().hex, device=owner, operation='curtail',
                    configuration_revision=request['configuration_revision'],
                    policy_revision=request['policy_revision'], headroom_reserved=False,
                    parameters={'models': value['parameters'].get('models', [])}))
                deficit -= bound - maximum
            if caps:
                await self.capacity_update('caps', caps)
        for value in reductions:
            if not self.heating_request_current(request):
                break
            try:
                await physical.perform(value)
            except (AdmissionPending, GatewayConflict, ControlObservationError, ValueError, KeyError, TypeError):
                continue
        return battery_needed and self.heating_request_current(request)

    def capacity_condition(self, value, effective):
        """A still-active positive cap is reduced service, including when the plan now fits it."""
        cap = next((c for c in self.capacity.get('coordination', {}).get('caps', {}).values()
                    if c['owner'] == value['device']), None)
        if cap is not None and cap.get('heating'):
            return None
        if cap is not None and value['device'] == 'ev':
            entity, low, _ = self.service.physical.ev_mapping(self.options())
            current = ev_current_ceiling(cap['maximum_w'], self.report(entity)['attributes'], low)
            if current > 0:
                return {'status': 'capacity_limited', 'cause': 'house_import_capacity',
                        'maximum_current_a': int(current)}
        if effective != value or cap is not None:
            return {'status': 'temporarily_absent', 'cause': 'house_import_capacity'}
        return None

    def capacity_resume_effect(self, cap):
        row = self.capacity['devices'].get(cap['device'],{})
        values = [*row.get('effects',{}).values(),*([row['settled']] if row.get('settled') else [])]
        if not values:
            return None
        latest = max(values,key=lambda item:item['order'])
        return {**latest,'controls':cap['resume_controls'],'after_w':cap['requested_w'],
                'possible_w':cap['requested_w']}

    async def capacity_recoveries(self):
        """Positive capacity evidence independent of an executable plan slot.

        Retain the cap until a current owner intention is admitted. Returning
        this evidence neither sends a setting nor claims delivered service.
        """
        await self.refresh_capacity()
        recovered = []
        for identity,cap in self.capacity.get('coordination',{}).get('caps',{}).items():
            owner = cap['owner']
            if cap.get('heating') or device_mode(self.options(),owner) != 'controlling':
                continue
            effect = self.capacity_resume_effect(cap)
            if effect is None:
                continue
            try:
                self.admit_import(effect)
            except (ControlDeadlineError,ValueError,KeyError,TypeError):
                continue
            recovered.append(owner.removeprefix('device:') if owner.startswith('device:') else '$'+owner)
        return sorted(set(recovered))

    def control_notices(self, models):
        options = self.options()
        mappings = options.get('device_control_mappings', {})
        targets = {'device:'+key: set(actuator_targets(mapping)) for key, mapping in mappings.items()}
        _, pool = pool_control_mapping(options, models)
        targets['pool'] = set(actuator_targets(pool))
        targets['ev'] = {options.get('ev_charge_switch_entity')} | {
            m.get('control_entity_id') for m in mappings.values() if m.get('control_type') == 'variable_power'}
        targets['battery'] = {options.get('battery_'+key+'_entity') for key in ('mode','charge_limit','discharge_limit')}
        notices = {}
        for owner, entities in targets.items():
            entities.discard(None)
            if device_mode(options, owner) != 'control_verification':
                continue
            values = []
            for entity in sorted(entities):
                report = self.report(entity) or {}
                state, attrs = report.get('state'), report.get('attributes', {})
                if state in (None, 'unknown', 'unavailable'):
                    continue
                if entity.startswith('climate.') and attrs.get('temperature') is not None:
                    values.append(f"{entity} is {state} with target {attrs['temperature']} °C")
                else:
                    values.append(f"{entity} is {state} {attrs.get('unit_of_measurement', '')}".rstrip())
            if values:
                notices[owner] = ['Current settings · ' + ', '.join(values) + '.']
        previous = {}
        for row in self.capacity['devices'].values():
            effects = [item for item in row['effects'].values() if item['dispatch_through'] is not None]
            for effect in [*effects, row.get('settled')]:
                if not effect:
                    continue
                owner = effect.get('owner', effect['logical'])
                current = targets.get(owner)
                if not current:
                    continue
                for entity in effect['controls'].keys() - current:
                    state = (self.report(entity) or {}).get('state')
                    if state in ('on', 'heat') or state in CHARGING_MODES or state == 'Command Discharging (PV First)':
                        previous.setdefault(owner, set()).add(entity)
        for owner, entities in previous.items():
            notices.setdefault(owner, []).append('Previous control entities still active · ' + ', '.join(sorted(entities)) + '.')
        battery_fault = self.service.battery.obligation['fault']
        if battery_fault:
            notices.setdefault('battery', []).append(battery_fault)
        return {owner:' '.join(details) for owner, details in notices.items()}

    async def coordinate_battery(self, effect, proposal, route_id, authorize):
        """Withdraw dependent demand through current owners before battery support.

        No lock is held across perform(): that owner takes the same physical
        lock. A new/current intention is required after a policy change; debt
        and caps retain physical identity across that change.
        """
        from copy import deepcopy
        from uuid import uuid4
        battery = self.service.battery
        installed = battery.installation[3]
        controls = battery.target_controls(effect.request)
        mode, limit_entity = installed['battery_mode_entity'],installed['battery_discharge_limit_entity']
        attributes = self.report(limit_entity)['attributes']
        states = [dict(effect.request.target), *(dict(step.after) for step in proposal.steps)]
        route_support = min(battery_discharge_ceiling(state[mode], state[limit_entity]) for state in states)
        async with self.service.physical.lock:
            authorize()
            await self.refresh_capacity()
            self.install_capacity(await battery.persist_target(effect.request, authorize,
                route_id=route_id, route_support_w=route_support))
            authorize()
            current_ceiling = battery_discharge_ceiling(self.report(mode)['state'],
                native_watts(self.report(limit_entity)['state'],attributes))
            if route_support >= current_ceiling:
                return
            descriptor = self.describe_import('battery',limit_entity,controls[limit_entity])
            descriptor = {**descriptor,'controls':controls}
            demand = self.import_required(effect=descriptor)
            deficit = max(0.0,demand-self.discharge_credit(descriptor,demand)-self.house_import_limit())
            if deficit <= 0:
                return
            coordination = self.capacity['coordination']
            candidates = []
            for identity,row in self.capacity['devices'].items():
                values = [*row['effects'].values(), *([row['settled']] if row.get('settled') else [])]
                if not values:
                    continue
                latest = max(values,key=lambda item:item['order'])
                owner = latest.get('owner',latest['logical'])
                if latest['logical'] == 'battery' or device_mode(self.options(),owner) != 'controlling':
                    continue
                # Dependency is current physical viability, not only the
                # credit needed at original admission. Base demand may have
                # risen since this owner was admitted without borrowing.
                kind = self.options().get('device_control_mappings',{}).get(owner.removeprefix('device:'),{}).get('control_type')
                if owner not in ('ev','pool') and kind not in ('switch_schedule','permit_inhibit'):
                    continue
                if not self.service.physical.capacity_reduction_available(owner,coordination['intents'].get(owner)):
                    continue
                candidates.append((latest['order'],identity,owner,self.reserved_power_bound(row),latest['controls']))
            caps, owners = {}, []
            for _,identity,owner,bound,native_controls in sorted(candidates,reverse=True):
                if deficit <= 0:
                    break
                if bound <= 0:
                    continue
                maximum = max(0.0,bound-deficit)
                if owner == 'ev':
                    mapping = next((m for m in self.options().get('device_control_mappings',{}).values()
                        if m.get('control_type') == 'variable_power' and m.get('control_entity_id') in native_controls),None)
                    if mapping is None:
                        continue
                    attributes = self.report(mapping['control_entity_id'])['attributes']
                    maximum = ev_current_ceiling(maximum,attributes,mapping.get('minimum_value',attributes['min']))*EV_WATTS_PER_AMP
                else:
                    maximum = 0.0
                previous = coordination['caps'].get(identity)
                resume = previous if previous and previous['requested_w'] >= bound else {
                    'resume_controls':native_controls,'requested_w':bound}
                caps[identity] = {'device':identity,'owner':owner,'maximum_w':maximum,'withdrawal':route_id,
                                  'resume_controls':resume['resume_controls'],'requested_w':resume['requested_w']}
                owners.append(owner)
                deficit -= bound-maximum
            await self.capacity_update('caps',caps)
            intents = deepcopy(self.capacity['coordination']['intents'])
        for owner in owners:
            authorize()
            value = intents.get(owner)
            if value is None:
                continue
            intent = DeviceIntent.parse(value)
            try:
                self.service.authorize_device(intent)
            except AdmissionPending:
                continue  # The current scheduler will provide this owner.
            value.update(request_id=uuid4().hex,headroom_reserved=True)
            await self.service.physical.perform(value)
        async with self.service.physical.lock:
            authorize()
            await self.refresh_capacity()
            demand = self.import_required(effect=descriptor)
            if demand-self.discharge_credit(descriptor,demand) > self.house_import_limit():
                raise AdmissionPending('Waiting for dependent load reductions before withdrawing battery support')

    def native_load_bounds(self):
        """Causal excluded-load evidence, keyed by the reviewed meter partition."""
        bounds = {}
        def include(effect, bound):
            if effect and effect['logical'] != 'battery' and effect['sensor']:
                bounds[effect['sensor']] = max(bounds.get(effect['sensor'],0.0), bound)
        for row in self.capacity['devices'].values():
            issued = {**row, 'effects':{key:effect for key,effect in row['effects'].items()
                      if effect['dispatch_through'] is not None}}
            bound = self.reserved_power_bound(issued)
            for effect in [*issued['effects'].values(), *([row['settled']] if row.get('settled') else [])]:
                include(effect, bound)
        options = self.options()
        switch = options.get('ev_charge_switch_entity')
        if switch and device_mode(options,'ev') == 'controlling':
            effect = self.describe_import('ev',switch,self.report(switch)['state'])
            include(effect,effect['before_w'])
        for owner, record in self.service.physical.ownership.records.items():
            if owner in ('battery','ev') or device_mode(options,owner) != 'controlling':
                continue
            for entity in record['originals']:
                effect = self.describe_import(owner, entity, (self.report(entity) or {}).get('state'))
                if effect:
                    include(effect,self.observed_device_bound(effect))
        return bounds

    def house_import_limit(self):
        if self.service.import_limit_w is None:
            raise AdmissionPending('Waiting for the current website house import limit')
        return import_limit(self.service.import_limit_w)

    def house_power(self):
        options = self.options()
        if house_meter_errors(options):
            raise HouseMeterError(options)
        entity = options['house_consumption_power_entity']
        return power(self.report(entity), source=entity)[0]

    def import_required(self, *, effect=None, exclude_device=None, exclude_devices=(), reserved=(), house_w=None):
        options = self.options()
        house = self.house_power() if house_w is None else house_w
        bounds, meters, batteries = {}, {}, set()
        attributed_meters = set()
        def include(descriptor, bound):
            device = descriptor['device']
            bounds[device] = max(bounds.get(device, 0.0), bound)
            meters.setdefault(device, set()).update([descriptor['sensor']] if descriptor['sensor'] else [])
            if descriptor.get('draw_power_w') is not None:
                attributed_meters.add(descriptor['sensor'])
            if descriptor['logical'] == 'battery':
                batteries.add(device)
        switch = options.get('ev_charge_switch_entity')
        if switch and device_mode(options, 'ev') == 'controlling':
            # Read/validate available native settings independently of desired
            # ownership values; outstanding effects have their own journal.
            descriptor = self.describe_import('ev', switch, self.report(switch)['state'])
            include(descriptor, descriptor['before_w'])
        # Uncontrolled appliances are already in the measured house demand.
        # Only controlling devices add a native peak envelope; journalled
        # commands below retain their reservations even during a mode exit.
        mappings = [(key, mapping, 'device:'+key) for key, mapping in options.get('device_control_mappings', {}).items()
                    if device_mode(options, 'device:'+key) == 'controlling']
        for owner, record in self.service.physical.ownership.records.items():
            if device_mode(options, owner) != 'controlling':
                continue
            mappings.extend((key, mapping, owner) for key, mapping in record.get('options', {}).get('device_control_mappings', {}).items()
                if set(actuator_targets(mapping)) & set(record['originals']))
        for key, mapping, owner in mappings:
            if self.mapped_power_bound(mapping) is None:
                continue
            targets = actuator_targets(mapping)
            if not targets:
                continue
            entity = targets[0]
            descriptor = self.describe_import(owner, entity, (self.report(entity) or {}).get('state'))
            if descriptor is not None:
                include(descriptor,self.observed_device_bound(descriptor))
        battery = self.service.battery
        if battery.installation is not None and exclude_device != self.battery_capacity_key(battery.installation[3]):
            _, catalog, conversion, installed = battery.installation
            mode = installed['battery_mode_entity']
            sensor = installed['battery_power_measurement_entity']
            held = (device_mode(options, 'battery') == 'control_verification' or any(
                options.get(key) != installed[key] for key in (
                    'battery_mode_entity', 'battery_charge_limit_entity', 'battery_discharge_limit_entity')))
            try:
                descriptor = self.describe_import('battery', mode, self.report(mode)['state'])
                bound = descriptor['before_w']
            except (ValueError, KeyError, TypeError):
                if not held:
                    raise
                # A former or unowned control surface may become unavailable.
                # Its commissioned maximum remains in the house budget.
                descriptor = {'device':self.battery_capacity_key(installed), 'logical':'battery', 'sensor':sensor}
                bound = conversion.grid_charge.input(catalog.charge_max_w)
            try:
                measured, _ = power(self.report(sensor), source=sensor, signed=True)
            except (ValueError, KeyError, TypeError):
                if not held:
                    raise
                measured = 0.0
            include(descriptor, max(bound, conversion.grid_charge.input(max(0.0, measured))))
        for row in self.capacity['devices'].values():
            bound = self.reserved_power_bound(row)
            for descriptor in [*row['effects'].values(), *([row['settled']] if row.get('settled') else [])]:
                include(descriptor, bound)
        for reservation in reserved:
            include(reservation, reservation['possible_w'])
        if effect is not None:
            include(effect, max(effect['before_w'],effect['after_w'],effect['possible_w']))
        partitions, used = [], set()
        for device, bound in bounds.items():
            if device == exclude_device or device in exclude_devices:
                continue
            measured = 0.0
            sensors = meters[device]
            # Shared or replaced meters cannot establish two independent load
            # partitions. Retain the whole rated bound without subtracting it.
            if device not in batteries and len(sensors) == 1:
                sensor = next(iter(sensors))
                # A learned draw is incremental. Outlets already in gross house
                # demand cannot offset the possible draw of the heater.
                if sensor not in used and sensor not in attributed_meters:
                    try:
                        measured, _ = power(self.report(sensor), source=sensor)
                    except ValueError:
                        # With no partition measurement, subtract nothing from
                        # the house reading and retain the full reviewed bound.
                        pass
                    used.add(sensor)
            partitions.append((measured, bound))
        return required_import(house, partitions)

    def external_ready(self, device, *, entity=None, value=None, priority=False, require_battery_release=True):
        # The app has already reserved unknown pending demand. HA independently
        # checks the current battery response at the final native boundary.
        battery = self.service.battery
        try:
            if entity is not None:
                self.admit_import(self.describe_import(device, entity, value))
            # Manual demand shares the durable native import envelope with the
            # battery, including all in-flight charge. When that envelope fits,
            # forcing charge to stop would spend another load's flexibility.
            if priority:
                self.heating_waiting.discard(device)
                return True
            return (not require_battery_release or battery.installation is None
                    or device_mode(self.options(), 'battery') == 'control_verification'
                    or battery_released(battery.installation[3], self.report))
        except (AdmissionPending, ControlDeadlineError, GridCapacityConfigurationError, ControlObservationError):
            raise
        except (ValueError, KeyError, TypeError):
            return False

    async def limit_ev_current(self, entity, switch, requested, minimum):
        """Use a feasible scheduled current and retain the original target for recovery."""
        native = self.report(entity)
        effect = self.describe_import('ev', switch, 'on')
        ev = self.measured_control_power(effect['sensor'])
        house = self.measured_control_power(self.options()['house_consumption_power_entity'])
        grid_entity = self.options().get('grid_power_entity')
        grid = self.measured_control_power(grid_entity, signed=True) if grid_entity else None
        if self.report(switch)['state'] == 'on':
            # The car's part of the import budget, shared by live priority
            # with the battery and eligible heaters. Sizing is absolute, so it
            # stays stable both before and after the charger meter catches
            # up. Keep the plan target for recovery.
            decision = self.priority_pass(ev=dict(entity=entity, switch=switch, requested=requested, minimum=minimum))
            available = ev_current_ceiling(decision['granted']['ev'], native['attributes'], minimum)
            return min(requested, available)
        identity = effect['device']
        if identity in self.capacity.get('coordination', {}).get('caps', {}):
            return requested

        # A current battery writer can release its charging load before the
        # car starts. Other native and unsettled loads remain in the budget.
        exclude = None
        battery = self.service.battery
        if self.active_battery_target() is not None and battery.installation is not None:
            installed = battery.installation[3]
            if self.report(installed['battery_mode_entity'])['state'] in CHARGING_MODES:
                exclude = self.battery_capacity_key(installed)
        limit = self.house_import_limit()
        reserved = self.heating_effects()
        available = requested
        while available >= minimum and available > 0:
            watts = available * EV_WATTS_PER_AMP
            desired = {**effect, 'after_w':watts, 'possible_w':watts,
                       'controls':{**effect['controls'], entity:available}}
            demand = self.import_required(effect=desired, exclude_device=exclude, reserved=reserved.values())
            deficit = demand - self.discharge_credit(desired, demand) - limit
            if deficit <= 0:
                if available < requested:
                    await self.capacity_update('caps', {identity: {
                        'device':identity, 'owner':'ev', 'maximum_w':watts,
                        'withdrawal':'house_import_capacity',
                        'resume_controls':{**effect['controls'], entity:requested},
                        'requested_w':requested * EV_WATTS_PER_AMP,
                    }})
                return available
            reduced = ev_current_ceiling(watts - deficit, native['attributes'], minimum)
            if reduced >= available:
                break
            available = reduced
        # The normal admission check explains why even the requested start must wait.
        return requested

    def ev_spare_current(self, entity, switch, scheduled, maximum, house_budget, *, hold_increase=False):
        """Spend only observed spare power after the scheduled car has started."""
        try:
            if self.report(switch)['state'] != 'on':
                return scheduled
            effect = self.describe_import('ev', switch, 'on')
            identity, sensor = effect['device'], effect['sensor']
            if hold_increase:
                native = self.report(entity)
                observed = float(canonical_metadata(native['state'], native['attributes']))
                issued = commanded_controls(self.capacity, identity, issued_only=True)
                maximum = min(maximum, max(scheduled, finite(issued.get(entity, observed))))
            if identity in self.capacity.get('coordination', {}).get('caps', {}):
                return scheduled
            ev = self.measured_control_power(sensor)
            grid_entity = self.options().get('grid_power_entity')
            if ev <= 0 or not grid_entity:
                return scheduled
            grid = self.measured_control_power(grid_entity, signed=True)
            house = max(0.0, self.house_power() - ev)
            limit = self.house_import_limit()
            reserved = [*self.heating_effects().values(), *self.current_import_claims(exclude_owner='ev')]
            # A boost keeps what live priority grants the car plus whatever
            # the measured budget leaves unused. It is reclaimed first only
            # while the run stays on track without it.
            decision = self.priority_pass(ev=dict(entity=entity, switch=switch, requested=scheduled,
                minimum=scheduled, maximum=maximum))
            ceiling = min(maximum * EV_WATTS_PER_AMP, decision['granted']['ev'] + decision['left_w'],
                          scheduled * EV_WATTS_PER_AMP + max(0.0, house_budget - house))
            if ceiling <= scheduled * EV_WATTS_PER_AMP:
                return scheduled
            available = ev_current_ceiling(ceiling, self.report(entity)['attributes'], scheduled)
            if available <= scheduled:
                return scheduled
            native = self.report(entity)
            if available <= float(canonical_metadata(native['state'], native['attributes'])):
                # The old charging envelope must remain reserved for other
                # writers, but cannot force a small reduction down to nominal.
                return available
            watts = available * EV_WATTS_PER_AMP
            desired = {**effect, 'after_w':watts, 'possible_w':watts,
                       'controls':{**effect['controls'], entity:available}}
            if self.import_required(effect=desired, reserved=reserved) <= limit:
                return available
        except ControlObservationError:
            raise
        except (KeyError, TypeError, ValueError):
            # Optional current needs complete live evidence. The normal
            # scheduled command still goes through its own admission.
            pass
        return scheduled

    def attach(self):
        @callback
        def matches(data):
            entity = data.get('entity_id')
            return not self.closed and (entity in self.entities or entity in self.power_entities)
        @callback
        def observe(event):
            if self.closed:
                return
            entity = event.data['entity_id']
            state = event.data.get('new_state')
            self.observed_serial += 1
            if state and state.attributes.get('entity_id') and er.async_get(self.hass).async_get(entity):
                discovered = filter_sources({entity}, self.hass.states.get,
                    lambda key: item.platform if (item := er.async_get(self.hass).async_get(key)) else None)
                if discovered - self.entities:
                    metadata_changed(event)
            kind = 'state_report' if event.event_type == EVENT_STATE_REPORTED else 'state_change'
            row = self.report(entity, event.data.get('new_state'), kind, compact=True) if entity in self.power_entities else None
            raised_peak = observe_power_peak(self.power_peaks, row)
            if self.observe_setting_peaks(row):
                # Per-setting peaks are not observation payload; persist them
                # beside it. The journal's max-merge makes write order irrelevant.
                self.entry.async_create_background_task(self.hass,
                    self.service.stream.call('save_record', 'power_peaks', dict(self.power_peaks)),
                    name='shs_power_setting_peaks')
            if not self.execution_source(entity) and not raised_peak:
                self.intake_counts['replaceable'] += 1
                self.live_revision += 1
                self.live_changes[entity] = self.live_revision
                return
            physical = self.service.physical
            physical.observation_changed.set()
            try:
                row = row or self.report(entity, event.data.get('new_state'), kind, compact=True)
                self.intake_counts['ordered'] += 1
                self.intake_counts['bytes'] += len(json.dumps(row))
                receipt = self.service.stream.capture('observation', row)
                def committed(future):
                    if (not self.closed and
                            (raised_peak or entity in self.capacity_entities
                             or entity == self.options().get('house_consumption_power_entity'))
                            and not future.cancelled() and future.exception() is None):
                        self.wake_heating_capacity()
                    if (not self.closed and entity in self.capacity_entities and not self.capacity_refresh_pending
                            and not future.cancelled() and future.exception() is None):
                        self.capacity_refresh_pending = True
                        async def refresh():
                            try:
                                if not self.closed:
                                    await self.refresh_capacity()
                            finally:
                                self.capacity_refresh_pending = False
                        self.entry.async_create_background_task(self.hass, refresh(), name='shs_import_capacity')
                receipt.add_done_callback(committed)
                self.fact_versions[entity] = self.fact_versions.get(entity,0)+1
                if entity in self.pool_metadata:
                    self.pool_metadata[entity] = temperature_metadata(state)
                    if temperature_available(state):
                        self.pool_unavailable.discard(entity)
                    else:
                        self.pool_unavailable.add(entity)
            except Exception:
                self.service.revoke()
                # The stream logs its initiating fault once with its traceback.
                # Later source reports cannot recover it and must not flood HA.
                if self.service.stream.failure is None:
                    _LOGGER.exception('SHS gateway observation persistence unavailable')
        @callback
        def metadata_changed(event):
            if self.closed:
                return
            self.collect_power_history()
            if self.metadata_unchanged():
                return
            self.service.invalidate_context()
            self.entry.async_create_background_task(self.hass, self.refresh_configuration(), name='shs_gateway_configuration')
        @callback
        def group_changed(change, entry):
            if entry.domain == 'group':
                self.collect_power_history()

        self.entry.async_on_unload(async_dispatcher_connect(
            self.hass, SIGNAL_CONFIG_ENTRY_CHANGED, group_changed))
        for event in (EVENT_STATE_CHANGED, EVENT_STATE_REPORTED):
            self.entry.async_on_unload(self.hass.bus.async_listen(event, observe, event_filter=matches))
        for event in (EVENT_CORE_CONFIG_UPDATE, er.EVENT_ENTITY_REGISTRY_UPDATED,
                      dr.EVENT_DEVICE_REGISTRY_UPDATED, ar.EVENT_AREA_REGISTRY_UPDATED):
            self.entry.async_on_unload(self.hass.bus.async_listen(event, metadata_changed))


async def open_gateway(hass, entry):
    path = Path(hass.config.path('.storage', f'shs_energy.gateway.{entry.entry_id}.sqlite'))
    journal = GatewayJournal(path)
    def read_identity():
        with closing(journal.connect(readonly=True)) as db:
            row = db.execute('SELECT seed,activation FROM authority').fetchone()
            return json.loads(row[0])['identity'], row[1] is not None
    identity, activated = await hass.async_add_executor_job(read_identity)
    def core_digest():
        from hashlib import sha256
        root = Path(__file__).parent/'shs_core'
        files = {str(p.relative_to(root)):sha256(p.read_bytes()).hexdigest()
                 for p in sorted(root.rglob('*')) if p.is_file() and '__pycache__' not in p.parts and p.suffix != '.pyc'}
        return sha256(json.dumps(files,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()
    if not activated and identity['pair']['core_sha256'] != await hass.async_add_executor_job(core_digest):
        raise GatewayConflict('The installed companion core differs from the paired app')
    if identity['entry_id'] != entry.entry_id or (not activated and identity['pair']['integration_version'] != INTEGRATION_VERSION):
        raise GatewayConflict('The installed companion differs from the seeded migration release')
    await hass.async_add_executor_job(journal.open)
    stream = GatewayStream(journal, hass.async_add_executor_job)
    stream.start()
    source = HomeAssistantSource(hass, entry)
    service = GatewayService(stream, identity, source)
    service.heating = HeatingOverrides(GatewayRecord(stream, 'heating_overrides'), service.heating_changed)
    source.service = service
    source.configuration = ExecutionConfiguration(GatewayRecord(stream,'execution_configuration'),
        source.invalidate, source.capture_configuration)
    service.installation_pending = source.configuration.lock.locked

    async def send(domain, action, data):
        await hass.services.async_call(domain, action, data, blocking=True)
    commands = GatewayCommands(stream, source)
    native = NativeExecutor(CommandTransport(commands, commands.execute), hass.states.get,
                            lambda:hass.config.units.temperature_unit, send, capacity=source)
    registry = er.async_get(hass)
    inputs = ControllerInputs(hass.states.get, lambda:hass.config.units.temperature_unit,
        lambda entity:item.platform if (item:=registry.async_get(entity)) else None)
    service.physical = DeviceGateway(inputs, source.options, GatewayRecord(stream,'ownership'), native,
        authorize=service.authorize_device, before_external=source.external_ready,
        operations=GatewayOperations(stream, service.session), capacity=source, heating=service.heating,
        configuration_pending=service.admission_pending, control_notices=source.control_notices)
    now = lambda:int(datetime.now(timezone.utc).timestamp()*1000)
    service.battery = BatteryGateway(stream,service.physical,source.options,source.report,now,
        authorize=service.authorize,session=service.session,context=service.context, coordinate=source.coordinate_battery,
        configuration_pending=service.admission_pending)
    try:
        await source.configuration.load(dict(entry.options))
        await service.heating.load(source.options())
        admission = await stream.call('load_record','source_admission')
        if admission is not None:
            source.install_bindings(admission)
        await service.physical.load()
        await source.refresh_capacity()
        source.run_progress = await stream.call('load_record', 'run_progress') or {}
        retained_limit = await stream.call('load_record', 'house_import_limit')
        if retained_limit is not None:
            service.import_limit_w = import_limit(retained_limit['import_limit_w'])
        await service.battery.open()
        source.attach()
        await source.capture_configuration()
        entry.async_create_background_task(hass, source.learn_device_draws(), name='shs_device_draw')
    except BaseException:
        service.heating.close()
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
            admit(msg['body'].get('contract'), offer(INTEGRATION_VERSION))
            peer = sockets[connection] = AppConnection(service)
            peer.projection_assembly = ProjectionAssembly()
            @callback
            def disconnected():
                sockets.pop(connection,None)
                current = service.connection is peer
                peer.disconnected()
                if current and service.source.projection:
                    service.source.projection.disconnected()
                hass.async_create_task(peer.close())
            connection.subscriptions['shs_energy_gateway'] = disconnected
        if peer is None:
            raise GatewayConflict('Connect the paired SHS app first')
        request = {key:msg[key] for key in ('id','operation','body')}
        if request['operation'] == 'connect':
            request['body'] = {key:value for key,value in request['body'].items() if key != 'contract'}
        elif request['operation'] == 'projection_chunk':
            if peer.closed or peer.service.connection is not peer or not peer.service.active:
                raise GatewayConflict('Projection belongs to an inactive socket')
            value = peer.projection_assembly.receive(request['body'])
            if value is None:
                connection.send_result(msg['id'], {'received':request['body']['index']})
                return
            request.update(operation='projection',body={'value':value})
        result = await peer.request(request)
        if request['operation'] == 'connect':
            result['result']['contract'] = offer(INTEGRATION_VERSION)
        connection.send_result(msg['id'],result['result'])
    except ConfigurationChanged:
        raise
    except HouseholdReadError as error:
        if msg['operation'] != 'source':
            raise
        connection.send_error(msg['id'], 'shs_source_unavailable', str(error))
    except (ValueError, KeyError, TypeError, RuntimeError) as error:
        stale = peer is None or peer.closed or peer.service.connection is not peer
        connection.send_error(msg['id'],'shs_gateway_stale' if stale else
            'shs_gateway_configuration_pending' if isinstance(error, ConfigurationPending) else
            'shs_gateway_admission' if isinstance(error, AdmissionPending) else 'shs_gateway_rejected',str(error))


def register_gateway(hass):
    websocket_api.async_register_command(hass,websocket_gateway)
