"""Persist sensor choices, resolve live equipment limits, and discover by name."""
import ast
from copy import deepcopy
from math import isfinite
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

ROOT = Path(__file__).parents[1] / 'custom_components/shs_energy'
sys.path.insert(0, str(ROOT))
import const
from configuration_schema import prepare_options, resolve_configuration
from configuration_values import resolve_battery_quantities
from migration import migrate_options

SOURCES = {
    'battery_capacity_kwh': ('sensor.sigen_plant_rated_energy_capacity', '18.08', 'kWh'),
    'battery_charge_max_w': ('sensor.sigen_plant_ess_rated_charging_power', '8.8', 'kW'),
    'battery_discharge_max_w': ('sensor.sigen_plant_ess_rated_discharging_power', '9600', 'W'),
    'battery_min_soc': ('sensor.sigen_plant_discharge_cut_off_soc', '5.0', '%'),
}

class BatteryQuantityTests(unittest.TestCase):
    def setUp(self):
        self.entities = {entity: {'state': state, 'attributes': {'unit_of_measurement': unit}}
                         for entity, state, unit in SOURCES.values()}
        self.options = prepare_options({}, {key: value[0] for key, value in SOURCES.items()}, self.entities.get)

    def resolve(self):
        return resolve_battery_quantities(resolve_configuration(self.options), self.entities.get)

    def test_sensor_ids_survive_save_and_new_readings_reach_planning_units(self):
        self.assertEqual(self.options, {key: value[0] for key, value in SOURCES.items()})
        self.assertEqual(self.resolve(), dict(battery_capacity_kwh=18.08, battery_charge_max_w=8800,
                                            battery_discharge_max_w=9600, battery_min_soc=.05))
        self.entities[SOURCES['battery_min_soc'][0]]['state'] = '0.5'
        self.entities[SOURCES['battery_charge_max_w'][0]]['state'] = '7.2'
        self.assertEqual(self.resolve()['battery_min_soc'], .005)  # 0.5%, never guessed as 50%.
        self.assertEqual(self.resolve()['battery_charge_max_w'], 7200)

    def test_literal_values_work_without_any_sensor(self):
        values = dict(battery_capacity_kwh=18.08, battery_charge_max_w=8800,
                      battery_discharge_max_w=9600, battery_min_soc=.05)
        saved = prepare_options({}, values, lambda _: None)
        self.assertEqual(resolve_battery_quantities(resolve_configuration(saved), lambda _: None), values)

    def test_missing_unavailable_wrong_units_and_invalid_limits_fail(self):
        entity = SOURCES['battery_charge_max_w'][0]
        for reading in (None, {'state': 'unavailable', 'attributes': {'unit_of_measurement': 'kW'}},
                        {'state': '8.8', 'attributes': {'unit_of_measurement': 'kWh'}},
                        {'state': '-1', 'attributes': {'unit_of_measurement': 'kW'}},
                        {'state': 'nan', 'attributes': {'unit_of_measurement': 'kW'}}):
            with self.subTest(reading=reading):
                self.entities[entity] = reading
                with self.assertRaises(ValueError):
                    self.resolve()
                with self.assertRaises(ValueError):
                    prepare_options({}, {'battery_charge_max_w': entity}, self.entities.get)

    def test_energy_conversion_and_soc_ceiling(self):
        self.entities[SOURCES['battery_capacity_kwh'][0]] = {
            'state': '18080', 'attributes': {'unit_of_measurement': 'Wh'}}
        self.assertAlmostEqual(self.resolve()['battery_capacity_kwh'], 18.08)
        self.entities[SOURCES['battery_min_soc'][0]]['state'] = '100'
        with self.assertRaisesRegex(ValueError, 'below 100%'):
            self.resolve()
        with self.assertRaisesRegex(ValueError, 'below 100%'):
            prepare_options(self.options, {'battery_min_soc': SOURCES['battery_min_soc'][0]}, self.entities.get)

    def test_cutoff_migrates_once_into_single_field(self):
        old = {'battery_min_soc': .05, 'battery_min_soc_entity': SOURCES['battery_min_soc'][0]}
        before = deepcopy(old)
        saved, _ = migrate_options(old, source_version=8)
        self.assertEqual(saved['battery_min_soc'], SOURCES['battery_min_soc'][0])
        self.assertNotIn('battery_min_soc_entity', saved)
        self.assertEqual(old, before)
        self.assertEqual(migrate_options(saved, source_version=9)[0], saved)


class BatteryDiscoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_discovery_keeps_sensor_ids_and_matches_friendly_names(self):
        # Exercise the actual HA adapter with only its IO dependencies injected.
        tree = ast.parse((ROOT / 'configuration.py').read_text())
        names = {'async_discover_configuration', '_entity_id', '_first_state', '_state_text',
                 '_number', '_attribute_number', '_as_kwh', '_as_watts'}
        nodes = [ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0)]
        nodes += [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in names]
        ns = {k: getattr(const, k) for k in dir(const) if k.startswith('OPT_')}
        ns.update(CONFIGURABLE_CATEGORIES=const.CONFIGURABLE_CATEGORIES, isfinite=isfinite, resolved_options=lambda hass, existing: resolve_configuration(existing),
                  async_get_manager=AsyncMock(return_value=SimpleNamespace(data={'energy_sources': []})),
                  _energy_dashboard_inventory=lambda *_: [])
        exec(compile(ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[])), 'configuration.py', 'exec'), ns)
        for renamed in (False, True):
            with self.subTest(renamed=renamed):
                states = [SimpleNamespace(entity_id=('sensor.renamed_' + key if renamed else entity), state=value,
                          attributes={'friendly_name': entity.replace('_', ' '), 'unit_of_measurement': unit})
                          for key, (entity, value, unit) in SOURCES.items()]
                hass = SimpleNamespace(states=SimpleNamespace(async_all=lambda: states))
                result = await ns['async_discover_configuration'](hass, {})
                for key, state in zip(SOURCES, states):
                    self.assertEqual(result['configuration'][key], state.entity_id)
