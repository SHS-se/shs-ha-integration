"""Native HA entities over an app-owned, durable entity catalogue."""
import asyncio
from copy import deepcopy
import logging
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from .shs_core.const import DOMAIN
from .shs_core.gateway_stream import GatewayRecord

_LOGGER=logging.getLogger(__name__)

class GatewayProjection(DataUpdateCoordinator[dict]):
    def __init__(self,hass,entry,service):
        super().__init__(hass,_LOGGER,name='shs_app_projection',config_entry=entry,update_interval=None)
        self.entry,self.service=entry,service
        self.projection=None
        self.catalogue=None
        self.online=False
        self.ready=asyncio.Event()
        self.platforms_loaded=False
        self.record=GatewayRecord(service.stream,'entity_catalogue')

    async def restore(self):
        self.catalogue=await self.record.async_load()
        if self.catalogue is not None:
            if self.catalogue.get('schema')!=1:raise ValueError('Unsupported SHS entity catalogue')
            self.ready.set()

    @property
    def app_url(self):
        return self.catalogue.get('app_url') if self.catalogue else None

    async def accept(self,value):
        if value.get('schema')!=2 or value.get('entities',{}).get('schema')!=1:
            raise ValueError('Unsupported SHS app entity projection')
        sensors=value['entities']['sensors']
        ids=[row['unique_id'] for row in sensors]
        if len(ids)!=len(set(ids)) or any(not key.startswith(self.entry.entry_id+'_') for key in ids):
            raise ValueError('Invalid SHS sensor identity')
        if set(ids)!=set(value['entities']['values']):raise ValueError('SHS sensor values differ from catalogue')
        catalogue=dict(schema=1,sensors=deepcopy(sensors),devices=[{**{k:deepcopy(row.get(k)) for k in ('key','name','planned','system_member')},
            'permission':{'controller_id':row['permission']['controller_id']}} for row in value['execution_devices']],app_url=value['app_url'])
        if catalogue!=self.catalogue:
            await self.record.async_save(catalogue)
            self.catalogue=catalogue
        self.projection=value
        self.online=True
        for key,repair in value['repairs'].items():
            severity=repair['severity']
            if severity in (None,'info'):ir.async_delete_issue(self.hass,DOMAIN,key)
            else:ir.async_create_issue(self.hass,DOMAIN,key,is_fixable=False,
                severity=ir.IssueSeverity.ERROR if severity=='error' else ir.IssueSeverity.WARNING,
                translation_key=key,translation_placeholders=repair['placeholders'])
        self.async_set_updated_data(value['entities']['values'])
        self.ready.set()

    def disconnected(self):
        self.online=False
        self.async_update_listeners()

    def async_add_battery_listener(self,listener):return self.async_add_listener(listener)

    async def async_backfill_prices(self,days):return await self.service.request_app('backfill_prices',{'days':days})
    async def async_profile(self,seconds):return await self.service.request_app('profile',{'allocation_seconds':seconds})
