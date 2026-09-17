"""Compiler-produced outlooks explain selected decisions without granting control."""
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import sys
from types import SimpleNamespace as NS
import unittest

sys.path.insert(0, str(Path(__file__).parents[1] / 'custom_components/shs_energy'))
from battery_execution_outlook import read_execution_outlook, describe_outlook
from battery_execution_policy import read_execution_policy, evaluate_policy, ExecutionConditions

FIXTURES = Path(__file__).parent / 'fixtures'


def fixture(dc=False):
    prefix = 'battery-execution-' + ('dc-' if dc else '')
    fixture = json.loads((FIXTURES / (prefix + 'outlook.json')).read_text())
    p = read_execution_policy(json.dumps(fixture['policy']))
    raw = fixture['outlook']
    s = p.summary
    conditions = ExecutionConditions(1, s.from_ms, s.until_ms, 5, 0, 2000, 2000, s.identity.context, s.permissions)
    decision = evaluate_policy(p, conditions, s.from_ms)
    session = NS(compiled=p, decision=decision, status='active', selected_id=decision.selected_id)
    return raw, read_execution_outlook(raw, p), NS(policy=session, conditions=conditions), s.from_ms


class OutlookTests(unittest.TestCase):
    def test_selected_incumbent_not_top_rank_controls_witness_and_benefit(self):
        for dc in (False, True):
            _, outlook, state, now = fixture(dc)
            rows = state.policy.decision.ranked
            hold = next(r for r in rows if r.operation.operation == 'hold')
            for row in rows:
                state.policy.selected_id = row.operation.id
                result = describe_outlook(outlook, state, now)
                self.assertEqual(result['selected_operation'], row.operation.id)
                self.assertAlmostEqual(result['benefit_vs_hold_sek'], hold.full[-1] - row.full[-1])
                if row.operation.operation == 'hold':
                    self.assertEqual(result['benefit_vs_hold_sek'], 0)
                self.assertEqual(result['battery_power_basis'], 'dc' if dc else 'ac')
            self.assertNotEqual(rows[0].operation.id, rows[-1].operation.id)

    def test_bridge_obeys_ac_and_dc_energy_balance_and_replaces_only_matching_direction(self):
        for dc in (False, True):
            _, outlook, state, now = fixture(dc)
            base = state.policy.decision.ranked[0]
            plant = state.policy.compiled.summary.plant
            for key, witness in outlook.witnesses.items():
                if witness.kind != 'anchor': continue
                for delta in (-.1, 0, .1, .0000001):
                    energy = witness.anchor_kwh - delta
                    if not plant.cutoff_kwh <= energy <= plant.capacity_kwh: continue
                    selected = replace(base, witness_id=key, energy_end_kwh=energy)
                    state.policy.decision = replace(state.policy.decision, ranked=(selected,))
                    state.policy.selected_id = selected.operation.id
                    result = describe_outlook(outlook, state, now)
                    bridge = [e for e in result['events'] if e['start_ms'] == outlook.bridge.start_ms]
                    if delta == 0:
                        self.assertEqual(bridge, [])
                    else:
                        self.assertEqual(len(bridge), 1)
                        event = bridge[0]
                        self.assertEqual(event['kind'], 'charge' if delta > 0 else 'discharge')
                        stored = event['battery_w'] * .25 / 1000
                        if not dc:
                            stored *= plant.charge_efficiency if delta > 0 else 1 / plant.discharge_efficiency
                        self.assertAlmostEqual(energy + (stored if delta > 0 else -stored), witness.anchor_kwh)
                        self.assertEqual(event['consumption_w'], outlook.bridge.consumption_w)
                        self.assertEqual(event['solar_w'], outlook.bridge.solar_w)
                    for kind, suffix in [('charge', witness.charge), ('discharge', witness.discharge)]:
                        if (kind == 'charge' and delta > 0) or (kind == 'discharge' and delta < 0): continue
                        events = [e for e in result['events'] if e['kind'] == kind]
                        self.assertEqual(len(events), int(suffix is not None))
                        if suffix:
                            self.assertEqual(events[0]['start_ms'], suffix.start_ms)
                            self.assertEqual(events[0]['battery_w'], suffix.battery_w)

    def test_idle_and_verification_are_explicit_and_have_no_invented_future_actions(self):
        _, outlook, state, now = fixture()
        row = replace(next(r for r in state.policy.decision.ranked if r.operation.operation != 'hold'), witness_id='idle')
        state.policy.decision = replace(state.policy.decision, ranked=(row,))
        state.policy.selected_id = row.operation.id
        state.policy.status = 'diagnostic_only'
        result = describe_outlook(outlook, state, now)
        self.assertEqual(result['events'], [])
        self.assertEqual(result['basis'], 'verification')
        self.assertIsNone(result['benefit_vs_hold_sek'])

    def test_no_explanation_for_missing_stale_or_unmatched_evidence(self):
        _, outlook, state, now = fixture()
        unavailable = {'state': 'unavailable'}
        self.assertEqual(describe_outlook(None, state, now), unavailable)
        self.assertEqual(describe_outlook(outlook, None, now), unavailable)
        self.assertEqual(describe_outlook(outlook, state, state.policy.compiled.summary.until_ms), unavailable)
        for status in ('awaiting_context', 'outside_coverage'):
            state.policy.status = status
            self.assertEqual(describe_outlook(outlook, state, now), unavailable)
        state.policy.status = 'active'
        self.assertEqual(describe_outlook(replace(outlook, source_hash='changed'), state, now), unavailable)
        state.conditions = replace(state.conditions, valid_until_ms=now + 1)
        self.assertEqual(describe_outlook(outlook, state, now + 1), unavailable)
        state.policy.selected_id = 'missing'
        self.assertEqual(describe_outlook(outlook, state, now), unavailable)

    def test_reader_rejects_wrong_policy_witnesses_and_malformed_events(self):
        raw, _, state, _ = fixture()
        anchor = next(key for key, w in raw['witnesses'].items() if w['kind'] == 'anchor')
        mutations = [
            lambda v: v.update(source_hash='wrong'), lambda v: v.update(family_id='wrong'),
            lambda v: v.update(battery_power_basis='dc'), lambda v: v.update(horizon_end_ms=float('nan')),
            lambda v: v['witnesses'].pop('idle'), lambda v: v.update(bridge=None),
            lambda v: v['bridge'].update(start_ms=v['bridge']['start_ms'] + 900000),
            lambda v: v['bridge'].update(consumption_w=float('inf')),
            lambda v: v['witnesses'][anchor].update(first_suffix_charge={**v['bridge'], 'battery_w': 10}),
            lambda v: v['witnesses'][anchor].update(anchor_kwh=-1),
            lambda v: v.update(unknown=True),
        ]
        for mutate in mutations:
            value = deepcopy(raw); mutate(value)
            with self.assertRaises(ValueError): read_execution_outlook(value, state.policy.compiled)

    def test_missing_hold_baseline_is_not_advertised_as_profit(self):
        _, outlook, state, now = fixture()
        rows = tuple(r for r in state.policy.decision.ranked if r.operation.operation != 'hold')
        state.policy.decision = replace(state.policy.decision, ranked=rows)
        state.policy.selected_id = rows[0].operation.id
        self.assertIsNone(describe_outlook(outlook, state, now)['benefit_vs_hold_sek'])
