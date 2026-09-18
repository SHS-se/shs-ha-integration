"""Exercise native entity lifecycle and shared mode changes with fake HA ports."""
import ast
import asyncio
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest

ROOT = Path(__file__).parents[1] / 'custom_components' / 'shs_energy'
sys.path.append(str(ROOT))
from operating_modes import device_mode, execution_mode_options
import const


def load_adapter(filename, namespace):
    """Run adapter classes/functions with HA ports supplied by this test."""
    tree = ast.parse((ROOT / filename).read_text())
    tree.body = [n for n in tree.body if not isinstance(n, (ast.Import, ast.ImportFrom))]
    exec(compile(tree, filename, 'exec'), namespace)
    return SimpleNamespace(**namespace)


def device(key='$battery', *, planned=True, reason=None, name='House battery'):
    system = key[1:] if key.startswith('$') else None
    return {'key': key, 'name': name, 'system': system, 'planned': planned,
        'permission': {'reason': reason, 'controller_id': system or 'device:' + key}}


class Entity:
    hass = None
    writes = 0
    async def async_remove(self, *, force_remove=False):
        self.removed = force_remove
        await self.async_will_remove_from_hass()
    def async_write_ha_state(self):
        self.writes += 1


class Registry:
    def __init__(self): self.rows = {}
    def async_remove(self, entity_id): del self.rows[entity_id]


class Rig:
    def __init__(self):
        self.devices = [device(), device('sensor.heater', name='Heater'), device('sensor.fridge', planned=False)]
        self.tasks = []
        self.replans = []
        self.ticks = 0
        self.added = []
        self.registry = Registry()
        self.listeners = []
        self.entry = SimpleNamespace(entry_id='home', options={'device_modes': {
            '$battery': 'control_verification', 'sensor.heater': 'control_verification'}},
            data={const.CONF_DEVICE_TOKEN_ID: 'token'}, async_create_background_task=self.background)
        self.entry.runtime_data = SimpleNamespace(controller=SimpleNamespace(async_tick=self.tick),
            async_optimisation_push=self.replan, async_update_listeners=self.notify)
        self.hass = SimpleNamespace(config_entries=SimpleNamespace(async_update_entry=self.update_entry))
        self.shared = load_adapter('control_configuration.py', {'datetime': datetime, 'timezone': timezone,
            'shs_const': const, 'execution_mode_options': execution_mode_options})
        # Exercise the real shared mode action; only its cached inventory port is fake.
        self.shared.async_set_execution_mode.__globals__['async_execution_devices'] = self.get_devices
        self.adapter = load_adapter('select.py', {'asyncio': asyncio, 'callback': lambda fn: fn,
            'SelectEntity': Entity, 'EntityCategory': SimpleNamespace(CONFIG='config'),
            'DeviceInfo': dict, 'DeviceEntryType': SimpleNamespace(SERVICE='service'),
            'DOMAIN': const.DOMAIN, 'CONF_CUSTOMER_NAME': const.CONF_CUSTOMER_NAME,
            'CONF_DEVICE_TOKEN_ID': const.CONF_DEVICE_TOKEN_ID, 'MODES': ('control_verification','controlling'),
            'device_mode': device_mode, 'HomeAssistantError': RuntimeError,
            'async_execution_devices': self.get_devices,
            'async_set_execution_mode': self.shared.async_set_execution_mode,
            'er': SimpleNamespace(async_get=lambda hass:self.registry,
                async_entries_for_config_entry=lambda reg,eid:[r for r in reg.rows.values() if r.config_entry_id==eid])})
        self.manager = self.adapter.ExecutionModeEntities(self.hass,self.entry,self.add)
        self.listeners.append(self.manager.schedule_refresh)

    async def get_devices(self,hass,entry): return deepcopy(self.devices)
    async def tick(self): self.ticks += 1
    async def replan(self,**kwargs): self.replans.append(kwargs)
    def update_entry(self,entry,*,options): entry.options=options
    def notify(self):
        for listener in self.listeners: listener()
    def background(self,hass,coro,*,name):
        task=asyncio.create_task(coro);self.tasks.append(task);return task
    async def add(self,entities):
        for entity in entities:
            entity.hass=self.hass
            entity.entity_id='select.'+entity.device['key'].replace('$','').replace('.','_')
            self.registry.rows[entity.entity_id]=SimpleNamespace(entity_id=entity.entity_id,
                unique_id=entity._attr_unique_id,domain='select',platform=const.DOMAIN,config_entry_id='home')
            await entity.async_added_to_hass();self.added.append(entity)
    async def drain(self):
        while any(not task.done() for task in self.tasks):
            await asyncio.gather(*self.tasks)


class SelectTests(unittest.IsolatedAsyncioTestCase):
    async def test_schedule_membership_removes_active_and_registry_and_readds(self):
        r=Rig();await r.manager.refresh()
        self.assertEqual(set(r.manager.entities),{'$battery','sensor.heater'})
        battery=r.manager.entities['$battery'];unique=battery._attr_unique_id
        r.devices[0]['planned']=False
        await r.manager.refresh()
        self.assertTrue(battery.removed)
        self.assertNotIn('select.battery',r.registry.rows)
        r.devices[0]['planned']=True
        await r.manager.refresh()
        self.assertEqual(r.manager.entities['$battery']._attr_unique_id,unique)
        self.assertEqual(r.manager.entities['$battery'].current_option,'control_verification')

    async def test_registry_cleanup_on_restart_including_disabled_entities(self):
        r=Rig()
        r.registry.rows['select.old']=SimpleNamespace(entity_id='select.old',unique_id='home_execution_mode_gone',
            domain='select',platform=const.DOMAIN,config_entry_id='home',disabled_by='user')
        r.registry.rows['sensor.keep']=SimpleNamespace(entity_id='sensor.keep',unique_id='home_price',
            domain='sensor',platform=const.DOMAIN,config_entry_id='home')
        await r.manager.refresh()
        self.assertNotIn('select.old',r.registry.rows)
        self.assertIn('sensor.keep',r.registry.rows)

    async def test_automation_and_panel_use_same_persistence_and_replan(self):
        r=Rig();await r.manager.refresh();entity=r.manager.entities['$battery']
        await entity.async_select_option('controlling');await r.drain()
        self.assertEqual(entity.current_option,'controlling')
        self.assertEqual(r.entry.options['device_modes']['sensor.heater'],'control_verification')
        self.assertEqual(r.replans,[{'force_plan':True}]);self.assertEqual(r.ticks,1)
        await r.shared.async_set_execution_mode(r.hass,r.entry,'$battery','control_verification');await r.drain()
        self.assertEqual(entity.current_option,'control_verification')
        self.assertEqual(len(r.replans),2)
        await entity.async_select_option('control_verification');await r.drain()
        self.assertEqual(len(r.replans),2)
        self.assertEqual(entity._attr_entity_category,'config')

    async def test_blocked_control_then_setup_correction_then_exclusion(self):
        r=Rig();await r.manager.refresh();entity=r.manager.entities['$battery']
        r.devices[0]['permission']['reason']='Configure Instantaneous house consumption and Signed grid power'
        before=deepcopy(r.entry.options)
        with self.assertRaisesRegex(RuntimeError,'Instantaneous house consumption and Signed grid power'):
            await entity.async_select_option('controlling')
        self.assertEqual(r.entry.options,before);self.assertEqual(r.replans,[])
        r.devices[0]['permission']['reason']=None
        await entity.async_select_option('controlling');await r.drain()
        self.assertEqual(entity.current_option,'controlling')
        r.devices[0]['planned']=False
        with self.assertRaisesRegex(RuntimeError,'Planned, Included'):
            await entity.async_select_option('controlling')
        await r.manager.refresh();self.assertTrue(entity.removed)

    async def test_verification_can_always_withdraw_control_while_plan_unavailable(self):
        r=Rig();r.entry.options['device_modes']['$battery']='controlling'
        r.devices[0]['permission']['reason']='Waiting for a plan'
        await r.manager.refresh()
        await r.manager.entities['$battery'].async_select_option('control_verification');await r.drain()
        self.assertEqual(r.entry.options['device_modes']['$battery'],'control_verification')

    async def test_refresh_coalesces_and_rechecks_change_during_await(self):
        r=Rig();started=asyncio.Event();release=asyncio.Event();calls=0
        async def delayed(hass,entry):
            nonlocal calls
            calls+=1;value=deepcopy(r.devices)
            if calls==1:started.set();await release.wait()
            return value
        r.manager.refresh.__globals__['async_execution_devices']=delayed
        r.manager.schedule_refresh();await started.wait()
        r.devices[0]['planned']=False
        for _ in range(20):r.manager.schedule_refresh()
        release.set();await r.drain()
        self.assertEqual(calls,2)
        self.assertNotIn('$battery',r.manager.entities)
        r.manager.close();r.manager.schedule_refresh()
        self.assertEqual(len(r.tasks),1)

    async def test_invalid_option_and_removed_device_never_grant_control(self):
        r=Rig();await r.manager.refresh()
        for key,mode in [('$battery','monitoring'),('gone','controlling')]:
            with self.assertRaises(ValueError):
                await r.shared.async_set_execution_mode(r.hass,r.entry,key,mode)
        self.assertEqual(r.ticks,0)

    async def test_real_schedule_view_removes_website_monitoring_and_local_exclusion(self):
        from presentation import complete_device_views
        from device_controls import apply_planner_support, mapping_report, is_room_thermal_control
        from configuration_fields import _control_fields
        from test_device_controls import _battery
        r=Rig();r.entry.options.update(_battery())
        r.entry.runtime_data.operational_status={'actionable':False,'reason':'Waiting for a plan'}
        r.entry.runtime_data.optimisation_plan=None;r.entry.runtime_data.controller.status={}
        r.hass.states=SimpleNamespace(async_all=lambda:[])
        raw={'key':'sensor.heater','name':'Heater','planning_role':'controllable','control_type':'switch_schedule'}
        choices={'devices':[raw],'home':{'battery':{'included':True}},'refreshed_at':datetime.now(timezone.utc).isoformat()}
        namespace=r.shared.execution_device_views.__globals__
        namespace.update(resolved_options=lambda hass,options:options,
            entity_display_name_by_id=lambda hass:{},area_name_by_id=lambda hass:{},entity_area_id_by_id=lambda hass:{},
            suggest_device_control_mapping=lambda *args:{},apply_planner_support=apply_planner_support,
            mapping_report=mapping_report,is_room_thermal_control=is_room_thermal_control,
            _control_fields=_control_fields,complete_device_views=complete_device_views)
        async def views(hass,entry):return r.shared.execution_device_views(hass,entry,choices)
        r.manager.refresh.__globals__['async_execution_devices']=views
        await r.manager.refresh();self.assertEqual(set(r.manager.entities),{'$battery','sensor.heater'})
        raw['planning_role']='base_load'
        await r.manager.refresh();self.assertEqual(set(r.manager.entities),{'$battery'})
        r.entry.options['excluded_device_readings']=['$battery']
        await r.manager.refresh();self.assertEqual(r.manager.entities,{})
        self.assertFalse(r.registry.rows)
        r.entry.options['excluded_device_readings']=[]
        choices['home']['battery']['included']=False
        await r.manager.refresh();self.assertFalse(r.manager.entities)

    async def test_first_inventory_applies_same_inclusion_defaults_before_exposing_entities(self):
        from configuration_schema import initialise_device_inclusion
        r=Rig()
        module=load_adapter('control_configuration.py', {'initialise_device_inclusion':initialise_device_inclusion})
        def views(hass,entry,choices):
            return [{'key':'$battery','mapping_status':'not_configured',
                'planned':'$battery' not in entry.options.get('excluded_device_readings',[])}]
        module.async_execution_devices.__globals__['execution_device_views']=views
        result=await module.async_execution_devices(r.hass,r.entry,{'devices':[]})
        self.assertFalse(result[0]['planned'])
        self.assertEqual(r.entry.options['excluded_device_readings'],['$battery'])
        self.assertTrue(r.entry.runtime_data._plan_configuration_changed)
