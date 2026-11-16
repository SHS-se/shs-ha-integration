"""App configuration actions and catalogue, shared by the browser and HA controls."""
from datetime import datetime, timezone
from contextlib import nullcontext

from shs_core import const as c
from shs_core.configuration_schema import (
    ConfigurationFieldErrors, prepare_options, save_device, resolve_configuration, initialise_device_inclusion,
)
from shs_core.configuration_view import execution_device_views
from shs_core.device_controls import CONTROL_TYPES, device_planning_path
from shs_core.hot_water import hot_water_setup_errors
from shs_core.device_setup import DEVICE_SETUPS, LOAD_TYPES, read_device_setups
from shs_core.discovery import (CatalogState, DiscoveryCatalog, discover_configuration,
                                _energy_dashboard_inventory, dashboard_meters, meter_room)
from shs_core.operating_modes import admission_identity, execution_mode_options, reconcile_admissions
from shs_core.grid_capacity import house_meter_errors
from shs_core.presentation import system_fields
from .configuration_view import configuration_payload


class ConfigurationEditor:
    def __init__(self, engine):
        self.engine = engine
        self.catalog = None
        self.context = None

    async def refresh_catalog(self):
        value = await self.engine.gateway.call('source',dict(operation='catalog',body={}))
        self.context = value['context']
        self.meter_areas = value['meter_areas']
        home = self.context['home']
        self.catalog = DiscoveryCatalog({key:CatalogState(row['entity_id'],row['state'],row['attributes'],
            datetime.fromisoformat(row['last_updated'])) for key,row in value['states'].items()},
            value['preferences'],home['latitude'],home['longitude'])

    async def inventory(self):
        await self.refresh_catalog()
        meters = dashboard_meters(self.catalog.preferences)
        overrides = self.engine.configuration.options().get(c.OPT_DEVICE_CATEGORY_OVERRIDES, {})
        devices = _energy_dashboard_inventory(self.catalog, self.catalog.preferences, overrides)
        for device in devices:
            device['meter_room'] = meter_room(meters[device['key']], self.meter_areas,
                                             self.context['area_names'])
        return devices

    async def import_device_setups(self):
        """Adopt the selected backend's choices without changing live admission."""
        configuration = self.engine.configuration
        if DEVICE_SETUPS in configuration.options():
            self.publish_device_settings()
            return
        choices = read_device_setups(await self.engine.household.client.read_device_setups())
        existing = configuration.options()
        if DEVICE_SETUPS not in existing:
            await configuration.admit(existing, {**existing, DEVICE_SETUPS: choices})
            self.publish_device_settings()

    async def _publish_and_replan(self, environment, household):
        await household.async_refresh_device_configuration()
        if household.resolved_options()[c.OPT_PLANNING_MODE] == c.PLANNING_MODE_LIVE:
            household._plan_configuration_changed = True
            self.engine.spawn(self.engine.cloud_job(environment, lambda: self.engine.request_plan(household), stage='upload'),
                              'configuration_replan_' + environment)

    def publish_device_settings(self, *, replan=False):
        for environment, household in self.engine.households.items():
            operation = (lambda e=environment, h=household: self._publish_and_replan(e, h)) if replan else household.async_refresh_device_configuration
            self.engine.spawn(self.engine.cloud_job(environment, operation, stage='website_choices'),
                              'device_settings_sync_' + environment)

    def resolved_options(self):
        return resolve_configuration(self.engine.configuration.options(),self.catalog.latitude,self.catalog.longitude)

    def read(self, entity):
        state = self.catalog.states.get(entity)
        return dict(state=state.state,attributes=state.attributes) if state else None

    def entities(self):
        result = []
        for key,state in sorted(self.catalog.states.items()):
            area = self.context['entity_areas'].get(key)
            a = state.attributes
            result.append(dict(entity_id=key,name=a.get('friendly_name') or key,domain=key.split('.')[0],
                state=state.state[:120],unit=a.get('unit_of_measurement'),last_updated=state.last_updated.isoformat(),
                device_class=a.get('device_class'),state_class=a.get('state_class'),minimum=a.get('min'),maximum=a.get('max'),options=a.get('options',[]),
                area_id=area,area_name=self.context['area_names'].get(area)))
        return result

    async def devices(self, choices=None, *, include_suggestions=True, context=None):
        if self.catalog is None:
            await self.refresh_catalog()
        h = self.engine.household
        if choices is None:
            choices = await h.async_cached_planning_configuration()
        before = self.engine.configuration.options()
        context = self.context if context is None else context
        value = execution_device_views(self.catalog,context,before,choices,h,include_suggestions=include_suggestions)
        initialised = initialise_device_inclusion(before,value)
        if initialised != before:
            await self.engine.configuration.admit(before,initialised)
            value = execution_device_views(self.catalog,context,initialised,choices,h,include_suggestions=include_suggestions)
        return value

    async def view(self, *, refresh_roles=False):
        await self.refresh_catalog()
        result = await configuration_payload(self,refresh_roles=refresh_roles)
        result.update(self.engine.configuration.status())
        result['backends'] = self.engine.backends.public_status(self.engine.households)
        result['locale'] = {key:self.context['home'][key] for key in ('language','timezone')}
        return result

    def prepare(self, existing, incoming):
        return prepare_options(existing,incoming,self.read,latitude=self.catalog.latitude,longitude=self.catalog.longitude)

    async def _commit(self, options, body, *, replan, reviewed=True):
        result = await self.engine.configuration.commit(body['expected_revision'],options,body['request_id'],reviewed=reviewed)
        h = self.engine.household
        # Canonical commit and gateway acknowledgement precede all policy work.
        # The ordered configuration receipt triggers subscription/plan handling.
        self.engine.wake_projection.set()
        if replan:
            h._plan_configuration_changed = True
            await self.engine.replan_all()
        return result

    async def _set_execution_mode(self, body):
        configuration = self.engine.configuration
        entered = configuration.revision
        verifying = body['mode'] == 'control_verification'
        async with (self.engine.gateway.prioritize_configuration() if verifying else nullcontext()):
            if not verifying:
                await self.refresh_catalog()
            devices = await self.devices()
            options = execution_mode_options(configuration.options(), devices, body['device_key'], body['mode'])
            device = next(d for d in devices if d['key'] == body['device_key'])
            owner = '$' + device['system'] if device.get('system') else device['key']
            await configuration.commit(body['expected_revision'], options, body['request_id'], reviewed=True,
                                       verification=(owner, entered) if verifying else None)
        self.engine.wake_projection.set()
        await self.engine.household.async_battery_inputs_refresh()
        await self.engine.evaluate_controller()
        return await self.view()

    async def action(self, operation, body):
        h = self.engine.household
        if operation == 'get':
            return await self.view(refresh_roles=body.get('refresh_roles',False))
        if operation == 'device_room':
            await self.engine.gateway.call('source', dict(operation=operation,
                body={key: body[key] for key in ('device_key', 'meters', 'area_id')}))
            self.publish_device_settings()
            return await self.view()
        if operation in ('pair_backend','select_backend'):
            await getattr(self.engine,operation)(body)
            result = await self.view()
            self.engine.request_restart()
            return result
        if operation == 'control':
            return await self._set_execution_mode(body)
        if operation == 'save_room_probe':
            return await self._save_room_probe(body)
        if operation == 'save_device':
            return await self._save_device(body)
        await self.refresh_catalog()
        if operation == 'discover':
            return discover_configuration(self.catalog,self.engine.configuration.options())
        if operation == 'replan':
            if self.resolved_options().get(c.OPT_PLANNING_MODE) == c.PLANNING_MODE_DISABLED:
                raise ValueError('Planning is turned off for this home')
            await self.engine.request_plan(h)
            if h.last_optimisation_error: raise ValueError(h.last_optimisation_error)
            return await self.view()
        existing = self.engine.configuration.options()
        if operation != 'save':
            raise ValueError('Unsupported configuration action')
        incoming = body['configuration']
        if c.OPT_DEVICE_CONTROL_MAPPINGS in incoming:
            raise ValueError('Device mappings must be saved from their own card')
        result = await self._commit(self.prepare(existing, incoming), body, replan=True)
        return {**result, 'saved': True, 'refreshing': False}

    async def _save_room_probe(self, body):
        await self.refresh_catalog()
        room, source = body['room_key'], body['temperature_entity_id']
        if room not in self.context['area_names']:
            raise ConfigurationFieldErrors({'temperature_entity_id': ['This room is no longer in Home Assistant.']})
        if source:
            state = self.catalog.states.get(source)
            if (not state or not source.startswith('sensor.') or self.context['entity_areas'].get(source) != room
                    or state.attributes.get('unit_of_measurement') != '°C'
                    or state.attributes.get('device_class') != 'temperature'
                    or state.attributes.get('state_class') != 'measurement'):
                raise ConfigurationFieldErrors({'temperature_entity_id': ['Choose a Celsius temperature sensor in this room with recorder statistics.']})

        def set_probe(existing):
            if any(mapping.get(c.ROOM_AREA_FIELD) == room for mapping in existing.get(c.OPT_DEVICE_CONTROL_MAPPINGS, {}).values()):
                raise ConfigurationFieldErrors({'temperature_entity_id': ['Edit this room probe through its device settings.']})
            rooms = dict(existing.get('rooms', {}))
            if source:
                rooms[room] = {'temperature_entity_id': source}
            else:
                rooms.pop(room, None)
            return {**existing, 'rooms': rooms}

        await self.engine.configuration.commit(body['expected_revision'], set_probe, body['request_id'],
            intent={'room_key': room, 'temperature_entity_id': source})
        self.engine.wake_projection.set()
        return await self.view()

    async def _save_device(self, body):
        configuration = self.engine.configuration
        intent = {key: body[key] for key in ('device_key', 'settings', 'mapping', 'configuration', 'room_key') if key in body}
        result = await configuration.commit(
            body['expected_revision'], lambda existing: self._prepare_device_save(existing, intent),
            body['request_id'], intent=intent,
        )
        if not result.get('replayed'):
            self.publish_device_settings(replan=True)
        response = await self.view()
        return {**response, 'saved': True, 'refreshing': False}

    async def _prepare_device_save(self, existing, intent):
        household = self.engine.household
        choices = await household.async_cached_planning_configuration()
        await self.refresh_catalog()
        catalog, context = self.catalog, self.context
        key = intent['device_key']
        device = next((row for row in choices['devices'] if row['key'] == key), None)
        incoming = intent.get('configuration', {})
        edits = intent.get('settings', {})
        if not isinstance(edits, dict) or not isinstance(incoming, dict):
            raise ValueError('Choose valid device settings')
        fields = {
            'category': (c.DEVICE_CATEGORIES, 'device category'),
            'control_type': (CONTROL_TYPES, 'control method'),
            'load_type': (LOAD_TYPES, 'load characteristic'),
        }
        if set(edits) - set(fields):
            raise ValueError('Choose valid device settings')
        errors = {field: [f'Choose a {label} from the list'] for field, (values, label) in fields.items()
                  if field in edits and edits[field] not in values}
        if errors:
            raise ConfigurationFieldErrors(errors)
        if device is None:
            if key not in ('$battery', '$ev', '$pool') or edits or intent.get('mapping'):
                raise ValueError('This device is no longer in the website inventory')
            allowed = {field['key'] for field in system_fields(key[1:])}
        else:
            if ('control_type' in edits or 'load_type' in edits) and DEVICE_SETUPS not in existing:
                raise ValueError('Wait for the app to retrieve your existing device settings')
            views = execution_device_views(catalog, context, existing, choices, household, include_suggestions=False)
            view = next((row for row in views if row['key'] == key), {})
            allowed = {field['key'] for field in view.get('system_fields', [])}
            if view.get('system') and view['system'] != device_planning_path({**device, **edits}):
                allowed = set()
        if set(incoming) - allowed:
            raise ValueError('These settings belong to another device')

        def read(entity):
            state = catalog.states.get(entity)
            return dict(state=state.state, attributes=state.attributes) if state else None

        options = prepare_options(existing, incoming, read, latitude=catalog.latitude, longitude=catalog.longitude)
        if device is None:
            return options

        old_mapping = resolve_configuration(existing).get(c.OPT_DEVICE_CONTROL_MAPPINGS, {}).get(key, {})
        method = device.get('control_type') or old_mapping.get('control_type')
        proposed = {**device, 'control_type': method, **edits}
        mapping = intent.get('mapping')
        planned = (device.get('planning_role') == 'controllable' and
                   key not in existing.get('excluded_device_readings', ()))
        changed_contract = (method != proposed['control_type']
                            or device_planning_path({**device, 'control_type': method}) != device_planning_path(proposed))
        if 'control_type' in edits and mapping is None:
            raise ConfigurationFieldErrors({'control_type': ['Complete the fields for the selected control method']})
        if planned and 'mapping' in intent and mapping is None:
            raise ConfigurationFieldErrors({'control_type': ['Keep a complete setup while this device is Planned']})
        errors = {}
        if planned and (mapping is not None or changed_contract):
            errors = house_meter_errors(resolve_configuration(options))
            if device_planning_path(proposed) is None:
                errors['control_type'] = ['Choose a control method supported for this category']
            elif device_planning_path(proposed) == 'ev' and not options.get('ev_charge_switch_entity'):
                errors['ev_charge_switch_entity'] = ['Choose the EV charging start/stop switch']
            elif device_planning_path(proposed) == 'boiler' and mapping is not None:
                hot_water_setup_errors(mapping, field_errors=errors)
        if 'category' in edits:
            options.setdefault(c.OPT_DEVICE_CATEGORY_OVERRIDES, {})[key] = edits['category']
        if 'mapping' in intent:
            try:
                options = save_device(options, key, mapping, proposed, read, entity_names=context['entity_names'],
                                      area_names=context['area_names'], entity_area_ids=context['entity_areas'])
            except ConfigurationFieldErrors as error:
                errors = {**error.field_errors, **errors}
            if errors:
                raise ConfigurationFieldErrors(errors)
            new_room = options.get(c.OPT_DEVICE_CONTROL_MAPPINGS, {}).get(key, {}).get(c.ROOM_AREA_FIELD)
            if (intent.get('room_key') is not None and mapping and mapping.get('temperature_entity_id')
                    and new_room != intent['room_key']):
                raise ConfigurationFieldErrors({'temperature_entity_id': ['The room assignment changed. Refresh and review the room probe.']})
        if errors:
            raise ConfigurationFieldErrors(errors)
        if DEVICE_SETUPS in options and (edits or 'mapping' in intent):
            rooms = existing.get('rooms', {})
            changed_rooms = {room for room, bindings in options.get('rooms', {}).items() if bindings != rooms.get(room)}
            mappings = options.get(c.OPT_DEVICE_CONTROL_MAPPINGS, {})
            for row in choices['devices']:
                device_key = row['key']
                if device_key != key and mappings.get(device_key, {}).get(c.ROOM_AREA_FIELD) not in changed_rooms:
                    continue
                setup = proposed if device_key == key else row
                previous = options[DEVICE_SETUPS].get(device_key, {})
                options[DEVICE_SETUPS][device_key] = {
                    'control_type': setup.get('control_type'),
                    'load_type': setup['load_type'],
                    'revision': max(previous.get('revision', 0) + 1, self.engine.configuration.revision + 1),
                }
        if changed_contract:
            old_groups = reconcile_admissions(existing, choices['devices'], choices['home'])['planning_admissions']
            changed = [proposed if row['key'] == key else row for row in choices['devices']]
            new_groups = reconcile_admissions(options, changed, choices['home'])['planning_admissions']
            for owner, mode in options.get('device_modes', {}).items():
                if mode == 'controlling' and admission_identity(owner, old_groups.get(owner)) != admission_identity(owner, new_groups.get(owner)):
                    options['device_modes'][owner] = 'control_verification'
        return options
