import type {Plan, Slot} from '../types';

/** A forecast without an executable instruction belongs to background demand. */
export function scheduledConsumption(slots: Slot[], devices: Plan['devices']) {
  const pool = devices.filter(device => device.planning_service === 'pool');
  const declared = pool.filter(device => device.pool_role === 'heater');
  const heaters = new Set((declared.length ? declared : pool.length < 2 ? pool : [pool.reduce((a, b) =>
    (b.active_power_w ?? 0) > (a.active_power_w ?? 0) ? b : a)]).map(device => device.key));
  const scheduled = (slot: Slot, key: string) => {
    const command = slot.device_commands?.[key];
    return Boolean(command && typeof command === 'object' && 'type' in command && command.type !== 'unavailable') ||
      heaters.has(key) && typeof slot.pool_permitted === 'boolean';
  };
  return slots.map(slot => ({
    ...slot,
    base_w: slot.base_w === null ? null : slot.base_w + Object.entries(slot.device_loads_w ?? {})
      .reduce((sum, [key, watts]) => sum + (scheduled(slot, key) ? 0 : watts), 0),
    device_loads_w: Object.fromEntries(Object.entries(slot.device_loads_w ?? {})
      .map(([key, watts]) => [key, scheduled(slot, key) ? watts : 0])),
  }));
}
