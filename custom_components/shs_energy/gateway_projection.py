"""Stable HA entity and editor views over compact app-owned projections."""
import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import logging

from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .shs_core.const import DOMAIN
from .shs_core.device_controls import battery_setup_attention
from .shs_core.gateway_journal import GatewayConflict
from .shs_core.const import ISSUE_BATTERY_CONTROL
from .shs_core.optimisation import PlanContractCache
from .shs_core.operating_modes import scoped_plan
from .shs_core.presentation import operational_status
from .shs_core.runtime_projection import FIELDS

_LOGGER = logging.getLogger(__name__)


class ProjectedSnapshot:
    def __init__(self, coordinator, name):
        self.coordinator, self.name = coordinator, name

    def snapshot(self):
        if self.coordinator.projection is None:
            return {'state':'pending','reason':'Waiting for the SHS app'}
        return deepcopy(self.coordinator.projection[self.name])


class ProjectedController:
    def __init__(self, coordinator):
        self.coordinator = coordinator
        self.lock = coordinator.service.physical.lock

    @property
    def status(self):
        if self.coordinator.projection is None:
            return {device:{'state':'pending','reason':'Waiting for the SHS app'} for device in ('battery','ev','pool')}
        return self.coordinator.projection['controllers']

    def options(self):
        return self.coordinator.service.source.options()

    def preview_commands(self, *args, **kwargs):
        return self.coordinator.service.physical.preview_commands(*args, **kwargs)

    def add_listener(self, listener, reports=None):
        return self.coordinator.async_add_listener(listener)

    async def async_tick(self):
        return await self.coordinator.service.request_app('tick',{'configuration_changed':self.coordinator._plan_configuration_changed})


class GatewayProjection(DataUpdateCoordinator[dict]):
    def __init__(self,hass,entry,service):
        super().__init__(hass,_LOGGER,name='shs_app_projection',config_entry=entry,update_interval=None)
        self.entry,self.service = entry,service
        self.projection = None
        self.ready = asyncio.Event()
        self.platforms_loaded = False
        self.controller = ProjectedController(self)
        self.battery_runtime = ProjectedSnapshot(self,'battery')
        self.battery_live_inputs = ProjectedSnapshot(self,'battery_live_inputs')
        self.battery_writer = ProjectedSnapshot(self,'battery_writer')
        self._plan_contract = PlanContractCache(lambda:self.optimisation_plan)
        self._plan_configuration_changed = False
        self._battery_attention = None

    def __getattr__(self,key):
        if key not in FIELDS or key == 'data':
            raise AttributeError(key)
        projection = self.__dict__.get('projection')
        if projection is not None:
            return projection['values'][key]
        if key == 'operational_status':
            return {'state':'pending','actionable':False,'reason':'Waiting for the SHS app','recovering':True,'retry_at':None}
        if key in ('tariff_components',):return {}
        if key in ('attention_items','optimisation_missing_inputs','optimisation_degraded_devices',
                   'optimisation_unplanned_services','missing_questions','incomplete_readings','skipped_readings',
                   'grid_price_forecast','total_price_forecast','latest_display_components','replan_recommendations',
                   'device_control_mapping_gaps'):return []
        return None

    @property
    def attention_items(self):
        rows = deepcopy(self.projection['values']['attention_items']) if self.projection else []
        if self._battery_attention is not None:
            rows = [row for row in rows if row['key'] != ISSUE_BATTERY_CONTROL]
            if self._battery_attention:
                rows.append(self._battery_attention)
        return sorted(rows,key=lambda item:({'error':0,'warning':1}.get(item['severity'],2),item['title']))

    def accept(self,value):
        if value.get('schema') != 1 or set(value.get('values',{})) != set(FIELDS):
            raise ValueError('Unsupported SHS app projection')
        self.projection = value
        self._battery_attention = None
        for key,repair in value['repairs'].items():
            self.publish_repair(key,repair['severity'],repair['placeholders'])
        self.async_set_updated_data(value['values']['data'])
        self.ready.set()

    def publish_repair(self,key,severity,placeholders):
        if severity in (None,'info'):
            ir.async_delete_issue(self.hass,DOMAIN,key)
        else:
            ir.async_create_issue(self.hass,DOMAIN,key,is_fixable=False,
                severity=ir.IssueSeverity.ERROR if severity=='error' else ir.IssueSeverity.WARNING,
                translation_key=key,translation_placeholders=placeholders)

    def _sync_battery_control_issue(self,options,*,included):
        value = battery_setup_attention(options,included=included)
        self._battery_attention = ({'key':ISSUE_BATTERY_CONTROL,**{key:v for key,v in value.items() if key!='placeholders'}} if value else {})
        self.publish_repair(ISSUE_BATTERY_CONTROL,value['severity'] if value else None,value['placeholders'] if value else {})

    def async_add_control_listener(self,listener):
        return self.async_add_listener(listener)

    def async_add_battery_listener(self,listener):
        return self.async_add_listener(listener)

    def binding_plan_for(self,device,options):
        plan = scoped_plan(self.optimisation_plan,options,device)
        now = datetime.now(timezone.utc)
        status = operational_status(plan,options.get('planning_mode','live'),[],now,validate=self._plan_contract)
        if device=='battery' and plan and (self.optimisation_plan or {}).get('battery_execution'):
            plan = {**plan,'battery_execution':self.optimisation_plan['battery_execution']}
        if not status['actionable']:return plan or {},None
        return plan,next((slot for slot in plan['plans']['priority']['slots'] if
            datetime.fromisoformat(slot['start']) <= now < datetime.fromisoformat(slot['start'])+timedelta(minutes=15)),None)

    def options_update_requires_reload(self, options=None):
        # Configuration stays canonical in HA; the app consumes its new receipt.
        return False

    def resolved_options(self):
        return self.service.source.options()

    async def async_optimisation_push(self, *, force_plan=False):
        return await self.service.request_app('optimisation',{'force_plan':force_plan})

    async def async_report_runtime(self):
        return await self.service.request_app('runtime_report',{})

    def cached(self,name):
        if self.projection is None:
            raise GatewayConflict('The SHS app has not published its configuration yet')
        return deepcopy(self.projection['cached'][name])

    async def async_cached_device_configuration(self):return self.cached('devices')
    async def async_cached_home_configuration(self):return self.cached('home')
    async def async_cached_planning_configuration(self):return self.cached('planning')
    async def async_cached_exchange_status(self):return self.cached('exchange')
    async def async_refresh_device_configuration(self):return await self.service.request_app('refresh_devices',{})
    async def async_report_device_mapping(self,device_key,mappings):
        return await self.service.request_app('report_mapping',dict(device_key=device_key,mappings=mappings))
    async def async_backfill_prices(self,days):return await self.service.request_app('backfill_prices',{'days':days})
    async def async_request_refresh(self):return await self.service.request_app('refresh',{})
    async def async_battery_inputs_refresh(self):return await self.service.request_app('tick',{})
    async def async_replan(self):return await self.service.request_app('replan',{})
    async def async_profile(self,seconds):return await self.service.request_app('profile',{'allocation_seconds':seconds})
    async def async_diagnostics(self):
        if self.projection is None:raise GatewayConflict('Waiting for app diagnostics')
        return {**deepcopy(self.projection['diagnostics']),'battery_live_inputs':self.battery_live_inputs.snapshot(),
            'battery_runtime':self.battery_runtime.snapshot(),'battery_writer':self.battery_writer.snapshot()}
