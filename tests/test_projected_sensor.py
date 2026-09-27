"""Wire metadata must become native HA enum instances, including on updates."""
from enum import StrEnum
from types import SimpleNamespace
import unittest
from test_execution_mode_select import load_adapter

class Category(StrEnum):
    DIAGNOSTIC = 'diagnostic'
class DeviceClass(StrEnum):
    POWER = 'power'
class StateClass(StrEnum):
    MEASUREMENT = 'measurement'
class CoordinatorEntity:
    def __init__(self, coordinator): self.coordinator = coordinator
class SensorEntity: pass

class SensorMetadataTests(unittest.TestCase):
    def test_native_metadata_and_catalogue_update_preserve_identity(self):
        adapter = load_adapter('sensor.py', dict(SensorEntity=SensorEntity,
            CoordinatorEntity=CoordinatorEntity, EntityCategory=Category,
            SensorDeviceClass=DeviceClass, SensorStateClass=StateClass,
            DeviceInfo=dict, DeviceEntryType=SimpleNamespace(SERVICE='service'),
            DOMAIN='shs_energy', CONF_DEVICE_TOKEN_ID='device_token_id', CONF_CUSTOMER_NAME='customer_name'))
        coordinator = SimpleNamespace(entry=SimpleNamespace(data={'device_token_id':'home'}),
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
