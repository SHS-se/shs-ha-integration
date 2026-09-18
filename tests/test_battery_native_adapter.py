"""Synthetic transition records exercise routing; they do not commission hardware."""
from dataclasses import replace
import unittest
from test_home_runtime_execution import Harness
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
            self.adapter.propose(replace(self.effect, observation=altered, command_controls=altered.controls))
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
