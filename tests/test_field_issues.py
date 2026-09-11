"""Corrections carry source-owned destinations, not guesses from message text."""
import ast
from pathlib import Path
from types import SimpleNamespace
import sys
import unittest

ROOT = Path(__file__).parents[1] / 'custom_components/shs_energy'
sys.path.insert(0, str(ROOT))
from optimisation import OptimisationInputError, REMEDY_WAITING
from device_controls import battery_control_errors, pool_band_errors, mapping_report, apply_requested_configuration


def price_catalog(coordinator):
    cls = next(n for n in ast.parse((ROOT / 'coordinator.py').read_text()).body
               if isinstance(n, ast.ClassDef) and n.name == 'ShsStatusCoordinator')
    method = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == '_supplier_price_catalog')
    namespace = {'OptimisationInputError': OptimisationInputError, 'REMEDY_WAITING': REMEDY_WAITING}
    exec(compile(ast.Module(body=[method], type_ignores=[]), 'coordinator.py', 'exec'), namespace)
    return namespace['_supplier_price_catalog'](coordinator)


class FieldIssueTests(unittest.TestCase):
    def test_download_failure_does_not_accuse_supplier_configuration(self):
        for error in [None, 'subscription_inactive', 'connection refused']:
            with self.subTest(error=error), self.assertRaises(OptimisationInputError) as raised:
                price_catalog(SimpleNamespace(supplier_prices=None, last_price_error=error))
            self.assertEqual(raised.exception.remedy, REMEDY_WAITING)
            self.assertEqual(raised.exception.fix, {'kind': 'none'})
            self.assertNotIn('must be configured', str(raised.exception))

    def test_explicit_missing_supplier_configuration_routes_to_website(self):
        with self.assertRaises(OptimisationInputError) as raised:
            price_catalog(SimpleNamespace(supplier_prices={'configuration': None}))
        self.assertEqual(raised.exception.fix, {'kind': 'website', 'path': '/portal/settings/energy-tariffs'})

    def test_renewed_catalog_proceeds_without_a_configuration_warning(self):
        catalog = {'configuration': {'price_area': 'SE3'}}
        self.assertIs(price_catalog(SimpleNamespace(supplier_prices=catalog)), catalog)

    def test_pool_highlights_the_missing_stop_only(self):
        fields = {}
        errors = pool_band_errors({'pool_enabled': True, 'pool_start_temperature_entity': 'number.start'}, field_errors=fields)
        self.assertEqual(list(fields), ['pool_stop_temperature_entity'])
        self.assertEqual(fields['pool_stop_temperature_entity'], errors)

    def test_battery_reports_keys_for_each_missing_control(self):
        fields = {}
        battery_control_errors({'battery_control_enabled': True, 'battery_enabled': True}, field_errors=fields)
        self.assertIn('battery_mode_entity', fields)
        self.assertIn('battery_charge_limit_entity', fields)
        self.assertNotIn('pv_forecast_latitude', fields)

    def test_unmapped_device_identifies_its_actual_required_actuator(self):
        report = mapping_report('switch_schedule', None)
        self.assertEqual(set(report['field_errors']), {'actuator_entity_ids'})

    def test_missing_entity_marks_only_the_reference_that_is_broken(self):
        report = mapping_report('switch_schedule', {'control_type': 'switch_schedule', 'actuator_entity_ids': ['switch.gone'], 'power': 'sensor.power'}, {'sensor.power'})
        self.assertEqual(set(report['field_errors']), {'actuator_entity_ids'})
        self.assertIn('switch.gone', report['field_errors']['actuator_entity_ids'][0])
        self.assertNotIn('switch.gone', report['mapping_error'], 'website summary stays privacy-minimised')

    def test_local_field_metadata_is_not_added_to_website_device_contract(self):
        device = {'key': 'sensor.heater', 'category': 'heating', 'suggested_load_type': 'fixed_full_load'}
        result = apply_requested_configuration([device], {'sensor.heater': {'planning_role': 'controllable', 'control_type': 'switch_schedule'}}, {})
        self.assertNotIn('field_errors', result[0])
