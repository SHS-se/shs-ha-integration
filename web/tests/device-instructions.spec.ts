import {test, expect} from '@playwright/test';
import {scheduledConsumption} from '../src/plan-chart/device-instructions';
import type {Plan, Slot} from '../src/types';

test('synthetic device forecasts remain background until their interval has an instruction', () => {
  const devices: Plan['devices'] = ['car', 'heater', 'boiler', 'pool'].map(key => ({key, name: key, category: key,
    ...(key === 'pool' ? {planning_service: 'pool', pool_role: 'heater'} : {})}));
  const commands = {
    car: {type: 'variable_power', value: 8, unit: 'A'}, heater: {type: 'setpoint', value: 20},
    boiler: {type: 'permit_inhibit', permitted: true}, pool: {type: 'unavailable'},
  };
  const slots = [
    {base_w: 1000, device_loads_w: {car: 2100, heater: 800, boiler: 600, pool: 3000},
      device_commands: Object.fromEntries(devices.map(device => [device.key, {type: 'unavailable'}])), pool_permitted: null},
    {base_w: 1000, device_loads_w: {car: 2100, heater: 800, boiler: 600, pool: 3000}, device_commands: commands, pool_permitted: true},
  ] as unknown as Slot[];
  const before = structuredClone(slots);
  const projected = scheduledConsumption(slots, devices);
  expect(projected[0].base_w).toBe(7500);
  expect(projected[0].device_loads_w).toEqual({car: 0, heater: 0, boiler: 0, pool: 0});
  expect(projected[1].base_w).toBe(1000);
  expect(projected[1].device_loads_w).toEqual(slots[1].device_loads_w);
  expect(slots).toEqual(before);
});
