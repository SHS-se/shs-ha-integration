"""Rooms lose heat into the house, so the app sends the house's temperature."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import unittest

from household_fixture import Rig
from shs_core.const import MAX_THERMAL_SLOTS_PER_PUSH, THERMAL_ROOM_HISTORY_HOURS
from shs_core.thermal import (
    MIN_HOUSE_AREAS, build_thermal_slots, house_temperature_entities, house_temperature_quarters,
    merge_thermal_slots,
)

NOW = datetime(2026, 10, 20, 12, tzinfo=timezone.utc)
QUARTER = timedelta(minutes=15)


def sensor(device_class='temperature', state_class='measurement', unit='°C'):
    return SimpleNamespace(state='21', attributes={
        'device_class': device_class, 'state_class': state_class, 'unit_of_measurement': unit})


class HouseSourcesTests(unittest.TestCase):
    def test_only_indoor_temperature_measurements_in_an_area(self):
        states = {
            'sensor.kitchen_temperature': sensor(),
            'sensor.kitchen_humidity': sensor(device_class='humidity', unit='%'),
            'sensor.kitchen_daily_mean': sensor(state_class=None),
            'sensor.hall_fahrenheit': sensor(unit='°F'),
            'sensor.outdoor_temperature': sensor(),
            'sensor.pool_water': sensor(),
            'climate.kitchen': sensor(),
            'sensor.unplaced_temperature': sensor(),
        }
        areas = {entity: 'kitchen' for entity in states if entity != 'sensor.unplaced_temperature'}
        found = house_temperature_entities(areas, states.get, ('sensor.outdoor_temperature', 'sensor.pool_water', None))
        self.assertEqual(found, {'sensor.kitchen_temperature': 'kitchen'})


class HouseQuartersTests(unittest.TestCase):
    def test_median_of_area_medians_outvotes_a_warm_chip_and_a_cold_garage(self):
        slot = NOW
        quarters = {
            'sensor.kitchen_th': {slot: 22.0}, 'sensor.kitchen_plug_chip': {slot: 31.0}, 'sensor.kitchen_alarm': {slot: 22.4},
            'sensor.hall': {slot: 21.0}, 'sensor.bedroom': {slot: 20.0}, 'sensor.garage': {slot: 9.0},
        }
        areas = {'sensor.kitchen_th': 'kitchen', 'sensor.kitchen_plug_chip': 'kitchen', 'sensor.kitchen_alarm': 'kitchen',
                 'sensor.hall': 'hall', 'sensor.bedroom': 'bedroom', 'sensor.garage': 'garage'}
        # Area medians 22.4, 21.0, 20.0, 9.0: the median of four is 20.5.
        self.assertEqual(house_temperature_quarters(quarters, areas), {slot: 20.5})

    def test_too_few_areas_is_not_a_house(self):
        quarters = {f'sensor.t{i}': {NOW: 20.0 + i} for i in range(MIN_HOUSE_AREAS - 1)}
        areas = {f'sensor.t{i}': f'area{i}' for i in range(MIN_HOUSE_AREAS - 1)}
        self.assertEqual(house_temperature_quarters(quarters, areas), {})


class HouseRowsTests(unittest.TestCase):
    def test_house_rides_beside_outdoor_and_alone(self):
        earlier = NOW - QUARTER
        rows = build_thermal_slots({}, {NOW: 5.0}, {earlier: 21.0, NOW: 21.5})
        self.assertEqual([(row['start'], row.get('outdoor_temperature_c'), row.get('house_temperature_c')) for row in rows],
                         [(earlier.isoformat(), None, 21.0), (NOW.isoformat(), 5.0, 21.5)])
        merged = merge_thermal_slots(build_thermal_slots({}, {NOW: 5.0}), build_thermal_slots({}, {}, {NOW: 21.5}))
        self.assertEqual(merged[0]['house_temperature_c'], 21.5)
        self.assertEqual(merged[0]['outdoor_temperature_c'], 5.0)


HEATER = 'sensor.bathroom_floor_heating_energy'
DEVICE = {'key': HEATER, 'statistic_id': HEATER, 'planning_role': 'controllable',
          'control_type': 'setpoint', 'category': 'heating', 'active_power_w': 680.0,
          'mapping_summary': {'room_key': 'master_bathroom'}}
OPTIONS = {'planning_mode': 'live', 'outdoor_temperature_entity': 'sensor.outdoor',
           'device_control_mappings': {HEATER: {
               'control_type': 'setpoint', 'temperature_entity_id': 'sensor.bathroom_temperature',
               'actuator_entity_ids': ['climate.bathroom_floor']}}}
HOUSE = {'sensor.kitchen': 22.0, 'sensor.hall': 21.0, 'sensor.bedroom': 20.0, 'sensor.outdoor': 4.0}


def recorder(start, end, entity_ids, period, units, types):
    moments, moment = [], start
    while moment < end:
        moments.append(moment)
        moment += timedelta(minutes=5)
    result = {entity: [{'start': t, 'mean': value} for t in moments] for entity, value in HOUSE.items() if entity in entity_ids}
    if 'sensor.bathroom_temperature' in entity_ids:
        result['sensor.bathroom_temperature'] = [{'start': t, 'mean': 21.0} for t in moments]
    if HEATER in entity_ids:
        result[HEATER] = [{'start': t, 'change': 0.340 / 12} for t in moments]
    return result


class HousePushTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.rig = Rig(now=NOW, options=OPTIONS)
        self.rig.history.statistics.side_effect = recorder
        for entity in HOUSE:
            self.rig.states[entity] = sensor()
        self.rig.areas.update({'sensor.kitchen': 'kitchen', 'sensor.hall': 'hall', 'sensor.bedroom': 'bedroom',
                               'sensor.outdoor': 'garden'})

    async def quarters(self, history):
        return await self.rig.household._thermal_quarters(OPTIONS, [dict(DEVICE)], NOW - timedelta(hours=1), NOW, history)

    async def test_current_rows_carry_the_house_without_the_outdoor_sensor(self):
        slots = await self.quarters(None)
        self.assertEqual({slot['house_temperature_c'] for slot in slots}, {21.0})
        self.assertEqual({slot['outdoor_temperature_c'] for slot in slots}, {4.0})

    async def test_a_week_of_house_history_follows_once_the_room_is_sent(self):
        room_done = (NOW - timedelta(hours=THERMAL_ROOM_HISTORY_HOURS)).isoformat()
        slots = await self.quarters({'master_bathroom': room_done})
        self.assertEqual(len(slots), MAX_THERMAL_SLOTS_PER_PUSH)
        older = slots[:-4]
        self.assertTrue(all(slot['zone_observations'] == {} and 'outdoor_temperature_c' not in slot for slot in older))
        self.assertEqual({slot['house_temperature_c'] for slot in older}, {21.0})
        self.assertEqual(self.rig.household.pending_room_history['$house'], older[0]['start'])


if __name__ == '__main__':
    unittest.main()
