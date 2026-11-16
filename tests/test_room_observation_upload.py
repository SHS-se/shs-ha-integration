from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock

from household_fixture import Rig


class RoomObservationUploadTests(IsolatedAsyncioTestCase):
    async def test_probe_only_room_uploads_completed_means_without_device_inventory(self):
        key = "hall_probe"
        sensor = "sensor.synthetic_hall_temperature"
        rig = Rig(options={"rooms": {key: {"temperature_entity_id": sensor}}})
        household = rig.household
        household.ports = replace(household.ports, area_names=lambda: {key: "Hall", "garage": "Garage"})
        household.data = {"room_observations": True, "subscription_active": True}
        rig.states[sensor] = SimpleNamespace(attributes={"unit_of_measurement": "°C"})
        rig.client.upload_room_observations = AsyncMock(return_value={"connections": [], "readings_ignored": 0})
        start = rig.now - timedelta(minutes=15)
        rig.history.statistics.return_value = {
            sensor: [{"start": start + timedelta(minutes=offset), "mean": 19 + offset / 5}
                     for offset in (0, 5, 10, 15)],
        }

        await household.async_room_observations()
        await household.async_room_observations()

        rig.client.upload_room_observations.assert_awaited_once()
        rooms, slots = rig.client.upload_room_observations.await_args.args
        self.assertEqual(rooms["garage"], {"name": "Garage", "source_key": None})
        self.assertEqual(len(rooms[key]["source_key"]), 64)
        self.assertEqual(slots, [{"room_key": key, "start": start.isoformat(), "temperature_c": 20}])
        household.ports.inventory.assert_not_awaited()
        self.assertIsNone(household.optimisation_plan)

    async def test_unsupported_backend_leaves_its_room_and_planning_state_alone(self):
        rig = Rig()
        rig.household.data = {"subscription_active": True}
        rig.client.upload_room_observations = AsyncMock()

        await rig.household.async_room_observations()

        rig.client.upload_room_observations.assert_not_called()
        rig.history.statistics.assert_not_called()
