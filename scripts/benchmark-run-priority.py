"""Measure live run-priority decisions during a burst of meter updates.

Runs the real gateway decision path on the test rig with a charging car, a
charging house battery and three scheduled room thermostats. Every simulated
meter update wakes all five owners, as a burst does in Home Assistant.

    python scripts/benchmark-run-priority.py [updates]

Numbers are for the machine running the script. Earlier profiling put the
Intel N100 Home Assistant host at about 2-2.5x an Apple M3; scale accordingly
for Raspberry Pi-class hardware. See docs/run-priority.md.
"""
import asyncio
import logging
from pathlib import Path
import statistics
import sys
from time import perf_counter, process_time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tests'))
import test_grid_capacity as rig  # noqa: E402

ROOMS = ('laundry', 'master', 'pool')


async def measure(updates):
    case = rig.LiveImportTests('test_meter_burst_shares_one_decision_pass')
    await case.asyncSetUp()
    source = case.source
    case.executable_ev(case.car_run(hours=2, spare_w=4000))
    case.charging_battery(soc='54')
    intents = source.capacity['coordination']['intents']
    for index, room in enumerate(ROOMS):
        case.scheduled_room(temperature=19.5 + index)
        options = source.options()
        options['device_control_mappings'][room] = dict(options['device_control_mappings']['room'],
            actuator_entity_ids=[f'climate.{room}'], power=f'sensor.{room}_power')
        options['device_modes'][room] = 'controlling'
        source.configuration.options = lambda options=options: options
        case.rows[f'climate.{room}'] = case.rows['climate.room']
        case.rows[f'sensor.{room}_power'] = case.rows['sensor.room_power']
        source.power_peaks[f'sensor.{room}_power'] = 2000
        value = intents['device:room']
        intents[f'device:{room}'] = {**value, 'device': f'device:{room}', 'parameters': {
            **value['parameters'], 'commands': {room: value['parameters']['commands']['room']}}}
    intents.pop('device:room')
    charge_run = {'run': case.battery_run(minutes=12, target_wh=10600), 'want_w': 3800}

    async def wake_every_owner():
        await source.limit_ev_current('number.ev', 'switch.ev', 10, 5)
        for room in ROOMS:
            source.scheduled_heat_shed(f'device:{room}')
        await source.request('battery_capacity', {'charge_run': charge_run})

    results = {}
    for name, change in (
            ('rapid updates, every meter moving', lambda step: (15000 + step % 700, 11000 + step % 500)),
            ('delayed readings, grid ahead of the house meter', lambda step: (15000 + step % 700, 11000))):
        await wake_every_owner()
        passes, wall, cpu = source.priority_passes, [], process_time()
        for step in range(updates):
            grid, house = change(step)
            case.rows['sensor.grid']['state'] = str(grid)
            case.rows['sensor.house']['state'] = str(house)
            started = perf_counter()
            await wake_every_owner()
            wall.append((perf_counter() - started) * 1000)
        results[name] = dict(
            decision_ms_median=round(statistics.median(wall), 3),
            decision_ms_p95=round(sorted(wall)[int(len(wall) * .95)], 3),
            decision_ms_max=round(max(wall), 3),
            cpu_ms_per_update=round((process_time() - cpu) * 1000 / updates, 3),
            reserved_demand_passes_per_update=round((source.priority_passes - passes) / updates, 2))
    logging.disable(logging.CRITICAL)  # The rig's journal worker is cancelled at exit.
    return results


if __name__ == '__main__':
    count = int(sys.argv[1]) if len(sys.argv) > 1 else 500
    print(f'{count} meter updates, 5 owners woken by each (car, battery, 3 thermostats)')
    for name, row in asyncio.run(measure(count)).items():
        print(f'\n{name}')
        for key, value in row.items():
            print(f'  {key}: {value}')
