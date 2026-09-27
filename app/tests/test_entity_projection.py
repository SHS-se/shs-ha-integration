"""The HA bridge restores identity without inventing live values while offline."""
import ast
import asyncio
from copy import deepcopy
import logging
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock
from shs_core.native_configuration import native_options
from shs_app.entities import project_modes

ROOT=Path(__file__).parents[2]/'custom_components/shs_energy'

def load(path,namespace):
    tree=ast.parse((ROOT/path).read_text())
    tree.body=[n for n in tree.body if not isinstance(n,(ast.Import,ast.ImportFrom))]
    exec(compile(tree,str(path),'exec'),namespace)
    return SimpleNamespace(**namespace)

class Coordinator:
    @classmethod
    def __class_getitem__(cls,_):return cls
    def __init__(self,hass,*args,**kwargs):self.hass=hass;self.data=None;self.notifications=0
    def async_set_updated_data(self,value):self.data=value;self.notifications+=1
    def async_update_listeners(self):self.notifications+=1

class Record:
    def __init__(self,stream,name):self.stream=stream
    async def async_load(self):return deepcopy(self.stream.saved)
    async def async_save(self,value):self.stream.saved=deepcopy(value)

class EntityProjectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_catalogue_survives_app_and_ha_restart_with_unavailable_values(self):
        module=load('gateway_projection.py',dict(asyncio=asyncio,deepcopy=deepcopy,logging=logging,
            ir=Mock(),DOMAIN='shs_energy',DataUpdateCoordinator=Coordinator,GatewayRecord=Record))
        service=SimpleNamespace(stream=SimpleNamespace(saved=None))
        entry=SimpleNamespace(entry_id='home',data={'device_token_id':'device'})
        coordinator=module.GatewayProjection(None,entry,service)
        await coordinator.restore();self.assertFalse(coordinator.ready.is_set())
        descriptor={'unique_id':'home_battery_controller','name':'Battery controller'}
        projected=dict(schema=2,entities=dict(schema=1,sensors=[descriptor],values={descriptor['unique_id']:
            dict(value='controlling',attributes={'explanation':'Following the plan'},available=True)},modes={}),
            execution_devices=[{'key':'$battery','name':'House battery','planned':True,'system_member':None,'permission':{'controller_id':'battery'}}],app_url='/app/shs',repairs={})
        await coordinator.accept(projected)
        class Base:
            def __init__(self,coordinator):self.coordinator=coordinator
        sensor_module=load('sensor.py',dict(CoordinatorEntity=Base,SensorEntity=object,DeviceInfo=dict,
            DeviceEntryType=SimpleNamespace(SERVICE='service'),DOMAIN='shs_energy',CONF_CUSTOMER_NAME='customer_name',CONF_DEVICE_TOKEN_ID='device_token_id'))
        sensor=sensor_module.ProjectedSensor(coordinator,descriptor)
        self.assertTrue(sensor.available);self.assertEqual(sensor.native_value,'controlling')
        coordinator.disconnected();self.assertFalse(sensor.available)
        reopened=module.GatewayProjection(None,entry,service);await reopened.restore()
        self.assertTrue(reopened.ready.is_set());self.assertFalse(reopened.online)
        self.assertEqual(reopened.catalogue['sensors'],[descriptor])
        self.assertEqual(reopened.catalogue['devices'],projected['execution_devices'])
        restored=sensor_module.ProjectedSensor(reopened,descriptor)
        self.assertFalse(restored.available);self.assertIsNone(restored.native_value)
        await reopened.accept(projected);self.assertTrue(restored.available)
        self.assertEqual(restored._attr_unique_id,sensor._attr_unique_id)

    async def test_native_configuration_excludes_app_connection_and_tariff_settings(self):
        source=dict(device_modes={'$battery':'controlling'},device_control_mappings={'heater':{'control_entity_id':'switch.heater'}},
            battery_charge_max_w=4000,device_token='private',base_url='https://example.invalid',supplier_price_entity='sensor.price',
            home_latitude=59,_observed_entities=['sensor.house'],discovery_evidence={'all':'metadata'})
        native=native_options(source)
        self.assertEqual(set(native),{'device_modes','device_control_mappings','battery_charge_max_w'})
        native['device_modes'].clear();self.assertTrue(source['device_modes'])
