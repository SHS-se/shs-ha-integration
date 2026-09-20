"""Native permission validation must not confuse a forecast with a ceiling."""
from copy import deepcopy
from pathlib import Path
import sys
import unittest

sys.path.append(str(Path(__file__).parents[1] / 'custom_components' / 'shs_energy'))
from battery_commands import validate_battery_command, battery_mode_key


class BatteryCommandTests(unittest.TestCase):
    def setUp(self):
        self.slot = {'battery_charge_w': 0, 'battery_discharge_w': 405.6,
                     'battery_command': {'schema_version': 2, 'operation': 'supply_house',
                                         'charge_limit_w': 0, 'discharge_limit_w': 9600,
                                         'allow_grid_charge': False, 'allow_battery_export': False}}

    def test_supply_permission_can_exceed_forecast_without_enabling_export(self):
        self.assertEqual(validate_battery_command(self.slot), self.slot['battery_command'])
        self.slot['battery_command']['discharge_limit_w'] = 405.6
        self.assertEqual(validate_battery_command(self.slot)['discharge_limit_w'], 405.6)

    def test_permission_does_not_relax_direction_or_forecast_bounds(self):
        for change in ({'charge_limit_w': 100}, {'allow_battery_export': True},
                       {'allow_grid_charge': True}, {'discharge_limit_w': 400}):
            with self.subTest(change=change):
                slot = deepcopy(self.slot)
                slot['battery_command'].update(change)
                with self.assertRaises(ValueError):
                    validate_battery_command(slot)

    def test_forced_operations_still_require_the_allocated_power(self):
        for operation, charge, discharge in (('export', 0, 9600), ('grid_charge', 8800, 0)):
            with self.subTest(operation=operation):
                slot = deepcopy(self.slot)
                slot.update(battery_charge_w=405.6 if charge else 0,
                            battery_discharge_w=405.6 if discharge else 0)
                slot['battery_command'].update(operation=operation, charge_limit_w=charge,
                    discharge_limit_w=discharge, allow_grid_charge=bool(charge),
                    allow_battery_export=bool(discharge))
                with self.assertRaisesRegex(ValueError, 'ceilings disagree'):
                    validate_battery_command(slot)


class SchemaThreeTests(unittest.TestCase):
    """Schema 3 separates "do not spend stored energy" from "let surplus go to the grid"."""

    def slot(self, operation, charge, discharge, *, charge_w=0, discharge_w=0, schema=3):
        return {'battery_charge_w': charge_w, 'battery_discharge_w': discharge_w,
                'battery_command': {'schema_version': schema, 'operation': operation,
                                    'charge_limit_w': charge, 'discharge_limit_w': discharge,
                                    'allow_grid_charge': operation == 'grid_charge',
                                    'allow_battery_export': operation == 'export'}}

    def test_retaining_stored_energy_keeps_the_rated_charge_permission(self):
        for operation, discharge, discharge_w in (('hold', 0, 0), ('supply_house', 9600, 405.6)):
            with self.subTest(operation=operation):
                command = validate_battery_command(
                    self.slot(operation, 8800, discharge, discharge_w=discharge_w))
                self.assertEqual(command['charge_limit_w'], 8800)
                self.assertEqual(battery_mode_key(command), 'battery_mode_baseline')

    def test_only_idle_closes_the_charge_permission(self):
        command = validate_battery_command(self.slot('idle', 0, 0))
        self.assertEqual(battery_mode_key(command), 'battery_mode_idle')
        for change in ({'charge_limit_w': 8800}, {'discharge_limit_w': 9600}):
            with self.subTest(change=change):
                slot = self.slot('idle', 0, 0)
                slot['battery_command'].update(change)
                with self.assertRaisesRegex(ValueError, 'zero'):
                    validate_battery_command(slot)

    def test_closing_the_charge_permission_needs_a_positive_ceiling_elsewhere(self):
        for operation, discharge in (('hold', 0), ('supply_house', 9600)):
            with self.subTest(operation=operation):
                with self.assertRaisesRegex(ValueError, 'positive charge ceiling'):
                    validate_battery_command(self.slot(operation, 0, discharge, discharge_w=0))

    def test_schema_two_keeps_its_own_meaning_of_hold(self):
        command = validate_battery_command(self.slot('hold', 0, 0, schema=2))
        self.assertEqual(battery_mode_key(command), 'battery_mode_idle')
        with self.assertRaisesRegex(ValueError, 'zero charge ceiling'):
            validate_battery_command(self.slot('hold', 8800, 0, schema=2))
        with self.assertRaisesRegex(ValueError, 'unsupported battery operation'):
            validate_battery_command(self.slot('idle', 0, 0, schema=2))

    def test_unknown_command_schema_is_refused(self):
        with self.assertRaisesRegex(ValueError, 'versioned battery operation'):
            validate_battery_command(self.slot('hold', 8800, 0, schema=4))
