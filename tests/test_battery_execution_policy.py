"""Executable policy properties and generated provider/consumer response parity."""
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import sys
import time
import unittest

sys.path.insert(0, str(Path(__file__).parents[1] / 'custom_components' / 'shs_energy'))
from battery_execution_policy import (read_execution_policy, evaluate_policy, evaluate_current,
    evaluate_continuation, ExecutionConditions, OutsideCoverage, COMPONENTS, MAX_POLICY_BYTES)

ROOT = Path(__file__).parents[1]


def policy_wire():
    identity = dict(policy_id='test-policy', revision=1, battery_id='battery', intent_revision='intent-1',
        plant_revision='plant-1', scope_revision='scope-1', external_scenario_revision='external-1',
        tariff_revision='tariff-1', response_model_revision='pv-first-v1', catalog_revision='catalog-1')
    return dict(schema='battery-execution-policy-v2', supply_scope={'kind':'whole_house'}, solar_attribution='proportional-self-consumed-pv-v1', profile='finite-continuation-v1', view='executable',
        identity=identity, actuals_origin_ms=0,
        validity=dict(from_ms=0, refresh_after_ms=840000, until_ms=900000, boundary_ms=900000),
        domain=dict(energy_kwh=[1,10],pv_w=[0,12000],residual_load_w=[0,15000]),
        plant=dict(cutoff_kwh=1,capacity_kwh=10,charge_max_w=4000,discharge_max_w=4000,
            charge_efficiency=.95,discharge_efficiency=.95,import_limit_w=17000,export_limit_w=13000,wear_basis="ac_throughput",wear_sek_per_kwh=.1),
        permissions=dict(available=True,grid_charge_allowed=True,battery_export_allowed=True,export_reserve_kwh=3,
            export_price_eligible=True,minimum_export_price_sek_per_kwh=.1,price_revision='tariff-1'),
        economics=dict(import_sek_per_kwh=.5,export_sek_per_kwh=.3,shaping_sek_per_kwh_per_kw=.02,ramp_sek_per_kw=.01),
        reference_id='hold', operations=[dict(id=op,operation=op,charge_limit_w=c,discharge_limit_w=d)
            for op,c,d in [('hold',0,0),('self_consumption',4000,4000),('solar_charge',4000,0),
                ('supply_house',0,4000),('grid_charge',3000,0),('export',0,2000)]],
        continuation=dict(representation='piecewise-quadratic-absolute-v1',coordinate_order=['energy_kwh','previous_import_w'],
            cells=[dict(id='terminal',witness_id='synthetic-no-terminal',domain=dict(energy_kwh=[1,10],previous_import_w=[0,17000],inequalities=[]),
                cost={key:dict(polynomial=[0,0,0],absolute_terms=[]) for key in ['import_sek','export_sek','wear_sek','shaping_sek','ramp_sek','terminal_sek']})]),
        quality=dict(assurance='exact-scoring-within-published-family',scorer_revision='offline-household-v2',compiler_revision='execution-v2',
            family_id='synthetic-test',source_hash='sha256:synthetic-test',numeric_tolerance_sek=1e-7,
            search_exhaustive_in_declared_graph=True,search_pruned_prefixes=0,heldout_count=0,heldout_max_regret_sek=None,certified_regret_bound_sek=None))


def policy(wire=None):
    return read_execution_policy(json.dumps(wire or policy_wire()).encode())


def conditions(p, *, now=1000, energy=5, pv=1000, load=2000, previous=1000, revision=1):
    return ExecutionConditions(revision, now, 900001, energy, pv, load, previous, p.summary.identity.context, p.summary.permissions)


class BatteryExecutionPolicyTests(unittest.TestCase):
    def test_discharge_storage_wear_matches_planner_basis(self):
        wire = policy_wire()
        wire['plant']['wear_basis'] = 'discharged_storage'
        p = policy(wire)
        for operation in p.summary.operations:
            result = evaluate_current(p, operation, conditions(p, now=0, pv=0, load=2500), 0)
            if isinstance(result, OutsideCoverage):
                continue
            components = dict(zip(COMPONENTS, result.current))
            self.assertAlmostEqual(components['wear_sek'],
                (5 - result.energy_end_kwh) * .1 if result.energy_end_kwh < 5 else 0)

    def test_live_time_energy_pv_and_load_reprice_without_recompile(self):
        p = policy()
        for now, energy, pv, load in [(1000,5,1000,2000),(180000,5.5,2500,1500),(720000,6,100,4000)]:
            decision = evaluate_policy(p, conditions(p,now=now,energy=energy,pv=pv,load=load), now)
            self.assertNotIsInstance(decision, OutsideCoverage)
            self.assertEqual(decision.valid_until_ms,900000)
            for row in decision.ranked:
                ref = next(r for r in decision.ranked if r.operation.id == decision.reference_id)
                for c,f,j,cr,jr in zip(row.current,row.future_delta,row.full,ref.current,ref.full):
                    self.assertAlmostEqual(c-cr+f,j-jr,places=7)
        self.assertTrue(evaluate_policy(p,conditions(p,now=850000),850000).refresh_due)
        self.assertEqual(evaluate_policy(p,conditions(p),900000).reason,'expired')

    def test_negative_price_uses_pv_first_forced_charge(self):
        wire=policy_wire(); wire['economics'].update(import_sek_per_kwh=-1,export_sek_per_kwh=0)
        p=policy(wire); decision=evaluate_policy(p,conditions(p,pv=1000,load=1000),1000)
        self.assertEqual(decision.selected_id,'grid_charge')
        row=next(r for r in decision.ranked if r.operation.id=='grid_charge')
        self.assertEqual(row.possible_import_w,3000)
        self.assertEqual(row.possible_export_w,0)
        self.assertLess(row.current[0],0)

    def test_saturation_scores_positive_duration_segments_and_internal_ramp(self):
        p=policy(); c=conditions(p,now=0,energy=9.9,pv=0,load=1000,previous=1000)
        op=next(o for o in p.summary.operations if o.id=='grid_charge')
        result=evaluate_current(p,op,c,0)
        active=.1/(3*.95)
        self.assertAlmostEqual(result.energy_end_kwh,10)
        self.assertEqual(result.terminal_import_w,1000)
        costs=dict(zip(COMPONENTS,result.current))
        self.assertAlmostEqual(costs['import_sek'],(4*active+1*(.25-active))*.5)
        self.assertAlmostEqual(costs['shaping_sek'],.5*.02*(16*active+1*(.25-active)))
        self.assertAlmostEqual(costs['ramp_sek'],.06)
        at_boundary=conditions(p,now=0,energy=10-3*.95*.25,pv=0,load=1000,previous=1000)
        boundary=evaluate_current(p,op,at_boundary,0)
        self.assertEqual(boundary.terminal_import_w,4000)
        self.assertAlmostEqual(dict(zip(COMPONENTS,boundary.current))['ramp_sek'],.03)

    def test_export_reserve_is_not_an_invented_native_stop(self):
        p=policy(); c=conditions(p,now=0,energy=3.1,pv=0,load=500)
        export=next(o for o in p.summary.operations if o.id=='export')
        self.assertEqual(evaluate_current(p,export,c,0).reason,'export_reserve_crossing')
        house=next(o for o in p.summary.operations if o.id=='supply_house')
        self.assertLess(evaluate_current(p,house,c,0).energy_end_kwh,3)
        for change in [dict(battery_export_allowed=False),dict(export_price_eligible=False),dict(minimum_export_price_sek_per_kwh=1)]:
            wire=policy_wire(); wire['permissions'].update(change); denied=policy(wire)
            self.assertIn(evaluate_current(denied,export,conditions(denied),1000).reason,('battery_export_not_allowed','export_price_ineligible'))

    def test_native_autonomous_modes_do_not_mint_grid_contributions(self):
        p=policy()
        for op in p.summary.operations:
            c=conditions(p,pv=3000,load=1000)
            result=evaluate_current(p,op,c,1000)
            if op.operation in ('self_consumption','solar_charge','supply_house','hold'):
                self.assertEqual((result.possible_import_w,result.possible_export_w),(0,0))
        solar=next(o for o in p.summary.operations if o.id=='solar_charge')
        result=evaluate_current(p,solar,conditions(p,pv=0,load=2000),1000)
        self.assertEqual(result.energy_end_kwh,5)

    def test_continuation_ramp_depends_on_terminal_import_and_terminal_value_counts_once(self):
        wire=policy_wire(); cost=wire['continuation']['cells'][0]['cost']
        cost['terminal_sek']['polynomial']=[0,1,0]
        cost['ramp_sek']['absolute_terms']=[dict(weight=.001,energy=100,previous_import=-1,constant=200)]
        p=policy(wire)
        _, first=evaluate_continuation(p,5,700); _, second=evaluate_continuation(p,5,1700)
        self.assertAlmostEqual(first[-1],-5)
        self.assertAlmostEqual(second[-1],-4)

    def test_domain_holes_and_hypothetical_or_stale_evidence_are_explicit(self):
        p=policy()
        self.assertEqual(evaluate_policy(p,conditions(p,pv=12001),1000).reason,'conditions_outside_domain')
        self.assertEqual(evaluate_policy(p,replace(conditions(p),valid_until_ms=1001),1001).reason,'stale_conditions')
        self.assertEqual(evaluate_policy(p,replace(conditions(p),identity=replace(p.summary.identity.context,scope_revision='other')),1000).reason,'context_mismatch')
        wire=policy_wire(); wire['continuation']['cells'][0]['domain']['energy_kwh']=[1,2]
        p=policy(wire); self.assertEqual(evaluate_policy(p,conditions(p),1000).reason,'reference_uncovered')
        wire['view']='conditional'
        with self.assertRaises(ValueError): policy(wire)

    def test_generated_provider_current_response_parity(self):
        for filename in ('battery-execution-dc-current-vectors.json', 'battery-execution-current-vectors.json', 'battery-execution-native-permissions-current-vectors.json'):
            corpus = json.loads((ROOT / 'tests/fixtures' / filename).read_text())
            p = read_execution_policy(json.dumps(corpus['policy']).encode())
            for case in corpus['cases']:
                with self.subTest(corpus=filename, case=case['id']):
                    now = case['at_ms']
                    c = ExecutionConditions(1, now, max(now+1,p.summary.until_ms+1), case['energy_kwh'], case['pv_w'],
                        case['residual_load_w'], case['previous_import_w'], p.summary.identity.context, p.summary.permissions)
                    op = next(o for o in p.summary.operations if o.id == case['operation_id'])
                    actual = evaluate_current(p,op,c,now)
                    expected = case['expected']
                    if not expected['eligible']:
                        self.assertIsInstance(actual,OutsideCoverage)
                        self.assertEqual(actual.reason,expected['reason'])
                    else:
                        self.assertNotIsInstance(actual,OutsideCoverage)
                        for key,value in zip(COMPONENTS,actual.current):
                            self.assertAlmostEqual(value,expected['current'][key],places=7)
                        for key in ('energy_end_kwh','terminal_import_w','possible_import_w','possible_export_w'):
                            self.assertAlmostEqual(getattr(actual,key),expected[key],places=7)

    def test_closed_reader_rejects_forgery_limits_and_unknown_native_models(self):
        mutations=[lambda w:w.update(extra=1),lambda w:w['identity'].update(response_model_revision='Grid First'),
            lambda w:w['operations'][0].update(operation='Command Charging (Grid First)'),
            lambda w:w['operations'][1].update(charge_limit_w=5000),
            lambda w:w['quality'].update(certified_regret_bound_sek=.02),
            lambda w:w['quality'].update(heldout_max_regret_sek=0),
            lambda w:w['validity'].update(until_ms=900001),
            lambda w:w['permissions'].update(grid_charge_allowed=1),
            lambda w:w['continuation']['cells'][0]['cost']['wear_sek'].update(polynomial=[0,-1,0])]
        for mutate in mutations:
            wire=policy_wire(); mutate(wire)
            with self.assertRaises(ValueError): policy(wire)
        with self.assertRaises(ValueError): read_execution_policy(b'{"schema":1,"schema":2}')
        with self.assertRaises(ValueError): read_execution_policy(b'x'*(MAX_POLICY_BYTES+1))

    def test_generated_provider_continuation_parity_and_low_state_coverage(self):
        for filename in ('battery-execution-continuation-vectors.json','battery-execution-dc-continuation-vectors.json'):
            corpus = json.loads((ROOT / 'tests/fixtures' / filename).read_text())
            p = read_execution_policy(json.dumps(corpus['policy']).encode())
            for case in corpus['cases']:
                with self.subTest(case=case['id']):
                    actual = evaluate_continuation(p, case['energy_kwh'], case['previous_import_w'])
                    expected = case['expected']
                    if expected is None:
                        self.assertIsNone(actual)
                    else:
                        self.assertEqual(actual[0], expected['witness_id'])
                        for key, value in zip(COMPONENTS, actual[1]):
                            self.assertAlmostEqual(value, expected['objective'][key], places=7)
            s = p.summary
            c = ExecutionConditions(1, s.from_ms, s.until_ms, s.plant.cutoff_kwh+.01, 3000, 1000, 0,
                                    s.identity.context, s.permissions)
            self.assertNotIsInstance(evaluate_policy(p, c, s.from_ms), OutsideCoverage)

    def test_declared_maximum_cells_evaluate_with_bounded_work(self):
        wire=policy_wire(); cell=wire['continuation']['cells'][0]
        wire['continuation']['cells']=[{**deepcopy(cell),'id':str(i)} for i in range(64)]
        p=policy(wire); started=time.monotonic()
        for _ in range(100): self.assertNotIsInstance(evaluate_policy(p,conditions(p),1000),OutsideCoverage)
        self.assertLess(time.monotonic()-started,5)
        wire['continuation']['cells'].append({**cell,'id':'65'})
        with self.assertRaises(ValueError): policy(wire)


if __name__=='__main__': unittest.main()

class ScopedPolicyTests(unittest.TestCase):
    def test_scoped_native_response_is_rejected_instead_of_model_only_clipping(self):
        wire=policy_wire()
        wire['supply_scope']={'kind':'selected','include_base':True,'planned_device_keys':[]}
        p=policy(wire)
        c=replace(conditions(p,load=3000,pv=1000),eligible_load_w=1000)
        supplied=evaluate_current(p,next(o for o in p.summary.operations if o.id=='supply_house'),c,1000)
        self.assertIsInstance(supplied,OutsideCoverage)
        self.assertEqual(supplied.reason,'native_operation_exceeds_supply_scope')
        exported=evaluate_current(p,next(o for o in p.summary.operations if o.id=='export'),c,1000)
        self.assertIsInstance(exported,OutsideCoverage)
        self.assertEqual(exported.reason,'native_operation_exceeds_supply_scope')
        wire['operations'].append(dict(id='scoped-500',operation='supply_house',charge_limit_w=0,discharge_limit_w=500))
        p=policy(wire)
        supplied=evaluate_current(p,next(o for o in p.summary.operations if o.id=='scoped-500'),c,1000)
        self.assertNotIsInstance(supplied,OutsideCoverage)
        self.assertAlmostEqual(supplied.terminal_import_w,1500)
        absent=replace(c,eligible_load_w=None)
        self.assertEqual(evaluate_policy(p,absent,1000).reason,'reference_uncovered')

    def test_scope_is_permission_economics_can_charge_or_preserve(self):
        wire=policy_wire()
        wire['economics'].update(import_sek_per_kwh=2.77,export_sek_per_kwh=0,ramp_sek_per_kw=0,shaping_sek_per_kwh_per_kw=0)
        p=policy(wire)
        c=conditions(p,load=1200,pv=0,energy=9)
        self.assertEqual(evaluate_policy(p,c,1000).selected_id,'self_consumption')
        # A high future value can make preservation win at the same load/price.
        wire['continuation']['cells'][0]['cost']['terminal_sek']['polynomial']=[0,4,0]
        wire['permissions']['grid_charge_allowed']=False
        wire['permissions']['battery_export_allowed']=False
        p=policy(wire);c=conditions(p,load=1200,pv=0,energy=9)
        decision=evaluate_policy(p,c,1000)
        selected=next(r for r in decision.ranked if r.operation.id==decision.selected_id)
        self.assertEqual(selected.operation.discharge_limit_w,0)
