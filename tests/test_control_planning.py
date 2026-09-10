"""Accepted definitions feed forecasts without enabling or touching actuators."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from zoneinfo import ZoneInfo
ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / 'custom_components/shs_energy'))
from control_planning import planning_controls, planning_options, pool_meter_models, observations
from control_setup import ControlSetup
from contract_v1.validate import revision
from test_control_setup import battery, pool
from test_control_agreement import Store
EXAMPLES = json.loads((ROOT / 'contracts/control/v1/examples.json').read_text())
NOW = datetime(2026, 9, 10, 12, tzinfo=timezone.utc)

class ControlPlanningTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.definition = deepcopy(EXAMPLES['battery-and-customer-pool'])
        inv, b = battery(); pinv, p = pool(); inv.update(pinv)
        b['control_id'], p['control_id'] = [c['control_id'] for c in self.definition['controls']]
        self.setup = ControlSetup(Store(), self.definition['home_id'], lambda: inv, lambda _: {})
        await self.setup.async_save(b, 0); await self.setup.async_save(p, 0)
        self.controls = self.definition['controls']
        self.accepted = {c['control_id']: revision(c) for c in self.controls}
        self.agreement = SimpleNamespace(saved={'definition': self.definition, 'accepted': self.accepted})

    async def test_options_resolve_current_registry_identity_and_keep_permissions_off(self):
        original = {'battery_control_enabled': False, 'pool_control_enabled': False, 'battery_capacity_kwh': 20, 'pool_volume_m3': 55}
        output = planning_options(original, self.setup, self.controls)
        self.assertEqual(output['battery_soc_entity'], 'sensor.soc')
        self.assertEqual(output['pool_water_temperature_entity'], 'sensor.water')
        self.assertTrue(output['battery_enabled'])
        self.assertFalse(output['battery_control_enabled'])
        self.assertFalse(output['pool_control_enabled'])
        self.assertNotIn('battery_enabled', original)
        record = self.setup.records[self.controls[0]['control_id']]
        record['binding_revision'] += 1
        with self.assertRaisesRegex(ValueError, 'binding changed'):
            planning_options(original, self.setup, self.controls)

    async def test_included_controls_wait_for_matching_acceptance(self):
        self.assertEqual(planning_controls(self.agreement)[0], self.controls)
        self.accepted.clear()
        with self.assertRaisesRegex(ValueError, 'await local acceptance'):
            planning_controls(self.agreement)
        for c in self.controls: c['desired']['included'] = False
        self.assertEqual(planning_controls(self.agreement)[0], self.controls)

    async def test_pool_history_keeps_meter_keys_and_excludes_no_extra_energy(self):
        devices = [{'key': 'original-' + str(i), 'name': 'Attributed meter', 'statistic_id': s['statistic_id'], 'category': 'other',
                    'suggested_load_type': 'fixed_full_load'} for i, s in enumerate(self.definition['sources']) if s.get('statistic_id')]
        keys = [d['key'] for d in devices if any(s['accounting_use'] == 'count' and self.definition['sources'][int(d['key'].split('-')[1])]['source_id'] == s['source_id'] for s in self.controls[1]['sources'])]
        actuals = [{'start': (NOW - timedelta(minutes=15*i)).isoformat(), 'device_energy_kwh': {k: .25 for k in keys}} for i in range(1, 193)]
        models, reserved = pool_meter_models(self.controls, self.definition, devices, actuals, [NOW], ZoneInfo('UTC'))
        self.assertEqual([m['key'] for m in models], keys)
        self.assertEqual(reserved, set(keys))
        self.assertTrue(all(m['active_power_w'] == 1000 for m in models))
        self.controls[1]['desired']['included'] = False
        models, reserved = pool_meter_models(self.controls, self.definition, devices, actuals, [NOW], ZoneInfo('UTC'))
        self.assertEqual(models, [])  # Returned to measured base load, not subtracted.
        self.assertEqual(reserved, set(keys))  # Also removed from legacy controlled models.
        self.controls[1]['desired']['included'] = True
        with self.assertRaisesRegex(ValueError, 'history'):
            pool_meter_models(self.controls, self.definition, devices, [], [NOW], ZoneInfo('UTC'))

    async def test_feedback_requires_its_own_fresh_timestamp_and_matching_control(self):
        feedback = deepcopy(EXAMPLES['customer-feedback'])
        feedback['accepted'] = revision(self.controls[1]); feedback['reported_at_utc'] = NOW.isoformat().replace('+00:00','Z')
        def read(entity):
            return {'state': '25', 'last_reported': NOW, 'attributes': {'feedback': feedback}}
        def value(): return observations(self.controls, self.definition, self.setup, read, NOW)[self.controls[1]['control_id']]['observations']['feedback']
        self.assertEqual(value()['value']['operation'], feedback['operation'])
        feedback['reported_at_utc'] = (NOW - timedelta(seconds=121)).isoformat().replace('+00:00','Z')
        self.assertIn('stale', value()['error'])
        feedback['reported_at_utc'] = NOW.isoformat().replace('+00:00','Z'); feedback['accepted']['desired'] += 1
        self.assertIn('older definition', value()['error'])

    async def test_unavailable_observations_are_explicit_and_never_success_claims(self):
        def read(_): raise ValueError('sensor unavailable')
        report = observations(self.controls, self.definition, self.setup, read, NOW)
        for c in report.values():
            self.assertTrue(all(v == {'error': 'sensor unavailable'} for v in c['observations'].values()))
            self.assertNotIn('control_enabled', c)
