"""The plug-in time only moves when the cable really goes in."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import unittest

sys.path.append(str(Path(__file__).parents[1] / 'custom_components/shs_energy'))
from shs_core.car_plug import observe_plug

NOW = datetime(2026, 10, 28, 17, 0, tzinfo=timezone.utc)
CABLE = 'binary_sensor.cable'


class ObservePlugTests(unittest.TestCase):
    def test_first_sight_of_a_connected_car_uses_home_assistants_change_time(self):
        record = observe_plug(None, CABLE, True, NOW - timedelta(hours=3), NOW)
        self.assertEqual(record['plugged_in_at'], (NOW - timedelta(hours=3)).isoformat())

    def test_a_missing_or_future_change_time_falls_back_to_now(self):
        self.assertEqual(observe_plug(None, CABLE, True, None, NOW)['plugged_in_at'], NOW.isoformat())
        self.assertEqual(observe_plug(None, CABLE, True, NOW + timedelta(hours=1), NOW)['plugged_in_at'], NOW.isoformat())

    def test_a_restart_that_resets_the_change_time_keeps_the_plug_in_time(self):
        record = observe_plug(None, CABLE, True, NOW - timedelta(hours=3), NOW)
        later = NOW + timedelta(hours=1)
        self.assertEqual(observe_plug(record, CABLE, True, later, later), record)

    def test_unplugging_clears_it_and_the_next_plug_in_starts_again(self):
        record = observe_plug(None, CABLE, True, NOW - timedelta(hours=3), NOW)
        off = observe_plug(record, CABLE, False, NOW, NOW)
        self.assertEqual((off['connected'], off['plugged_in_at']), (False, None))
        later = NOW + timedelta(hours=8)
        self.assertEqual(observe_plug(off, CABLE, True, later, later)['plugged_in_at'], later.isoformat())

    def test_a_different_cable_entity_is_a_first_sighting(self):
        record = observe_plug(None, CABLE, True, NOW - timedelta(hours=3), NOW)
        moved = observe_plug(record, 'binary_sensor.other', True, NOW - timedelta(hours=1), NOW)
        self.assertEqual(moved['plugged_in_at'], (NOW - timedelta(hours=1)).isoformat())


if __name__ == '__main__':
    unittest.main()
