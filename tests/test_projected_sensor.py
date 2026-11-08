"""Wire metadata must become native HA enum instances, including on updates."""
from enum import StrEnum
from types import SimpleNamespace
import unittest
from unittest.mock import Mock
from test_execution_mode_select import device, load_adapter, load_equipment

class Category(StrEnum):
    DIAGNOSTIC = 'diagnostic'
class DeviceClass(StrEnum):
    POWER = 'power'
class StateClass(StrEnum):
    MEASUREMENT = 'measurement'
class CoordinatorEntity:
    def __init__(self, coordinator): self.coordinator = coordinator
class SensorEntity: pass

def sensor_adapter(equipment):
    return load_adapter('sensor.py', dict(SensorEntity=SensorEntity, CoordinatorEntity=CoordinatorEntity,
        EntityCategory=Category, SensorDeviceClass=DeviceClass, SensorStateClass=StateClass,
        DeviceInfo=dict, DeviceEntryType=SimpleNamespace(SERVICE='service'),
        DOMAIN='shs_energy', CONF_DEVICE_TOKEN_ID='device_token_id', CONF_CUSTOMER_NAME='customer_name',
        equipment_device_info=equipment.equipment_device_info))


class SensorMetadataTests(unittest.TestCase):
    def test_native_metadata_and_catalogue_update_preserve_identity(self):
        equipment = load_equipment()
        adapter = sensor_adapter(equipment)
        coordinator = SimpleNamespace(entry=SimpleNamespace(entry_id='home', data={'device_token_id':'token'}),
            online=True, data={'home_power':{'value':42,'available':True}})
        descriptor = dict(unique_id='home_power', entity_category='diagnostic',
            device_class='power', state_class='measurement', name='Power')
        sensor = adapter.ProjectedSensor(coordinator, descriptor)
        self.assertIs(sensor._attr_entity_category, Category.DIAGNOSTIC)
        self.assertIs(sensor._attr_device_class, DeviceClass.POWER)
        self.assertIs(sensor._attr_state_class, StateClass.MEASUREMENT)
        self.assertEqual(sensor.native_value,42)
        sensor.update_descriptor({**descriptor, 'entity_category':None, 'name':'Measured power'})
        self.assertIsNone(sensor._attr_entity_category)
        self.assertEqual(sensor._attr_name,'Measured power')
        self.assertEqual(sensor._attr_unique_id,'home_power')

    def test_translated_name_is_not_masked_by_null_wire_name(self):
        equipment = load_equipment()
        adapter = sensor_adapter(equipment)
        coordinator = SimpleNamespace(entry=SimpleNamespace(entry_id='home', data={'device_token_id':'token'}))
        descriptor = dict(unique_id='home_subscription',name=None,translation_key='subscription')
        sensor = adapter.ProjectedSensor(coordinator, descriptor)
        self.assertFalse(hasattr(sensor,'_attr_name'))
        self.assertEqual(sensor._attr_translation_key,'subscription')
        sensor.update_descriptor({**descriptor,'name':'Explicit label'})
        self.assertEqual(sensor._attr_name,'Explicit label')
        sensor.update_descriptor(descriptor)
        self.assertFalse(hasattr(sensor,'_attr_name'))
        self.assertEqual(sensor._attr_unique_id,'home_subscription')
        # A deliberately nameless entity without a translation still uses HA's
        # whole-device naming semantics.
        sensor.update_descriptor({**descriptor,'translation_key':None})
        self.assertIsNone(sensor._attr_name)

    def test_equipment_sensors_share_their_owners_page_and_leave_home_sensors_on_home(self):
        equipment = load_equipment()
        adapter = sensor_adapter(equipment)
        entry = SimpleNamespace(entry_id='home', data={'device_token_id': 'token'})
        boiler = {**device('sensor.boiler', name='Water boiler'), 'category': 'hot_water'}
        inventory = [device(), device('$pool', name='Pool heater'), device('$ev', name='Car charger'),
                     {**device('$pool', name='Pump'), 'system_member': 'pool'}, boiler]
        locations = adapter.equipment_sensors(entry, inventory)
        self.assertEqual(locations['home_battery_controller']['name'], 'House battery')
        self.assertEqual(locations['home_pool_controller'], locations['home_pool_planned_request'])
        self.assertEqual(locations['home_pool_controller']['name'], 'Pool heater')
        self.assertEqual(locations['home_ev_controller'], locations['home_ev_planned_current'])
        self.assertEqual(locations['home_ev_controller'], locations['home_ev_planned_request'])
        self.assertEqual(locations['home_boiler_planned_request'],
                         equipment.equipment_device_info(entry, 'device:sensor.boiler', 'Water boiler'))
        self.assertNotIn('home_grid_operator', locations)
        self.assertNotIn('home_devices_controller', locations)
        coordinator = SimpleNamespace(entry=entry, data={'home_devices_controller': {'value': 'commanded',
            'attributes': {'devices': {'sensor.boiler': {'state': 'commanded'}}, 'explanation': 'Details'}}})
        summary = adapter.ProjectedSensor(coordinator, {'unique_id': 'home_devices_controller'})
        self.assertEqual(summary.native_value, 'commanded')
        self.assertEqual(summary.extra_state_attributes, {})

    def test_catalogue_change_reassigns_a_registered_entity_without_recreating_it(self):
        registry = Mock()
        registry.async_get_entity_id.return_value = 'sensor.custom_boiler_request'
        devices = Mock()
        devices.async_get_or_create.return_value = SimpleNamespace(id='boiler-page')
        equipment = load_equipment(er=SimpleNamespace(async_get=lambda _: registry),
                                   dr=SimpleNamespace(async_get=lambda _: devices))
        entry = SimpleNamespace(entry_id='home', data={'device_token_id': 'token'})
        entity = SimpleNamespace(unique_id='home_boiler_planned_request',
                                 _attr_device_info={'identifiers': {('shs_energy', 'token')}})
        info = equipment.equipment_device_info(entry, 'device:sensor.boiler', 'Water boiler')
        equipment.update_device_info(None, entry, 'sensor', entity, info)
        equipment.update_device_info(None, entry, 'sensor', entity, info)
        registry.async_update_entity.assert_called_once_with('sensor.custom_boiler_request', device_id='boiler-page')
        self.assertEqual(entity.unique_id, 'home_boiler_planned_request')

    def test_legacy_heater_page_is_retired_only_after_all_entities_move(self):
        registry = Mock()
        devices = Mock()
        old = SimpleNamespace(id='old-page', identifiers={('shs_energy', 'token:heating:device:sensor.heater')})
        entries = Mock(side_effect=[[SimpleNamespace(disabled_by='user')], []])
        equipment = load_equipment(er=SimpleNamespace(async_get=lambda _: registry, async_entries_for_device=entries),
                                   dr=SimpleNamespace(async_get=lambda _: devices,
                                       async_entries_for_config_entry=lambda reg, entry: [old] if entry == 'home' else []))
        entry = SimpleNamespace(entry_id='home', data={'device_token_id': 'token'})
        equipment.remove_empty_heater_device(None, entry, 'device:sensor.heater')
        devices.async_remove_device.assert_not_called()
        equipment.remove_empty_heater_device(None, entry, 'device:sensor.heater')
        devices.async_remove_device.assert_called_once_with('old-page')
