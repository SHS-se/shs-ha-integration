"""Independently synthetic conformance for the shared installation physics."""
from copy import deepcopy
import json
from math import ceil
from pathlib import Path
import sys
import unittest

sys.path.append(str(Path(__file__).parents[1] / 'custom_components' / 'shs_energy'))
from shs_core.battery_conversion import Conversion
from shs_core.battery_runtime import plan_contract
from shs_core.plan_execution import (Account, LiveState, StateObservation,
    admit_plan, assess_execution, balance, contract_wire, read_contract, record_meter,
    request_replan)

VECTORS = json.loads((Path(__file__).parents[1] / 'contracts' / 'battery-physics-v1.json').read_text())
BATTERY = VECTORS['battery']
CONVERSION = Conversion.read(BATTERY['conversion'])
QUARTER = 900_000


def wire():
    return {
        'schema': 'battery-plan-execution-v3', 'id': 'synthetic-reference',
        'plan_id': 'synthetic-plan', 'generation': 0, 'mode': 'controlling',
        'scope_revision': 'synthetic-base-only', 'model_revision': 'synthetic-refined-v1',
        'energy_basis': 'stored_energy_mwh', 'capacity_mwh': 10_000_000,
        'minimum_mwh': 1_000_000, 'maximum_mwh': 9_000_000,
        'export_reserve_mwh': 1_000_000, 'valid_until_ms': QUARTER,
        'source_receipt': 0, 'previous_contract_id': None,
        'storage': {'charge_efficiency': .9, 'discharge_efficiency': .8},
        'intervals': [{
            'start_ms': 0, 'end_ms': QUARTER, 'stored_start_mwh': 5_000_000,
            'stored_end_mwh': 5_000_000, 'target_kind': 'permission',
            'instruction': {'kind': 'idle'}, 'charge_ac_mwh': 0,
            'discharge_ac_mwh': 0, 'pv_mwh': 0, 'load_mwh': 125_000,
            'installation_mwh': 15_000, 'import_mwh': 140_000,
            'export_mwh': 0, 'curtailed_mwh': 0, 'unserved_mwh': 0,
            'rounding_mwh': 0,
        }],
        'objectives': [], 'recovery': [], 'dispositions': [],
        'demand': {'loads': [], 'profiles': [{'id': 'base', 'include_base': True, 'eligible_keys': []}]},
    }


def account(contract):
    value = Account()
    for direction in ('charge', 'discharge'):
        value = record_meter(value, event_id='synthetic-' + direction, stream=direction,
            direction=direction, boundary='battery_dc', epoch='synthetic-meter',
            source_at_ms=0, total_mwh=0)
    return admit_plan(value, contract, 0, StateObservation(0, 5_000_000, 'synthetic-SOC'))


class RefinedConformanceTests(unittest.TestCase):
    def test_forward_curves_and_storage_match_shared_hand_calculated_vectors(self):
        for row in VECTORS['forward']:
            with self.subTest(row['name']):
                charge_dc, discharge_dc = row['charge_dc_w'], row['discharge_dc_w']
                solar, grid = CONVERSION.charge_inputs(charge_dc, row['pv_w'], row['house_w'])
                charge, discharge = solar + grid, CONVERSION.discharge.output(discharge_dc)
                net = CONVERSION.net_grid(charge_dc, discharge_dc, row['pv_w'], row['house_w'])
                actual = {
                    'charge_w': charge, 'discharge_w': discharge,
                    'battery_idle_w': net - row['house_w'] + row['pv_w'] - charge + discharge,
                    'net_w': net,
                    'battery_kwh': row['stored_kwh'] + row['hours'] / 1000 * (
                        charge_dc * BATTERY['charge_efficiency']
                        - discharge_dc / BATTERY['discharge_efficiency']),
                }
                for key, expected in row['expected'].items():
                    self.assertAlmostEqual(actual[key], expected, places=9)

    def test_source_specific_charge_inverse_matches_shared_vectors(self):
        for row in VECTORS['charge_inverse']:
            with self.subTest(row['name']):
                dc = CONVERSION.charge_dc(row['ac_w'], row['pv_w'], row['house_w'])
                self.assertAlmostEqual(dc, row['expected_dc_w'], places=9)
                self.assertLessEqual(sum(CONVERSION.charge_inputs(dc, row['pv_w'], row['house_w'])), row['ac_w'] + 1e-9)

    def test_v3_explicit_installation_term_conserves_and_round_trips(self):
        value = wire()
        result = read_contract(value)
        self.assertEqual(result.intervals[0].installation_mwh, 15_000)
        self.assertEqual(contract_wire(result), value)
        invalid = deepcopy(value)
        invalid['intervals'][0]['installation_mwh'] = 0
        with self.assertRaisesRegex(ValueError, 'conserve'):
            read_contract(invalid)
        invalid = deepcopy(value)
        del invalid['intervals'][0]['installation_mwh']
        with self.assertRaisesRegex(ValueError, 'missing'):
            read_contract(invalid)
        invalid = deepcopy(value)
        del invalid['storage']
        with self.assertRaisesRegex(ValueError, 'missing'):
            read_contract(invalid)

    def test_new_plan_requires_full_physics_and_v3_without_legacy_inference(self):
        plan = {'schema_version': 11, 'plan_id': 'synthetic-plan',
                'battery': BATTERY, 'battery_execution': wire()}
        self.assertEqual(plan_contract(plan).wire_schema, 'battery-plan-execution-v3')
        missing = deepcopy(plan)
        del missing['battery']['conversion']
        with self.assertRaisesRegex(ValueError, 'conversion'):
            plan_contract(missing)
        old = deepcopy(plan)
        old['battery_execution']['schema'] = 'battery-plan-execution-v2'
        with self.assertRaisesRegex(ValueError, 'v3'):
            plan_contract(old)

    def test_stored_charge_target_timing_applies_storage_ratio_once(self):
        value = wire()
        value['intervals'][0].update(
            stored_end_mwh=5_180_000, target_kind='stored_energy',
            instruction={'kind': 'grid_charge', 'charge_ac_w': 1100},
            charge_ac_mwh=275_000, installation_mwh=0, import_mwh=400_000)
        value['objectives'] = [{'id': 'synthetic-charge', 'kind': 'stored_energy',
            'start_ms': 0, 'deadline_ms': QUARTER, 'target_mwh': 5_090_000,
            'reason': 'Synthetic 90 Wh stored target'}]
        live = LiveState(0, 5_000_000, 500, 0, 500, 4000, 5000, 10_000, 10_000)
        result = assess_execution(account(read_contract(value)), live, CONVERSION)
        self.assertEqual(result.charge_dc_w, 800)
        self.assertEqual(result.valid_until_ms, 450_000)

    def test_discharge_reserve_timing_uses_terminal_over_stored_ratio(self):
        value = wire()
        value['intervals'][0].update(
            stored_end_mwh=4_760_417, target_kind='demand_following',
            instruction={'kind': 'supply', 'profile_id': 'base',
                'charge_ac_limit_w': 0, 'discharge_ac_limit_w': 5000,
                'response': {'mode': 'follow_demand'}},
            discharge_ac_mwh=125_000, installation_mwh=0, import_mwh=0)
        live = LiveState(0, 5_000_000, 500, 0, 500, 4000, 5000, 10_000, 10_000,
            minimum_mwh=4_800_000)
        result = assess_execution(account(read_contract(value)), live, CONVERSION)
        self.assertEqual(result.discharge_dc_w, 766)
        self.assertEqual(result.valid_until_ms, ceil(200_000 * .8 * 3600 / 766))

    def test_measured_dc_account_uses_each_captured_storage_basis_and_late_corrections(self):
        first = wire()
        value = account(read_contract(first))
        def meter(value, at, total):
            return record_meter(value, event_id=f'synthetic-charge-{at}-{total}',
                stream='charge', direction='charge', boundary='battery_dc',
                epoch='synthetic-meter', source_at_ms=at, total_mwh=total)
        value = meter(value, QUARTER, 1_000_000)
        value = request_replan(value)
        second = wire()
        second.update(id='second-reference', plan_id='second-plan', generation=1,
            source_receipt=value.requests[-1].source_receipt,
            previous_contract_id=first['id'], valid_until_ms=2 * QUARTER,
            storage={'charge_efficiency': .8, 'discharge_efficiency': .75})
        second['intervals'][0].update(start_ms=QUARTER, end_ms=2 * QUARTER,
            stored_start_mwh=5_900_000, stored_end_mwh=5_900_000)
        value = admit_plan(value, read_contract(second), QUARTER,
                           StateObservation(QUARTER, 5_900_000, 'synthetic-SOC'))
        value = meter(value, 2 * QUARTER, 2_000_000)
        value = record_meter(value, event_id='synthetic-discharge-end', stream='discharge',
            direction='discharge', boundary='battery_dc', epoch='synthetic-meter',
            source_at_ms=2 * QUARTER, total_mwh=0)
        result = balance(value, 2 * QUARTER)
        # First 1 kWh terminal stores .9 kWh; second stores .8 kWh.
        self.assertEqual(result.charge.low, 2_000_000)
        self.assertEqual(result.flow_debt_low_mwh, -800_000)
        self.assertEqual(result.flow_debt_high_mwh, -800_000)
        self.assertTrue(value.__dict__['_stored_segments'])
        value = meter(value, QUARTER, 1_100_000)
        result = balance(value, 2 * QUARTER)
        # The corrected split is 1.1*.9 + .9*.8 = 1.71 stored kWh.
        self.assertEqual(result.flow_debt_low_mwh, -810_000)
        self.assertEqual(result.flow_debt_high_mwh, -810_000)
