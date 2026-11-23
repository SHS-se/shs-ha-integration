"""Synthetic accepted instructions survive source outages without new store measurements."""

from copy import deepcopy
import unittest

import test_controller as fixtures
from test_controller import State
from shs_core.service_continuity import retained_service_instruction
from shs_core.app_projection import schedule
from shs_core.device_port import device_intent
from shs_core.verification import VerificationJournal


class ServiceContinuityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        fixtures.ControllerTests.setUp(self)
        self.controller.verification = VerificationJournal(fixtures.Store(), fixtures.Store())
        self.plan = self.coordinator.optimisation_plan
        for model in self.plan["device_models"]:
            model["planning_role"] = "controllable"
            if model["key"] == "pool":
                model["planning_service"] = "pool"
        self.slot["device_commands"] = {
            "charger": {"type": "variable_power", "value": 10, "unit": "A"},
            "pool": {"type": "switch_schedule", "on_seconds": 900},
        }

    def unavailable(self, device):
        self.plan["capabilities"][device] = False
        self.plan["measurement_issues"] = [{"device": device, "field": "soc",
                                            "value": "unavailable", "reason": "Synthetic source outage"}]

    async def test_ev_retains_request_without_a_battery_projection_and_resumes_when_readable(self):
        self.options["device_modes"]["$ev"] = "controlling"
        self.unavailable("ev")
        self.plan["ev_battery"] = None
        self.states["sensor.ev_soc"] = State("unavailable", unit_of_measurement="%")
        await self.controller.async_start()
        self.assertEqual(self.calls, [])
        self.states["sensor.ev_soc"] = State(30, unit_of_measurement="%")
        await self.controller.async_tick()
        self.assertEqual(self.states["switch.charge"].state, "on")
        self.assertEqual(float(self.states["number.current"].state), 10)
        self.assertIsNone(self.plan["ev_battery"])

    async def test_uncovered_ev_interval_holds_without_sending_off(self):
        self.options["device_modes"]["$ev"] = "controlling"
        await self.controller.async_start()
        self.calls.clear()
        self.unavailable("ev")
        self.slot["ev_target_current_a"] = 0
        self.slot["device_commands"]["charger"] = {"type": "unavailable", "reason": "No accepted interval"}
        await self.controller.async_tick()
        self.assertEqual(self.calls, [])
        self.assertEqual(self.states["switch.charge"].state, "on")
        self.assertIn("holding", self.controller.status["ev"]["reason"])

    async def test_pool_retains_accepted_cutoff_without_a_water_projection(self):
        self.options["device_modes"]["$pool"] = "controlling"
        self.unavailable("pool")
        self.plan["pool"] = None
        self.plan["retained_pool_stop_temperature_c"] = 30
        self.slot.update(pool_permitted=True, pool_command_w=3300)
        self.states["sensor.water"] = State("unavailable", unit_of_measurement="°C")
        await self.controller.async_start()
        self.assertEqual(self.calls, [])
        self.states["sensor.water"] = State(29, unit_of_measurement="°C")
        await self.controller.async_tick()
        self.assertEqual(self.states["switch.pool"].state, "on")
        self.calls.clear()
        self.slot["pool_permitted"] = None
        self.slot.pop("pool_command_w")
        await self.controller.async_tick()
        self.assertEqual(self.calls, [])
        self.assertEqual(self.states["switch.pool"].state, "on")

    async def test_verification_and_explicit_disable_never_gain_control_from_retention(self):
        self.unavailable("ev")
        self.options["device_modes"]["$ev"] = "control_verification"
        await self.controller.async_start()
        self.assertEqual(self.calls, [])
        self.options["ev_enabled"] = False
        self.options["device_modes"]["$ev"] = "controlling"
        await self.controller.async_tick()
        self.assertEqual(self.calls, [])

    def test_invalid_readings_or_inconsistent_commands_cannot_enable_retention(self):
        self.unavailable("ev")
        self.assertTrue(retained_service_instruction(self.plan, self.slot, "ev"))
        invalid = deepcopy(self.plan)
        invalid["measurement_issues"][0]["value"] = 1.5
        self.assertFalse(retained_service_instruction(invalid, self.slot, "ev"))
        self.slot["device_commands"]["charger"]["value"] = 9
        self.assertFalse(retained_service_instruction(self.plan, self.slot, "ev"))

    def test_chart_projection_preserves_instruction_and_pool_owner_identity(self):
        self.slot["pool_permitted"] = True
        projected = schedule({**self.plan, "plans": {"priority": {"slots": [self.slot]}}},
                             {"state": "ready"})
        self.assertTrue(projected["household"][0]["pool_permitted"])
        self.assertEqual(next(device for device in projected["devices"] if device["key"] == "pool")
                         ["planning_service"], "pool")

    def test_native_pool_intent_preserves_cutoff_without_a_store_projection(self):
        self.unavailable("pool")
        self.plan["pool"] = None
        self.plan["retained_pool_stop_temperature_c"] = 30
        self.slot.update(pool_permitted=True, pool_command_w=3300)
        intent = device_intent("pool", "apply", self.plan, self.slot,
                               configuration_revision=1, policy_revision=1)
        self.assertEqual(intent["parameters"]["stop_temperature_c"], 30)
        self.assertEqual(intent["parameters"]["heating_w"], 3300)
        self.assertIsNone(self.plan["pool"])
