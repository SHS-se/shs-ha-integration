"""Register stable native sensors; SHS values and explanations come from the app."""
from homeassistant.components.sensor import SensorEntity
from homeassistant.helpers.device_registry import DeviceEntryType,DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from .shs_core.const import DOMAIN,CONF_CUSTOMER_NAME,CONF_DEVICE_TOKEN_ID


async def async_setup_entry(hass,entry,async_add_entities):
    coordinator=entry.runtime_data
    added={}
    def update_catalogue():
        additions=[]
        for descriptor in coordinator.catalogue['sensors']:
            key=descriptor['unique_id']
            if key not in added:
                added[key]=ProjectedSensor(coordinator,descriptor)
                additions.append(added[key])
            else:added[key].descriptor=descriptor
        if additions:async_add_entities(additions)
    update_catalogue()
    entry.async_on_unload(coordinator.async_add_listener(update_catalogue))


class ProjectedSensor(CoordinatorEntity,SensorEntity):
    _attr_has_entity_name=True
    def __init__(self,coordinator,descriptor):
        super().__init__(coordinator)
        self.descriptor=descriptor
        self._attr_unique_id=descriptor['unique_id']
        for key,value in descriptor.items():
            if key!='unique_id':setattr(self,'_attr_'+key,value)
        entry=coordinator.entry
        self._attr_device_info=DeviceInfo(identifiers={(DOMAIN,entry.data[CONF_DEVICE_TOKEN_ID])},
            name=entry.data.get(CONF_CUSTOMER_NAME) or 'Smart Home Solutions',
            manufacturer='Smart Home Solutions',entry_type=DeviceEntryType.SERVICE)

    def current(self):
        return (self.coordinator.data or {}).get(self._attr_unique_id,{})
    @property
    def available(self):return self.coordinator.online and self.current().get('available',False)
    @property
    def native_value(self):return self.current().get('value')
    @property
    def extra_state_attributes(self):return self.current().get('attributes',{})
