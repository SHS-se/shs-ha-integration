"""A newly mapped room sends a week of history, with duty read from its meter."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import unittest

from household_fixture import Rig
from shs_core.const import MAX_THERMAL_SLOTS_PER_PUSH, THERMAL_ROOM_HISTORY_HOURS
from shs_core.household_ports import HouseholdReadError

NOW = datetime(2026, 10, 19, 16, tzinfo=timezone.utc)
QUARTER = timedelta(minutes=15)
HEATER = 'sensor.bathroom_floor_heating_energy'
TEMPERATURE = 'sensor.bathroom_temperature'
DEVICE = {'key': HEATER, 'statistic_id': HEATER, 'planning_role': 'controllable',
          'control_type': 'setpoint', 'category': 'heating', 'active_power_w': 680.0,
          'mapping_summary': {'room_key': 'master_bathroom'}}
OPTIONS = {'planning_mode': 'live', 'device_control_mappings': {HEATER: {
    'control_type': 'setpoint', 'temperature_entity_id': TEMPERATURE,
    'actuator_entity_ids': ['climate.bathroom_floor']}}}


def recorder(start, end, entity_ids, period, units, types):
    """Five-minute statistics everywhere; the heater runs at half power."""
    rows, moment = [], start
    while moment < end:
        rows.append(moment)
        moment += timedelta(minutes=5)
    result = {}
    if TEMPERATURE in entity_ids:
        result[TEMPERATURE] = [{'start': t, 'mean': 21.0} for t in rows]
    if HEATER in entity_ids:
        # 340 W for five minutes.
        result[HEATER] = [{'start': t, 'change': 0.340 / 12} for t in rows]
    return result


class RoomHistoryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.rig = Rig(now=NOW, options=OPTIONS)
        self.rig.history.statistics.side_effect = recorder
        # The thermostat's state history is gone, as after purge_keep_days.
        self.household = self.rig.household

    async def quarters(self, start, history):
        return await self.household._thermal_quarters(OPTIONS, [dict(DEVICE)], start, NOW, history)

    async def test_meter_supplies_duty_without_actuator_history(self):
        slots = await self.quarters(NOW - timedelta(hours=1), None)
        self.assertEqual(len(slots), 4)
        self.assertEqual({slot['zone_observations']['master_bathroom']['actuator_duty'] for slot in slots}, {0.5})

    async def test_new_room_sends_older_quarters_in_the_room_left(self):
        start = NOW - timedelta(hours=1)
        slots = await self.quarters(start, {})
        starts = [datetime.fromisoformat(slot['start']) for slot in slots]
        self.assertEqual(len(slots), MAX_THERMAL_SLOTS_PER_PUSH)
        self.assertEqual(len(set(starts)), len(starts))
        self.assertEqual(starts[-1], NOW - QUARTER)
        earliest = NOW - QUARTER * MAX_THERMAL_SLOTS_PER_PUSH
        self.assertEqual(starts[0], earliest)
        self.assertTrue(all('master_bathroom' in slot['zone_observations'] for slot in slots))
        self.assertEqual(self.household.pending_room_history,
                         {'master_bathroom': earliest.isoformat(), '$house': earliest.isoformat()})

    async def test_history_continues_back_to_a_week_and_stops(self):
        start, history = NOW - timedelta(hours=1), {}
        floor = NOW - timedelta(hours=THERMAL_ROOM_HISTORY_HOURS)
        for _ in range(5):
            await self.quarters(start, history)
            history = self.household.pending_room_history
        self.assertEqual(history, {'master_bathroom': floor.isoformat(), '$house': floor.isoformat()})
        slots = await self.quarters(start, history)
        self.assertEqual(len(slots), 4)
        self.assertEqual(self.household.pending_room_history, history)

    async def test_flat_temperature_warns_and_recovers_without_changing_room_observations(self):
        self.rig.states[TEMPERATURE] = SimpleNamespace(state='21.0', last_reported=NOW,
            attributes={'friendly_name': 'Bathroom temperature', 'unit_of_measurement': '°C'})
        start = NOW - timedelta(hours=1)
        baseline = await self.quarters(start, None)
        self.assertEqual(self.household.attention_items, [])
        hourly = [{'start': (NOW - timedelta(hours=hours)).timestamp(), 'min': 21.0, 'max': 21.0}
                  for hours in range(1, 25)]

        def history(*args):
            if args[3] == 'hour':
                return {TEMPERATURE: hourly}
            return recorder(*args)

        self.rig.history.statistics.side_effect = history
        self.assertEqual(await self.quarters(start, None), baseline)
        warning, = self.household.attention_items
        self.assertEqual(warning['severity'], 'warning')
        self.assertIn('Bathroom temperature', warning['title'])
        self.assertEqual(warning['fix'], {'kind': 'entity', 'entity_id': TEMPERATURE})
        self.assertEqual(self.rig.repairs, [])

        def temporarily_unreadable(*args):
            if args[3] == 'hour':
                raise HouseholdReadError('Temporary recorder failure')
            return recorder(*args)

        self.rig.history.statistics.side_effect = temporarily_unreadable
        self.assertEqual(await self.quarters(start, None), baseline)
        self.assertEqual(self.household.attention_items, [warning])
        self.rig.states[TEMPERATURE].state = '21.1'
        self.assertEqual(self.household.attention_items, [])

        # A return to the old value after a real change is a different episode.
        self.rig.states[TEMPERATURE].state = '21'
        self.rig.history.statistics.side_effect = history
        hourly[0]['max'] = 21.1
        self.assertEqual(await self.quarters(start, None), baseline)
        self.assertEqual(self.household.attention_items, [])


if __name__ == '__main__':
    unittest.main()
