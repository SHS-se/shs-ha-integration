"""The commissioned EV range stays ready when entity bounds change."""
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest

sys.path.append(str(Path(__file__).parents[1] / 'custom_components/shs_energy'))
from household_fixture import Rig
from shs_core.configuration_schema import save_device
from shs_core.configuration_view import execution_device_views
from shs_core.discovery import CatalogState, DiscoveryCatalog
from shs_core.operating_modes import execution_mode_options
from shs_core.planning import build_services
from shs_core.presentation import device_readiness
from test_planning import HORIZON, device


class EvConfiguredRangeTests(unittest.TestCase):
    def test_save_readiness_permission_and_planning_use_the_same_configured_range(self):
        key = 'sensor.ev_energy'
        model = device(key, 'ev_charging', 'variable_power')
        mapping = dict(control_type='variable_power', control_entity_id='number.current',
                       minimum_value=5, maximum_value=16)
        states = {
            'number.current': dict(state='5', attributes=dict(min=0, max=5, step=1, unit_of_measurement='A')),
            'switch.charge': dict(state='on', attributes={}),
            'binary_sensor.connected': dict(state='on', attributes={}),
            'sensor.soc': dict(state='50', attributes={'unit_of_measurement': '%'}),
            'number.target': dict(state='80', attributes={'unit_of_measurement': '%'}),
            'sensor.remaining': dict(state='37.5', attributes={}),
        }
        options = dict(device_modes={'$ev': 'control_verification'},
            ev_charge_switch_entity='switch.charge', ev_connected_entity='binary_sensor.connected',
            ev_soc_entity='sensor.soc', ev_target_soc_entity='number.target',
            ev_energy_remaining_entity='sensor.remaining', battery_enabled=False, pool_enabled=False)
        names = {entity: entity for entity in states}
        options = save_device(options, key, mapping, model, states.get,
            entity_names=names, area_names={}, entity_area_ids={})
        rig = Rig(options=options)
        rig.household.controller = SimpleNamespace(status={})
        catalog = DiscoveryCatalog({entity: CatalogState(entity, **state)
            for entity, state in states.items()}, {}, 59, 18)
        choices = dict(devices=[model], home={}, refreshed_at=rig.now.isoformat())
        views = execution_device_views(catalog,
            dict(entity_names=names, area_names={}, entity_areas={}),
            options, choices, rig.household, include_suggestions=False)
        ev = next(row for row in views if row['key'] == key)
        self.assertEqual(ev['mapping_status'], 'ready')
        self.assertEqual(ev['field_errors'], {})
        self.assertIsNone(ev['permission']['reason'])
        self.assertEqual(device_readiness(views)['ready_devices'], 1)
        self.assertFalse(ev['permission']['enabled'])
        granted = execution_mode_options(options, views, key, 'controlling')
        self.assertEqual(granted['device_modes']['$ev'], 'controlling')
        self.assertEqual(options['device_modes']['$ev'], 'control_verification')

        for reported_maximum in (5, 16, 5):
            states['number.current']['attributes']['max'] = reported_maximum
            services, _, _ = build_services(options, HORIZON, [model],
                read_entity=states.get, local_tz=rig.home.timezone)
            self.assertEqual(services[0]['control']['min_current_a'], 5)
            self.assertEqual(services[0]['control']['max_current_a'], 16)
            rig.household.optimisation_missing_inputs = []
            rig.household._sync_optimisation_issue()
            self.assertFalse(rig.household.attention_items)

    def test_setup_banner_does_not_describe_invalid_values_as_missing(self):
        rig = Rig()
        rig.household.optimisation_missing_inputs = ['A configured value needs correction']
        rig.household._sync_optimisation_issue()
        warning = rig.household.attention_items[0]
        self.assertEqual(warning['title'], 'Planning inputs need attention')
