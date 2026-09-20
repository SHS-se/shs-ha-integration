"""The card's fields come from the contract, never from the device's identity."""

from __future__ import annotations

from pathlib import Path
import sys
import unittest

sys.path.append(str(Path(__file__).parents[1] / "custom_components" / "shs_energy"))

from configuration_fields import (  # noqa: E402
    CONTROL_FIELDS,
    OBSERVATION_FIELDS,
    control_fields,
    local_contract,
    mapping_keys,
)
from configuration_schema import MAPPING_KEYS  # noqa: E402
from device_commands import execution_setup_errors  # noqa: E402

PATHS = (None, "room", "pool", "boiler", "ev")


def keys(control_type, path, category=""):
    return [field["key"] for field in control_fields(control_type, path, category)]


class PoolContractTests(unittest.TestCase):
    def test_the_pool_card_is_the_same_whatever_method_the_website_shows(self):
        """A pool heater is driven as one relay however it was configured.

        This used to be three separate mechanisms: a short-circuit on
        ``system == "pool"`` in the field list, a rewrite of ``control_type``
        inside ``mapping_report``, and a routing rule that quietly stopped
        returning "pool" the moment the method changed — which is why choosing
        a power control moved the card's header to "No room".
        """
        expected = ["actuator_entity_ids", "power", "power_setting_entity_id"]
        for method in CONTROL_FIELDS:
            self.assertEqual(keys(method, "pool"), expected, method)

    def test_the_pool_only_offers_what_its_executor_can_command(self):
        actuator = next(
            field for field in control_fields("switch_schedule", "pool")
            if field["key"] == "actuator_entity_ids"
        )
        self.assertEqual(actuator["domains"], ["switch", "input_boolean"])
        # execute_pool has never honoured these, so the card must not offer them.
        for absent in ("minimum_on_seconds", "minimum_off_seconds",
                       "control_override_entity"):
            self.assertNotIn(absent, keys("switch_schedule", "pool"))

    def test_the_power_setting_is_observed_and_belongs_only_to_the_pool(self):
        self.assertEqual(
            [field["key"] for field in OBSERVATION_FIELDS["pool"]],
            ["power_setting_entity_id"],
        )
        for method in CONTROL_FIELDS:
            for path in PATHS:
                if path == "pool":
                    continue
                self.assertNotIn(
                    "power_setting_entity_id", keys(method, path),
                    f"{method} on {path} was offered the pool's observation",
                )

    def test_nothing_writes_the_power_setting(self):
        """It is an observation, so no executor may treat it as a target.

        `actuator_targets` is what the controller writes to; the setting must
        never appear there, whatever the mapping says.
        """
        from device_commands import actuator_targets
        mapping = {
            "control_type": "switch_schedule",
            "actuator_entity_ids": ["switch.pool"],
            "power_setting_entity_id": "number.desired_charge_power_pool_1",
        }
        self.assertEqual(actuator_targets(mapping), ["switch.pool"])

    def test_a_variable_power_pool_can_be_saved(self):
        """The literal failure: "Pool heater: this method is not supported"."""
        mapping = {
            "control_type": "variable_power",
            "actuator_entity_ids": ["switch.pool"],
            "power": 8000,
        }
        self.assertEqual(
            execution_setup_errors(mapping),
            ["this method is not supported for this device"],
            "the bare label is still refused, which is correct",
        )
        self.assertEqual(
            execution_setup_errors(mapping, local_contract("variable_power", "pool")),
            [],
            "the contract the executor actually runs must be accepted",
        )

    def test_local_contract_leaves_every_other_pairing_alone(self):
        for method in CONTROL_FIELDS:
            for path in (None, "room", "boiler", "ev"):
                self.assertEqual(local_contract(method, path), method)


class RoutingTests(unittest.TestCase):
    """`planning_path` is the single authority; the contract follows it."""

    def test_a_pool_heater_with_a_power_dial_is_still_the_pool(self):
        from device_controls import planning_path
        self.assertEqual(planning_path("variable_power", "pool_heating"), "pool")
        self.assertEqual(planning_path("switch_schedule", "pool_heating"), "pool")

    def test_the_room_rule_still_wins_for_a_pool_room_floor_heater(self):
        """A category must never imply a control contract.

        A floor heater metered as `pool_heating` but asked to hold a setpoint
        belongs to its room's heat model. Assuming otherwise once turned a
        single bad pairing into a whole-plan failure.
        """
        from device_controls import planning_path
        self.assertEqual(planning_path("setpoint", "pool_heating"), "room")
        self.assertEqual(planning_path("switch_schedule", "heating"), "room")

    def test_no_other_meter_gains_a_pool_route(self):
        from device_controls import planning_path
        for category in ("ev_charging", "hot_water", "property_energy", None):
            self.assertNotEqual(
                planning_path("variable_power", category), "pool", category,
            )

    def test_choosing_a_power_control_for_the_pool_saves(self):
        """The exact failure, end to end along the chain it travels.

        Website method -> planning_path -> local contract -> executor admission.
        Before the routing row existed this returned None at step two, so the
        device stopped being a pool device mid-edit and the executor was asked
        about a method it has never run.
        """
        from device_controls import planning_path
        mapping = {
            "control_type": "variable_power",
            "actuator_entity_ids": ["switch.pool_heater"],
            "power": 8000,
            "power_setting_entity_id": "number.desired_charge_power_pool_1",
        }
        path = planning_path("variable_power", "pool_heating")
        self.assertEqual(path, "pool")
        self.assertEqual(
            execution_setup_errors(mapping, local_contract("variable_power", path)),
            [],
        )
        self.assertEqual(
            keys("variable_power", path),
            ["actuator_entity_ids", "power", "power_setting_entity_id"],
        )


class DriftTests(unittest.TestCase):
    """The invariant the grounding found nothing enforcing."""

    def test_every_field_a_card_shows_can_be_saved(self):
        for method in CONTROL_FIELDS:
            persistable = MAPPING_KEYS[method]
            for path in PATHS:
                for key in keys(method, path, "heating"):
                    self.assertIn(
                        key, persistable,
                        f"{method} on {path} renders {key}, which save rejects",
                    )

    def test_every_entity_a_card_shows_is_existence_checked(self):
        """Rendered, saved and checked must be the same set.

        A field can otherwise be offered and persisted while `mapping_report`
        never notices its entity was deleted, which is how the render list and
        the entity list drifted apart in the first place.
        """
        from device_controls import _ENTITY_FIELDS_BY_CONTROL_TYPE
        from configuration_fields import local_contract
        for method in CONTROL_FIELDS:
            for path in PATHS:
                contract = local_contract(method, path)
                checked = set(_ENTITY_FIELDS_BY_CONTROL_TYPE.get(contract, ()))
                for field in control_fields(method, path, "heating"):
                    if field["kind"] not in ("entity", "entities"):
                        continue
                    if field["key"] in ("power", "control_override_entity"):
                        continue  # checked separately, by their own owners
                    self.assertIn(
                        field["key"], checked,
                        f"{method} on {path} renders {field['key']}, "
                        "whose entity is never existence-checked",
                    )

    def test_mapping_keys_covers_every_path(self):
        for method in CONTROL_FIELDS:
            self.assertLessEqual(
                set(keys(method, "pool")) | set(keys(method, None)),
                mapping_keys(method),
            )


class RegressionTests(unittest.TestCase):
    """Non-pool cards must be byte-identical to before the contract existed."""

    def test_room_and_ev_contracts_are_unchanged(self):
        self.assertEqual(
            keys("switch_schedule", "room", "heating"),
            ["actuator_entity_ids", "power", "temperature_entity_id",
             "control_override_entity", "minimum_on_seconds", "minimum_off_seconds"],
        )
        self.assertEqual(
            keys("variable_power", "ev"),
            ["control_entity_id", "power", "minimum_value", "maximum_value"],
        )
        self.assertEqual(
            keys("permit_inhibit", "boiler"),
            ["actuator_entity_ids", "power", "max_inhibit_slots",
             "control_override_entity"],
        )

    def test_an_unrouted_device_keeps_its_plain_contract(self):
        self.assertEqual(
            keys("switch_schedule", None),
            [field["key"] for field in CONTROL_FIELDS["switch_schedule"]],
        )


if __name__ == "__main__":
    unittest.main()
