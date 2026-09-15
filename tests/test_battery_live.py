"""Live capture preserves source evidence without commissioning hardware."""
import asyncio
from copy import deepcopy
from pathlib import Path
import sys
import unittest
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).parents[1] / 'custom_components/shs_energy'))
from battery_live import BatteryLiveInputs, MeasurementProfile, capture_battery_inputs, source_revision
from battery_supply import SupplyScope


def fixture():
    options = dict(house_consumption_power_entity='sensor.house', solar_production_power_entity='sensor.pv',
        battery_power_measurement_entity='sensor.battery', battery_soc_entity='sensor.soc',
        battery_mode_entity='select.mode', battery_charge_limit_entity='number.charge',
        battery_discharge_limit_entity='number.discharge', device_modes={'ev': 'control_verification'},
        device_control_mappings={'ev': {'power': 'sensor.ev'}})
    devices = [dict(key='ev', planning_role='controllable', planning_choice_at='role-1')]
    stamp = '1970-01-01T00:00:01+00:00'
    reports = {entity: dict(state=str(value), attributes=dict(unit_of_measurement='kW', state_class='measurement'), last_reported=stamp)
               for entity, value in [('sensor.house',3), ('sensor.pv',1), ('sensor.battery',-2), ('sensor.ev',2)]}
    reports['sensor.soc'] = dict(state='50', attributes=dict(unit_of_measurement='%'), last_reported=stamp)
    reports['select.mode'] = dict(state='Standby', attributes=dict(options=['Standby', 'Charge']), last_reported=stamp)
    for entity in ('number.charge', 'number.discharge'):
        reports[entity] = dict(state='8.8', attributes=dict(unit_of_measurement='kW', min=0,max=100,step=.001),last_reported=stamp)
    return options, devices, reports


def profile(options, devices):
    return MeasurementProfile.read(dict(schema='battery-measurements-v1', revision='synthetic-ac-evidence',
        source_revision=source_revision(options, devices), boundary='house-ac', evidence_reference='synthetic-test-only',
        max_age_ms=4000, max_alignment_ms=100, source_entities=['sensor.ev','sensor.house','sensor.pv']))


class CaptureTests(unittest.TestCase):
    def setUp(self):
        self.options, self.devices, self.reports = fixture()

    def capture(self, **changes):
        args = dict(now_ms=2000)
        args.update(changes)
        return capture_battery_inputs(self.options, self.devices, self.reports.get, **args)

    def test_unit_conversion_preserves_unverified_native_boundary(self):
        result = self.capture()
        self.assertEqual(result['sources']['battery_power_measurement_entity']['watts'], -2000)
        self.assertEqual(result['native_surface']['limits']['charge']['quantum_w'], 1)
        self.assertEqual(result['native_surface']['readback']['charge_limit_w'], 8800)
        self.assertEqual(result['native_surface']['power_basis'], 'unverified')
        self.assertIsNone(result['accounting'])
        self.assertFalse(result['control_authority'])
        self.assertIn('ac_boundary_unverified', [b['reason'] for b in result['blockers']])

    def test_stale_zero_remains_stale_without_borrowing_house_report_time(self):
        self.reports['sensor.pv']['state'] = '0'
        self.reports['sensor.pv']['last_reported'] = '1970-01-01T00:00:00+00:00'
        self.reports['sensor.house']['last_reported'] = '1970-01-01T00:01:00+00:00'
        result = self.capture(now_ms=60000)
        self.assertEqual(result['sources']['solar_production_power_entity']['state'], 'stale_report')
        self.assertEqual(result['sources']['house_consumption_power_entity']['state'], 'reported')
        self.reports['sensor.pv']['last_reported'] = self.reports['sensor.house']['last_reported']
        self.assertEqual(self.capture(now_ms=60000)['sources']['solar_production_power_entity']['state'], 'reported')

    def test_proportional_scope_includes_verification_and_invalidates_role_changes(self):
        reviewed = profile(self.options, self.devices)
        result = self.capture(profile=reviewed, scope=SupplyScope('selected', True))
        self.assertAlmostEqual(result['accounting']['house_supply_bound_w'], 2000/3)
        self.assertFalse(result['control_authority'])
        self.devices[0]['planning_role'] = 'base_load'
        result = self.capture(profile=reviewed, scope=SupplyScope('selected', True))
        self.assertIsNone(result['accounting'])
        self.assertIn('measurement_profile_configuration_changed', [b['reason'] for b in result['blockers']])

    def test_excluded_device_is_never_read_or_exported(self):
        self.options['excluded_device_readings'] = ['ev']
        seen = []
        def read(entity):
            seen.append(entity)
            return self.reports.get(entity)
        value = capture_battery_inputs(self.options, self.devices, read, now_ms=2000)
        self.assertNotIn('sensor.ev', seen)
        self.assertNotIn('planned:ev', value['sources'])
        self.assertEqual(len(seen),len(set(seen)))

    def test_duplicate_meter_and_missing_planned_power_do_not_create_base_load(self):
        for entity in ['sensor.house', None]:
            self.options['device_control_mappings']['ev']['power'] = entity
            value = self.capture(profile=profile(self.options,self.devices),scope=SupplyScope('selected',True))
            self.assertIsNone(value['accounting'])

    def test_interval_energy_and_bad_native_quantization_are_not_accepted(self):
        self.reports['sensor.house']['attributes']['unit_of_measurement'] = 'kWh'
        self.reports['number.charge']['state'] = '8.8005'
        result = self.capture()
        self.assertEqual(result['sources']['house_consumption_power_entity']['state'], 'unavailable')
        self.assertFalse(result['native_surface']['response_commissioned'])
        self.assertEqual(result['native_surface']['state'], 'unavailable')


class CaptureOwnerTests(unittest.IsolatedAsyncioTestCase):
    async def test_sampling_does_not_send_and_serializes_diagnostic_saves(self):
        options,devices,reports = fixture()
        entered,release = asyncio.Event(),asyncio.Event()
        saved=[]
        async def persist(value):
            entered.set()
            await release.wait()
            saved.append(deepcopy(value))
        now=[2000]
        owner=BatteryLiveInputs(reports.get,lambda:now[0],persist)
        first=asyncio.create_task(owner.sample(options,devices))
        await entered.wait()
        now[0]=62000
        second=asyncio.create_task(owner.sample(options,devices))
        release.set()
        await asyncio.gather(first,second)
        self.assertEqual([v['at_ms'] for v in saved],[2000,62000])
        now[0]=100000
        self.assertTrue(owner.snapshot()['capture_stale'])
        self.assertFalse(owner.snapshot()['control_authority'])

    async def test_shutdown_and_failed_persistence_cannot_revive_capture(self):
        options,devices,reports=fixture()
        owner=None
        async def persist(value):
            owner.close()
            raise OSError('disk full')
        owner=BatteryLiveInputs(reports.get,lambda:2000,persist)
        await owner.sample(options,devices)
        owner.unavailable('late store failure')
        self.assertEqual(owner.snapshot(),dict(state='stopped',control_authority=False))
        persist=AsyncMock(side_effect=OSError('disk full'))
        owner=BatteryLiveInputs(reports.get,lambda:2000,persist)
        await owner.sample(options,devices)
        self.assertEqual(owner.snapshot()['persistence_error'],'OSError')
        self.assertFalse(owner.snapshot()['control_authority'])


class CoordinatorMembershipTests(unittest.IsolatedAsyncioTestCase):
    async def test_planned_device_without_valid_controls_remains_in_capture(self):
        import ast
        from types import SimpleNamespace
        root = Path(__file__).parents[1] / 'custom_components/shs_energy'
        tree = ast.parse((root/'coordinator.py').read_text())
        cls = next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='ShsStatusCoordinator')
        method = next(n for n in cls.body if isinstance(n,ast.AsyncFunctionDef) and n.name=='async_battery_planned_devices')
        ns={}
        exec(compile(ast.Module(body=[method],type_ignores=[]),'coordinator.py','exec'),ns)
        cached={'optimisation_device_configuration':{
            'broken':dict(planning_role='controllable',control_type='unsupported'),
            'ev':dict(planning_role='controllable'),
            'monitored':dict(planning_role='base_load'),
        }}
        coordinator=SimpleNamespace(_store=SimpleNamespace(async_load=AsyncMock(return_value=cached)))
        devices=await ns['async_battery_planned_devices'](coordinator)
        self.assertEqual([d['key'] for d in devices],['broken','ev'])
        options,_,reports=fixture()
        result=capture_battery_inputs(options,devices,reports.get,now_ms=2000)
        self.assertEqual(result['sources']['planned:broken']['reason'],'source_not_configured')
        coordinator._store.async_load=AsyncMock(return_value={})
        with self.assertRaisesRegex(ValueError,'not been acknowledged'):
            await ns['async_battery_planned_devices'](coordinator)
