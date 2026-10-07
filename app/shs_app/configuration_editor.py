"""App configuration actions and catalogue, shared by the browser and HA controls."""
from datetime import datetime, timezone

from shs_core import const as c
from shs_core.configuration_schema import prepare_options, save_device, resolve_configuration, initialise_device_inclusion
from shs_core.configuration_view import execution_device_views
from shs_core.discovery import CatalogState, DiscoveryCatalog, discover_configuration, _energy_dashboard_inventory
from shs_core.operating_modes import execution_mode_options
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
        home = self.context['home']
        self.catalog = DiscoveryCatalog({key:CatalogState(row['entity_id'],row['state'],row['attributes'],
            datetime.fromisoformat(row['last_updated'])) for key,row in value['states'].items()},
            value['preferences'],home['latitude'],home['longitude'])

    async def inventory(self):
        await self.refresh_catalog()
        return _energy_dashboard_inventory(self.catalog,self.catalog.preferences)

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
                device_class=a.get('device_class'),minimum=a.get('min'),maximum=a.get('max'),options=a.get('options',[]),
                area_id=area,area_name=self.context['area_names'].get(area)))
        return result

    async def devices(self, choices=None, *, include_suggestions=True):
        if self.catalog is None:
            await self.refresh_catalog()
        h = self.engine.household
        if choices is None:
            choices = await h.async_cached_planning_configuration()
        before = self.engine.configuration.options()
        value = execution_device_views(self.catalog,self.context,before,choices,h,include_suggestions=include_suggestions)
        initialised = initialise_device_inclusion(before,value)
        if initialised != before:
            await self.engine.configuration.admit(before,initialised)
            value = execution_device_views(self.catalog,self.context,initialised,choices,h,include_suggestions=include_suggestions)
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

    async def _commit(self, options, body, *, replan):
        result = await self.engine.configuration.commit(body['expected_revision'],options,body['request_id'],reviewed=True)
        h = self.engine.household
        # Canonical commit and gateway acknowledgement precede all policy work.
        # The ordered configuration receipt triggers subscription/plan handling.
        self.engine.wake_projection.set()
        if replan:
            h._plan_configuration_changed = True
            await self.engine.replan_all()
        return result

    async def action(self, operation, body):
        h = self.engine.household
        if operation == 'get':
            return await self.view(refresh_roles=body.get('refresh_roles',False))
        if operation in ('pair_backend','select_backend'):
            await getattr(self.engine,operation)(body)
            result = await self.view()
            self.engine.request_restart()
            return result
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
        if operation == 'control':
            options = execution_mode_options(existing,await self.devices(),body['device_key'],body['mode'])
            await self._commit(options,body,replan=False)
            await h.async_battery_inputs_refresh()
            await self.engine.controller.async_tick()
            return await self.view()
        if operation == 'save':
            incoming = body['configuration']
            if c.OPT_DEVICE_CONTROL_MAPPINGS in incoming:
                raise ValueError('Device mappings must be saved from their own card')
            options = self.prepare(existing,incoming)
        elif operation == 'save_device':
            choices = await h.async_cached_planning_configuration()
            device = next((d for d in choices['devices'] if d['key']==body['device_key']),None)
            view = next((d for d in await self.devices(choices) if d['key']==body['device_key']),None)
            incoming = body.get('configuration',{})
            if device is None:
                if body['device_key'] not in ('$battery','$ev','$pool') or body.get('mapping'):
                    raise ValueError('This device is no longer in the website inventory')
                allowed = {f['key'] for f in system_fields(body['device_key'][1:])}
            else:
                allowed = {f['key'] for f in (view or {}).get('system_fields',[])}
            if set(incoming)-allowed:
                raise ValueError('These settings belong to another device')
            options = self.prepare(existing,incoming)
            if device is not None:
                if device.get('planning_role') != 'controllable':
                    saved = self.resolved_options().get('device_control_mappings',{}).get(device['key'],{})
                    device = {**device,'control_type':saved.get('control_type')}
                options = save_device(options,device['key'],body['mapping'],device,self.read,
                    entity_names=self.context['entity_names'],area_names=self.context['area_names'],
                    entity_area_ids=self.context['entity_areas'])
        else:
            raise ValueError('Unsupported configuration action')
        result = await self._commit(options,body,replan=True)
        if operation == 'save_device' and device is not None:
            await h.async_report_device_mapping(device['key'],options.get(c.OPT_DEVICE_CONTROL_MAPPINGS,{}))
        return {**result,'saved':True,'refreshing':False}
