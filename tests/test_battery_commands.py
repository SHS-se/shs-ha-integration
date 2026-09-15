"""Native permission validation must not confuse a forecast with a ceiling."""
from copy import deepcopy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).parents[1] / 'custom_components' / 'shs_energy'))
from battery_commands import validate_battery_command


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
