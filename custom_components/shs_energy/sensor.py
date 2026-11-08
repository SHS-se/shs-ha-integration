"""Register stable native sensors; SHS values and explanations come from the app."""
from homeassistant.components.sensor import SensorEntity, SensorDeviceClass, SensorStateClass
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from .equipment import equipment_device_info, update_device_info
from .shs_core.const import DOMAIN, CONF_CUSTOMER_NAME, CONF_DEVICE_TOKEN_ID


def equipment_sensors(entry, devices):
    """Place the app's fixed equipment sensors using its device catalogue."""
    owners = {row['permission']['controller_id']: row for row in devices if not row.get('system_member')}
    water = [row for row in devices if row['planned'] and row.get('category') == 'hot_water']
    groups = (
        ('battery', 'House battery', ('battery_controller',)),
        ('ev', 'Vehicle charger', ('ev_controller', 'ev_planned_request', 'ev_planned_current')),
        ('pool', 'Pool heater', ('pool_controller', 'pool_planned_request')),
    )
    if water:
        boiler = water[0]['permission']['controller_id'] if len(water) == 1 else 'boiler'
        groups += ((boiler, 'Hot water', ('boiler_planned_request',)),)
    placements = {}
    for owner, name, sensors in groups:
        info = equipment_device_info(entry, owner, owners.get(owner, {}).get('name', name))
        for sensor in sensors:
            placements[f'{entry.entry_id}_{sensor}'] = info
    return placements


def home_device_info(entry):
    return DeviceInfo(identifiers={(DOMAIN, entry.data[CONF_DEVICE_TOKEN_ID])},
        name=entry.data.get(CONF_CUSTOMER_NAME) or 'Smart Home Solutions',
        manufacturer='Smart Home Solutions', entry_type=DeviceEntryType.SERVICE)


async def async_setup_entry(hass,entry,async_add_entities):
    coordinator=entry.runtime_data
    added={}
    def update_catalogue():
        additions=[]
        placements = equipment_sensors(entry, coordinator.catalogue['devices'])
        home = home_device_info(entry)
        for descriptor in coordinator.catalogue['sensors']:
            key=descriptor['unique_id']
            info = placements.get(key, home)
            if key not in added:
                added[key]=ProjectedSensor(coordinator,descriptor,info)
                additions.append(added[key])
            else:
                sensor = added[key]
                sensor.update_descriptor(descriptor)
                update_device_info(hass, entry, 'sensor', sensor, info)
        if additions:async_add_entities(additions)
    update_catalogue()
    entry.async_on_unload(coordinator.async_add_listener(update_catalogue))


class ProjectedSensor(CoordinatorEntity,SensorEntity):
    _attr_has_entity_name=True
    def __init__(self,coordinator,descriptor,info=None):
        super().__init__(coordinator)
        self._attr_unique_id=descriptor['unique_id']
        entry = coordinator.entry
        self._device_summary = self._attr_unique_id == f'{entry.entry_id}_devices_controller'
        self._attr_device_info = info if info is not None else home_device_info(entry)
        self.update_descriptor(descriptor)

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
    def extra_state_attributes(self):
        attributes = self.current().get('attributes', {})
        if self._device_summary:
            return {key: value for key, value in attributes.items() if key not in ('devices', 'explanation')}
        return attributes
