"""House-wide import checks use live readings and owned native settings."""
from copy import deepcopy
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

import test_gateway_metadata as metadata
import test_verification as verification
import test_battery_gateway as battery
from shs_core.grid_capacity import (required_import, import_limit, empty_capacity,
    prepare_capacity, finish_capacity, observe_capacity, capacity_bound, dispatch_capacity, commanded_controls)
from shs_core.command_journal import Command, NativeAction
from shs_core.device_operations import ControlDeadlineError


class ImportArithmeticTests(unittest.TestCase):
    def test_measured_and_commanded_ev_demand_share_one_partition(self):
        self.assertEqual(required_import(9000, [(3000,6000),(0,2000)]), 14000)
        self.assertEqual(required_import(1000, [(0,11040),(0,4000)]), 16040)
        for cap in (None, 0, -1, float('nan'), float('inf'), True):
            with self.assertRaises(ValueError):
                import_limit(cap)


class LiveImportTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = metadata.MetadataTests.asyncSetUp

    def configure_ev(self):
        options = self.source.configuration.options()
        options.update(ev_charge_switch_entity='switch.ev', device_control_mappings={
            'ev': {'control_type':'variable_power', 'control_entity_id':'number.ev', 'power':'sensor.ev'}})
        self.source.configuration.options = lambda:deepcopy(options)
        self.source.report = self.rows.get
        self.rows['switch.ev'] = {'state':'off', 'attributes':{}}
        self.rows['number.ev'] = {'state':'16', 'attributes':{'unit_of_measurement':'A', 'min':0, 'max':16, 'step':1}}
        self.rows['sensor.ev'] = {'state':'0', 'attributes':{'unit_of_measurement':'W', 'state_class':'measurement'},
                                  'last_reported':self.rows['sensor.house']['last_reported']}
        self.physical.ownership.records['ev'] = {'options':options,
            'originals':{'switch.ev':'off', 'number.ev':'8'}, 'last_commands':{}}
        self.rows['select.mode']['state'] = 'Standby'
        self.rows['number.charge']['state'] = '0'
        self.rows['sensor.battery']['state'] = '0'
        executor = self.physical.native_executor
        executor.capacity = self.source
        executor.transport.journal.capacity = self.source
        executor.read_state = lambda entity:SimpleNamespace(**self.rows[entity])

    async def test_car_cannot_start_when_house_load_leaves_insufficient_capacity(self):
        self.configure_ev()
        self.rows['sensor.house']['state'] = '9000'
        with self.assertRaisesRegex(ControlDeadlineError, 'house import capacity'):
            self.source.external_ready('ev', entity='switch.ev', value='on')

    async def native(self, identity, device, entity, service, value=None):
        await self.physical.native_executor.execute(
            Command(identity, 'controller', device, 'plan', NativeAction(entity,service,value)),
            authorize=lambda:None, timeout=1)

    async def test_accepted_ev_start_reserves_capacity_before_meters_respond(self):
        self.configure_ev()
        self.service.import_limit_w = 15000
        await self.native('ev-start', 'ev', 'switch.ev', 'turn_on')
        # Desired ownership can change without sending a command (stale native
        # equality). It must never erase the independent accepted demand.
        self.physical.ownership.records['ev']['last_commands'] = {'switch.ev':'off', 'number.ev':6}
        self.rows['number.ev']['state'] = '6'
        self.assertEqual(self.source.import_required(), 12040)
        self.rows['select.mode']['state'] = 'Command Charging (PV First)'
        with self.assertRaisesRegex(ControlDeadlineError, 'house import capacity'):
            await self.native('battery-increase', 'battery', 'number.charge', 'set_value', 4)
        self.assertEqual(len(self.calls), 1)

    async def test_pending_battery_charge_reserves_capacity_while_register_and_power_lag(self):
        self.configure_ev()
        self.service.import_limit_w = 15000
        self.rows['select.mode']['state'] = 'Command Charging (PV First)'
        await self.native('battery-start', 'battery', 'number.charge', 'set_value', 4)
        self.assertEqual(self.rows['number.charge']['state'], '0')
        self.assertEqual(self.rows['sensor.battery']['state'], '0')
        with self.assertRaisesRegex(ControlDeadlineError, 'house import capacity'):
            await self.native('ev-start', 'ev', 'switch.ev', 'turn_on')
        self.assertEqual(len(self.calls), 1)

    async def test_battery_reduction_remains_possible_when_house_is_over_limit(self):
        self.configure_ev()
        self.rows['sensor.house']['state'] = '20000'
        self.rows['select.mode']['state'] = 'Command Charging (PV First)'
        self.rows['number.charge']['state'] = '4'
        await self.native('battery-relief', 'battery', 'number.charge', 'set_value', 0)
        self.assertEqual(len(self.calls), 1)

    async def test_generic_switch_adds_its_reviewed_power_to_current_house_demand(self):
        self.configure_ev()
        options = self.source.configuration.options()
        options['device_control_mappings']['pool'] = {'control_type':'switch_schedule',
            'actuator_entity_ids':['switch.pool'], 'power':2000}
        self.source.configuration.options = lambda:deepcopy(options)
        self.rows['switch.pool'] = {'state':'off', 'attributes':{}}
        self.rows['sensor.house']['state'] = '14000'
        self.service.import_limit_w = 15000
        with self.assertRaisesRegex(ControlDeadlineError, '16000 W required'):
            await self.native('pool-start', 'pool', 'switch.pool', 'turn_on')
        self.assertFalse(self.calls)

    async def test_nonfinite_current_never_creates_free_capacity(self):
        self.configure_ev()
        self.rows['switch.ev']['state'] = 'on'
        self.rows['number.ev']['state'] = 'nan'
        with self.assertRaises(ValueError):
            self.source.import_required()

    async def test_unknown_device_maximum_blocks_only_its_increase(self):
        self.configure_ev()
        self.service.import_limit_w = 15000
        options = self.source.configuration.options()
        options['device_control_mappings']['pool'] = {'control_type':'switch_schedule',
            'actuator_entity_ids':['switch.pool'], 'power':'sensor.pool'}
        self.source.configuration.options = lambda:deepcopy(options)
        self.rows['switch.pool'] = {'state':'on', 'attributes':{}}
        with self.assertRaisesRegex(ValueError, 'maximum electrical power in watts'):
            self.source.describe_import('pool','switch.pool','on')
        await self.native('pool-off', 'pool', 'switch.pool', 'turn_off')
        await self.native('ev-start', 'ev', 'switch.ev', 'turn_on')
        self.assertEqual(len(self.calls), 2)

    async def test_stale_high_battery_readback_does_not_turn_new_increase_into_relief(self):
        self.configure_ev()
        self.service.import_limit_w = 15000
        self.rows['select.mode']['state'] = 'Command Charging (PV First)'
        self.rows['number.charge']['state'] = '4'
        await self.native('accepted-stop', 'battery', 'number.charge', 'set_value', 0)
        self.rows['sensor.house']['state'] = '13000'
        with self.assertRaises(ControlDeadlineError):
            await self.native('not-relief', 'battery', 'number.charge', 'set_value', 3)
        self.assertEqual(len(self.calls), 1)

    async def test_remapped_charger_is_an_additional_load_until_old_demand_is_released(self):
        self.configure_ev()
        self.service.import_limit_w = 15000
        await self.native('old-start', 'ev', 'switch.ev', 'turn_on')
        self.physical.ownership.records.clear()
        options = self.source.configuration.options()
        options['ev_charge_switch_entity'] = 'switch.new_ev'
        options['device_control_mappings'] = {'new_ev':{'control_type':'variable_power',
            'control_entity_id':'number.new_ev','power':'sensor.new_ev'}}
        self.source.configuration.options = lambda:deepcopy(options)
        for new,old in [('switch.new_ev','switch.ev'),('number.new_ev','number.ev'),('sensor.new_ev','sensor.ev')]:
            self.rows[new] = deepcopy(self.rows[old])
        with self.assertRaises(ControlDeadlineError):
            await self.native('new-start', 'ev', 'switch.new_ev', 'turn_on')
        self.assertEqual(len(self.calls), 1)

    async def test_transient_description_error_does_not_fault_durable_transport(self):
        from shs_core.command_transport import CommandNotSent
        self.configure_ev()
        self.service.import_limit_w = 15000
        self.rows['number.ev']['state'] = 'unavailable'
        with self.assertRaises(CommandNotSent):
            await self.native('unavailable', 'ev', 'switch.ev', 'turn_on')
        self.assertIsNone(self.physical.native_executor.transport.fault)
        self.rows['number.ev']['state'] = '16'
        await self.native('recovered', 'ev', 'switch.ev', 'turn_on')
        self.assertEqual(len(self.calls), 1)

    async def test_equal_stale_reading_requires_an_actual_reassertion(self):
        self.configure_ev()
        self.service.import_limit_w = 15000
        await self.native('start', 'ev', 'switch.ev', 'turn_on')
        self.assertEqual(self.rows['switch.ev']['state'], 'off')
        self.assertTrue(self.physical.native_executor.requires_command('switch.ev','off'))
        self.assertFalse(self.physical.native_executor.requires_command('switch.ev','on'))

    async def test_stale_high_current_cannot_exempt_a_new_ev_increase(self):
        self.configure_ev()
        self.service.import_limit_w = 15000
        self.rows['switch.ev']['state'] = 'on'
        await self.native('lower-current', 'ev', 'number.ev', 'set_value', 6)
        self.rows['sensor.house']['state'] = '13000'
        with self.assertRaises(ControlDeadlineError):
            await self.native('higher-current', 'ev', 'number.ev', 'set_value', 10)
        self.assertEqual(len(self.calls),1)

    async def test_cold_cache_load_retains_accepted_demand(self):
        self.configure_ev()
        self.service.import_limit_w = 15000
        await self.native('start', 'ev', 'switch.ev', 'turn_on')
        self.source.capacity=empty_capacity()
        await self.source.refresh_capacity()
        self.assertEqual(self.source.import_required(),12040)

    async def test_switch_off_still_sends_when_current_is_unavailable_without_freeing_pending_demand(self):
        self.configure_ev()
        self.service.import_limit_w=15000
        await self.native('start','ev','switch.ev','turn_on')
        self.rows['number.ev']['state']='unavailable'
        await self.native('stop','ev','switch.ev','turn_off')
        self.assertEqual(len(self.calls),2)
        ev = next(row for key,row in self.source.capacity['devices'].items() if key.startswith('ev@'))
        self.assertEqual(capacity_bound(ev),11040)

    async def test_enabled_mapped_load_keeps_its_rating_during_zero_power_readings(self):
        self.configure_ev()
        self.service.import_limit_w=15000
        options=self.source.configuration.options()
        options['device_control_mappings']['pool']={'control_type':'switch_schedule',
            'actuator_entity_ids':['switch.pool'],'power':2000}
        self.source.configuration.options=lambda:deepcopy(options)
        self.rows['switch.pool']={'state':'on','attributes':{}}
        self.rows['sensor.house']['state']='14000'
        self.rows['select.mode']['state']='Command Charging (PV First)'
        with self.assertRaises(ControlDeadlineError):
            await self.native('battery-extra','battery','number.charge','set_value',1)
        self.assertFalse(self.calls)

    async def test_stopping_retains_accepted_current_for_the_next_start(self):
        self.configure_ev()
        self.service.import_limit_w=15000
        self.rows['number.ev']['state']='6'
        await self.native('set-current','ev','number.ev','set_value',16)
        await self.native('stop','ev','switch.ev','turn_off')
        for entity in ('switch.ev','sensor.ev'):
            await self.stream.capture('observation',{'entity_id':entity,**deepcopy(self.rows[entity])})
        await self.source.refresh_capacity()
        self.rows['sensor.house']['state']='5000'
        self.assertTrue(self.source.requires_command('number.ev',6))
        with self.assertRaises(ControlDeadlineError):
            await self.native('restart','ev','switch.ev','turn_on')
        self.assertEqual(len(self.calls),2)

    async def test_grid_first_and_unsupported_automatic_modes_do_not_create_free_headroom(self):
        for mode in ('Command Charging (Grid First)','Time of Use'):
            self.configure_ev()
            self.service.import_limit_w=15000
            self.rows['select.mode']['attributes']['options'].append(mode)
            self.rows['select.mode']['state']=mode
            self.rows['number.charge']['state']='4'
            with self.assertRaises(ControlDeadlineError):
                await self.native('ev-'+mode,'ev','switch.ev','turn_on')
        self.assertFalse(self.calls)

    async def test_entering_charging_mode_with_positive_native_ceiling_always_checks_budget(self):
        self.configure_ev()
        self.service.import_limit_w=15000
        self.rows['select.mode']['attributes']['options'].append('Time of Use')
        self.rows['select.mode']['state']='Time of Use'
        self.rows['number.charge']['state']='3'
        self.rows['sensor.house']['state']='13000'
        with self.assertRaises(ControlDeadlineError):
            await self.native('force-charge','battery','select.mode','select_option','Command Charging (PV First)')
        self.assertFalse(self.calls)


class CurrentReductionTests(unittest.IsolatedAsyncioTestCase):
    setUp = verification.VerificationTests.setUp

    async def test_lower_current_does_not_wait_for_battery_release(self):
        self.options['device_modes']['$ev'] = 'controlling'
        await self.controller.async_start()
        self.controller.before_external_command = Mock(return_value=False)
        self.slot['ev_target_current_a'] = 6
        await self.controller.async_tick()
        self.assertEqual(float(self.states['number.current'].state), 6)
        self.assertEqual(self.states['switch.charge'].state, 'on')
        self.controller.before_external_command.assert_not_called()

    async def test_stale_equality_does_not_skip_an_outstanding_lowering_command(self):
        self.options['device_modes']['$ev'] = 'controlling'
        self.states['switch.charge'].state = 'off'
        self.slot['ev_target_current_a'] = 0
        self.controller.native_executor.capacity = SimpleNamespace(
            requires_command=lambda entity,value:entity == 'switch.charge' and value == 'off',
            check_command=lambda command:None,mark_dispatch=lambda command:None)
        await self.controller.async_start()
        self.assertIn(('switch.charge','off'),self.calls)


class FinalBatteryCheckTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = battery.BatteryGatewayTests.asyncSetUp
    send = battery.BatteryGatewayTests.send
    step = battery.BatteryGatewayTests.step

    async def test_limit_recheck_after_durable_prepare_refuses_changed_budget(self):
        allowed = True
        def check(command):
            if not allowed:
                raise ControlDeadlineError('house load increased during prepare')
        self.physical.native_executor.capacity = SimpleNamespace(check_command=check, mark_dispatch=lambda command:None)
        prepare = self.journal.prepare_command
        def load_increases(command, effect=None):
            nonlocal allowed
            result = prepare(command, effect)
            allowed = False
            return result
        self.journal.prepare_command = load_increases
        self.assertEqual((await self.step())['status'], 'not_sent')
        self.assertFalse(self.calls)


class DurableDemandTests(unittest.TestCase):
    def setUp(self):
        self.capacity = empty_capacity()
        self.rows = {}

    def effect(self, after, control='on'):
        return dict(device='ev',logical='ev',before_w=11040,after_w=after,possible_w=after,
            controls={'switch.ev':control},changed='switch.ev',sensor='sensor.ev',signed=False,
            scale=1,overhead_w=0,response_tolerance_w=0)

    def report(self, ordinal, entity, value):
        self.rows[entity] = (ordinal, dict(entity_id=entity,state=value,
            attributes={'unit_of_measurement':'W','state_class':'measurement'},
            last_reported='2026-10-17T00:00:00Z'))
        observe_capacity(self.capacity,ordinal,lambda key:self.rows.get(key,(0,None)))

    def test_successful_reduction_waits_for_native_and_physical_response(self):
        prepare_capacity(self.capacity,'off',self.effect(0,'off'),10)
        dispatch_capacity(self.capacity,'off',10)
        finish_capacity(self.capacity,'off','service_returned')
        self.report(11,'switch.ev','off')
        self.assertEqual(capacity_bound(self.capacity['devices']['ev']),11040)
        self.report(12,'sensor.ev','0')
        self.assertEqual(capacity_bound(self.capacity['devices']['ev']),0)

    def test_control_acknowledgement_without_power_cannot_free_ev_capacity(self):
        effect=self.effect(0,'off');effect['sensor']=None
        prepare_capacity(self.capacity,'off',effect,10)
        dispatch_capacity(self.capacity,'off',10)
        finish_capacity(self.capacity,'off','service_returned')
        self.report(11,'switch.ev','off')
        self.assertEqual(capacity_bound(self.capacity['devices']['ev']),11040)

    def test_never_observed_uncertain_higher_write_survives_later_off_and_restart(self):
        prepare_capacity(self.capacity,'on',self.effect(11040),10)
        dispatch_capacity(self.capacity,'on',10)
        finish_capacity(self.capacity,'on','uncertain')
        prepare_capacity(self.capacity,'off',self.effect(0,'off'),20)
        dispatch_capacity(self.capacity,'off',20)
        finish_capacity(self.capacity,'off','service_returned')
        # Serializing the exact record is the restart path; no new evidence.
        import json
        self.capacity=json.loads(json.dumps(self.capacity))
        self.report(21,'switch.ev','off')
        self.report(22,'sensor.ev','0')
        self.assertEqual(capacity_bound(self.capacity['devices']['ev']),11040)

    def test_witnessed_uncertain_write_can_be_superseded_by_proven_lower_command(self):
        prepare_capacity(self.capacity,'on',self.effect(11040),10)
        dispatch_capacity(self.capacity,'on',10)
        finish_capacity(self.capacity,'on','uncertain')
        self.report(11,'switch.ev','on')
        self.report(12,'sensor.ev','10000')
        prepare_capacity(self.capacity,'off',self.effect(0,'off'),20)
        dispatch_capacity(self.capacity,'off',20)
        finish_capacity(self.capacity,'off','service_returned')
        self.report(21,'switch.ev','off')
        self.report(22,'sensor.ev','0')
        self.assertEqual(capacity_bound(self.capacity['devices']['ev']),0)


class RatingCorrectionTests(unittest.TestCase):
    def test_required_rating_and_house_meter_have_one_correction_path(self):
        from household_fixture import Rig
        from test_planning import device
        from shs_core.discovery import CatalogState, DiscoveryCatalog
        from shs_core.configuration_schema import save_device, prepare_options
        from shs_core.configuration_view import execution_device_views
        from shs_core.operating_modes import execution_mode_options
        from shs_core.grid_capacity import GridRatingError, HouseMeterError
        from shs_core.device_operations import correction_details
        key = 'water'
        model = device(key,'hot_water','permit_inhibit')
        states = {'switch.water':dict(state='off',attributes={}),
            'sensor.water':dict(state='0',attributes={'unit_of_measurement':'W','state_class':'measurement'}),
            'sensor.house':dict(state='1000',attributes={'unit_of_measurement':'W','state_class':'measurement'})}
        mapping = dict(control_type='permit_inhibit',actuator_entity_ids=['switch.water'],power='sensor.water')
        options = dict(battery_enabled=False,pool_enabled=False,ev_enabled=False,
            device_modes={key:'control_verification'},device_control_mappings={key:mapping})
        rig = Rig(options=options)
        rig.household.controller = SimpleNamespace(status={})
        catalog = DiscoveryCatalog({entity:CatalogState(entity,**row) for entity,row in states.items()}, {},59,18)
        names = {key:key for key in states}
        def view():
            return execution_device_views(catalog,dict(entity_names=names,area_names={},entity_areas={}),
                options,dict(devices=[model],home={}),rig.household,include_suggestions=False)[0]
        invalid = view()
        self.assertEqual(set(invalid['field_errors']),{'maximum_power_w','house_consumption_power_entity'})
        self.assertTrue(next(f for f in invalid['fields'] if f['key']=='maximum_power_w')['required'])
        with self.assertRaises(ValueError):
            execution_mode_options(options,[invalid],key,'controlling')
        rating_fix = correction_details(GridRatingError(key,mapping))['fix']['fields'][0]
        self.assertEqual((rating_fix['scope'],rating_fix['device_key'],rating_fix['key']),('mapping',key,'maximum_power_w'))
        house_fix = correction_details(HouseMeterError(options))['fix']['fields'][0]
        self.assertEqual((house_fix['scope'],house_fix['key']),('configuration','house_consumption_power_entity'))
        options = prepare_options(options,{'house_consumption_power_entity':'sensor.house'},states.get)
        options = save_device(options,key,{**mapping,'maximum_power_w':3000},model,states.get,
            entity_names=names,area_names={},entity_area_ids={})
        ready = view()
        self.assertEqual(ready['field_errors'],{})
        self.assertIsNone(ready['permission']['reason'])
        self.assertFalse(ready['permission']['enabled'])
        self.assertEqual(execution_mode_options(options,[ready],key,'controlling')['device_modes'][key],'controlling')


class DispatchFenceTests(unittest.TestCase):
    setUp = DurableDemandTests.setUp
    effect = DurableDemandTests.effect
    report = DurableDemandTests.report
    def test_reports_queued_during_prepare_cannot_confirm_a_later_dispatch(self):
        prepare_capacity(self.capacity,'off',self.effect(0,'off'),10)
        self.report(11,'switch.ev','off')
        self.report(12,'sensor.ev','0')
        dispatch_capacity(self.capacity,'off',12)
        finish_capacity(self.capacity,'off','service_returned')
        observe_capacity(self.capacity,13,lambda key:self.rows.get(key,(0,None)))
        self.assertEqual(capacity_bound(self.capacity['devices']['ev']),11040)
        self.report(14,'switch.ev','off')
        self.report(15,'sensor.ev','0')
        self.assertEqual(capacity_bound(self.capacity['devices']['ev']),0)

    def test_retained_older_uncertain_effect_cannot_replace_newer_confirmed_controls(self):
        prepare_capacity(self.capacity,'on',self.effect(11040),10)
        dispatch_capacity(self.capacity,'on',10)
        finish_capacity(self.capacity,'on','uncertain')
        prepare_capacity(self.capacity,'off',self.effect(0,'off'),20)
        dispatch_capacity(self.capacity,'off',20)
        finish_capacity(self.capacity,'off','service_returned')
        self.report(21,'switch.ev','off')
        self.report(22,'sensor.ev','0')
        self.assertEqual(commanded_controls(self.capacity,'ev'),{'switch.ev':'off'})
        self.assertEqual(capacity_bound(self.capacity['devices']['ev']),11040)
