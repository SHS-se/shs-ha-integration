"""Synthetic transition records exercise routing; they do not commission hardware."""
from dataclasses import replace
import unittest
from test_home_runtime_policy import Harness
from home_runtime import NeedTransition, Step, Guard
from battery_native_adapter import CommissionedAdapter


class NativeAdapterTests(unittest.TestCase):
    def setUp(self):
        self.h = Harness()
        self.effect = next(e for e in self.h.offer() if isinstance(e, NeedTransition))
        controls = self.effect.observation.controls
        self.steps = []
        for key, value in self.effect.request.target:
            if dict(controls)[key] != value:
                step = Step(key, value, controls, (Guard('ready', 1, 1),),
                            self.h.group.spec.maximum, 100, 200, True, 'fixture-only')
                self.steps.append(step)
                controls = step.after
        self.adapter = CommissionedAdapter(self.h.authority.catalog, 'fixture-only',
            tuple(self.steps), self.h.authority.catalog.control_surface_revision, False)

    def test_only_recorded_transitions_reach_the_exact_scored_target(self):
        proposed = self.adapter.propose(self.effect)
        self.assertEqual(proposed.steps, tuple(self.steps))
        self.assertEqual(dict(proposed.steps[-1].after), dict(self.effect.request.target))

    def test_unobserved_route_and_changed_surface_are_rejected(self):
        altered = replace(self.effect.observation, controls=(('mode', 'Standby'), ('charge', 100), ('discharge', 0)))
        with self.assertRaisesRegex(ValueError, 'no commissioned transition'):
            self.adapter.propose(replace(self.effect, observation=altered))
        with self.assertRaisesRegex(ValueError, 'identity changed'):
            self.adapter.propose(replace(self.effect, adapter_revision='changed'))
        with self.assertRaisesRegex(ValueError, 'matching physical'):
            replace(self.adapter, observed_surface_revision='changed')

    def test_duplicate_or_unquantized_transitions_are_not_capabilities(self):
        with self.assertRaises(ValueError):
            replace(self.adapter, steps=(*self.adapter.steps, self.adapter.steps[0]))
        bad = replace(self.steps[-1], value=100000)
        with self.assertRaises(ValueError):
            replace(self.adapter, steps=(*self.steps[:-1], bad))

class NativeReadbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_explicit_poll_publishes_unchanged_registers_but_rejects_missing_values(self):
        from types import SimpleNamespace, ModuleType
        from unittest.mock import patch
        from battery_sigen import refresh_sigen_readback
        keys=('plant_remote_ems_control_mode','plant_ess_max_charging_limit','plant_ess_max_discharging_limit')
        called=[]
        async def refresh():called.append('poll')
        coordinator=SimpleNamespace(data={'plant':dict(zip(keys,(4,0,4)))},last_update_success=True,async_refresh=refresh)
        entities={key:SimpleNamespace(coordinator=coordinator,entity_description=SimpleNamespace(key=key),available=True,
                    async_write_ha_state=lambda key=key:called.append(key)) for key in keys}
        module=ModuleType('homeassistant.helpers.entity_platform')
        module.async_get_platforms=lambda hass,domain:[SimpleNamespace(entities=entities)] if domain=='sigen' else []
        with patch.dict('sys.modules',{'homeassistant.helpers.entity_platform':module}):
            await refresh_sigen_readback(None,keys)
            self.assertEqual(called,['poll',*keys])
            called.clear();coordinator.data['plant'].pop(keys[1])
            with self.assertRaisesRegex(ValueError,'incomplete'):await refresh_sigen_readback(None,keys)
            self.assertEqual(called,['poll'])
