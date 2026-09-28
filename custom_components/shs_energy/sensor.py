"""Register stable native sensors; SHS values and explanations come from the app."""
from homeassistant.components.sensor import SensorEntity, SensorDeviceClass, SensorStateClass
from homeassistant.helpers.entity import EntityCategory
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
            else:added[key].update_descriptor(descriptor)
        if additions:async_add_entities(additions)
    update_catalogue()
    entry.async_on_unload(coordinator.async_add_listener(update_catalogue))


class ProjectedSensor(CoordinatorEntity,SensorEntity):
    _attr_has_entity_name=True
    def __init__(self,coordinator,descriptor):
        super().__init__(coordinator)
        self._attr_unique_id=descriptor['unique_id']
        self.update_descriptor(descriptor)
        entry=coordinator.entry
        self._attr_device_info=DeviceInfo(identifiers={(DOMAIN,entry.data[CONF_DEVICE_TOKEN_ID])},
            name=entry.data.get(CONF_CUSTOMER_NAME) or 'Smart Home Solutions',
            manufacturer='Smart Home Solutions',entry_type=DeviceEntryType.SERVICE)

    def update_descriptor(self, descriptor):
        self.descriptor = descriptor
        native_types = {'entity_category': EntityCategory, 'device_class': SensorDeviceClass,
                        'state_class': SensorStateClass}
        for key, value in descriptor.items():
            if key == 'unique_id':
                continue
            if key == 'name' and value is None and descriptor.get('translation_key'):
                # HA gives an explicit None name precedence over translations.
                # An unnamed wire field means the translated label owns the name.
                self.__dict__.pop('_attr_name', None)
                continue
            if value is not None and key in native_types:
                value = native_types[key](value)
            setattr(self, '_attr_' + key, value)

    def current(self):
        return (self.coordinator.data or {}).get(self._attr_unique_id,{})
    @property
    def available(self):return self.coordinator.online and self.current().get('available',False)
    @property
    def native_value(self):return self.current().get('value')
    @property
    def extra_state_attributes(self):return self.current().get('attributes',{})
