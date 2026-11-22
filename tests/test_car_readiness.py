"""Independently synthetic home visits, gaps and restarts; no household data."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
import unittest

sys.path.append(str(Path(__file__).parents[1] / 'custom_components/shs_energy'))
from shs_core.car_readiness import observe_location, location_readiness, READINESS_SECONDS

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
ENTITY = 'device_tracker.synthetic_vehicle'


def reading(state, at):
    return dict(state=state, last_changed=at.isoformat())


class LocationClockTests(unittest.TestCase):
    def test_home_advances_away_freezes_and_next_arrival_restarts(self):
        record = observe_location(None, ENTITY, reading('home', NOW), NOW)
        twelve = NOW + timedelta(hours=12)
        self.assertEqual(location_readiness(record, ENTITY, twelve)[0]['elapsed_seconds'], 43200)
        away = observe_location(record, ENTITY, reading('not_home', twelve), twelve)
        later = twelve + timedelta(hours=100)
        clock, estimated = location_readiness(away, ENTITY, later)
        self.assertEqual((clock['elapsed_seconds'], clock['running'], estimated), (43200, False, False))
        home = observe_location(away, ENTITY, reading('home', later), later)
        self.assertEqual(location_readiness(home, ENTITY, later)[0]['elapsed_seconds'], 0)

    def test_restart_and_repeated_home_reports_preserve_arrival(self):
        record = observe_location(None, ENTITY, reading('home', NOW), NOW)
        reloaded = json.loads(json.dumps(record))
        later = NOW + timedelta(hours=14)
        reported = observe_location(reloaded, ENTITY, reading('home', later), later)
        self.assertEqual(reported, record)
        self.assertEqual(location_readiness(reported, ENTITY, later)[0]['elapsed_seconds'], 50400)

    def test_named_zones_are_away_and_do_not_start_a_clock(self):
        home = observe_location(None, ENTITY, reading('home', NOW), NOW)
        left = NOW + timedelta(hours=2)
        away = observe_location(home, ENTITY, reading('work', left), left)
        other = observe_location(away, ENTITY, reading('shop', left + timedelta(hours=3)), left + timedelta(hours=3))
        self.assertEqual(other, away)
        self.assertEqual(location_readiness(other, ENTITY, left)[0]['elapsed_seconds'], 7200)

    def test_unavailable_at_home_never_renews_grace_and_recovery_repairs_estimate(self):
        record = observe_location(None, ENTITY, reading('home', NOW), NOW)
        for state in ('unavailable', 'unknown', None):
            missing = observe_location(record, ENTITY, reading(state, NOW), NOW + timedelta(hours=5))
            clock, estimated = location_readiness(missing, ENTITY, NOW + timedelta(hours=6))
            self.assertTrue(estimated)
            self.assertEqual(clock['elapsed_seconds'], READINESS_SECONDS)
            recovered = observe_location(missing, ENTITY, reading('home', NOW + timedelta(hours=6)), NOW + timedelta(hours=6))
            self.assertEqual(location_readiness(recovered, ENTITY, NOW + timedelta(hours=6))[0]['elapsed_seconds'], 21600)

    def test_gap_while_away_cannot_erase_an_unobserved_home_visit(self):
        record = observe_location(None, ENTITY, reading('home', NOW), NOW)
        away = observe_location(record, ENTITY, reading('not_home', NOW + timedelta(hours=2)), NOW + timedelta(hours=2))
        missing = observe_location(away, ENTITY, reading('unavailable', NOW), NOW + timedelta(hours=3))
        recovered = observe_location(missing, ENTITY, reading('not_home', NOW), NOW + timedelta(hours=5))
        clock, estimated = location_readiness(recovered, ENTITY, NOW + timedelta(hours=6))
        self.assertTrue(estimated)
        self.assertEqual(clock['elapsed_seconds'], READINESS_SECONDS)
        arrival = observe_location(recovered, ENTITY, reading('home', NOW + timedelta(hours=7)), NOW + timedelta(hours=7))
        self.assertFalse(location_readiness(arrival, ENTITY, NOW + timedelta(hours=7))[1])

    def test_initial_away_unknown_and_different_source_are_conservative(self):
        for state in ('not_home', 'unknown', 'unavailable'):
            record = observe_location(None, ENTITY, reading(state, NOW), NOW)
            clock, estimated = location_readiness(record, ENTITY, NOW)
            self.assertTrue(estimated)
            self.assertEqual(clock['elapsed_seconds'], READINESS_SECONDS)
        self.assertTrue(location_readiness(record, 'device_tracker.other', NOW)[1])

    def test_recovering_home_after_an_away_gap_does_not_invent_an_arrival_time(self):
        away = observe_location(None, ENTITY, reading('not_home', NOW), NOW)
        missing = observe_location(away, ENTITY, reading('unavailable', NOW), NOW + timedelta(hours=1))
        home = observe_location(missing, ENTITY, reading('home', NOW + timedelta(hours=3)), NOW + timedelta(hours=3))
        self.assertTrue(location_readiness(home, ENTITY, NOW + timedelta(hours=4))[1])
        away = observe_location(home, ENTITY, reading('not_home', NOW + timedelta(hours=5)), NOW + timedelta(hours=5))
        arrival = observe_location(away, ENTITY, reading('home', NOW + timedelta(hours=6)), NOW + timedelta(hours=6))
        clock, estimated = location_readiness(arrival, ENTITY, NOW + timedelta(hours=6))
        self.assertEqual((clock['elapsed_seconds'], estimated), (0, False))
