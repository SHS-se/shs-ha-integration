"""Switch-aware validity of device demand, independent of control authority."""
from dataclasses import dataclass

from .native_readings import BatteryPowerReadingError, power


def planned_switch_bindings(options, devices):
    excluded = set(options.get('excluded_device_readings', []))
    mappings = options.get('device_control_mappings', {})
    result = {}
    for device in devices:
        key = device['key']
        if key in excluded or device.get('planning_role') != 'controllable':
            continue
        entities = mappings.get(key, {}).get('actuator_entity_ids', [])
        if device.get('category') == 'ev_charging':
            entities = [options['ev_charge_switch_entity']] if options.get('ev_charge_switch_entity') else []
        if entities and all(isinstance(e, str) and e.split('.')[0] in ('switch', 'input_boolean') for e in entities):
            result[key] = tuple(entities)
    return dict(sorted(result.items()))


@dataclass(frozen=True)
class DevicePower:
    watts: float
    at_ms: int
    valid_until_ms: int
    reported_at_ms: int | None
    basis: str
    switch_states: tuple[tuple[str, str], ...]


def read_device_power(source, switches, read_entity, *, now_ms, capture_lifetime_ms):
    if not isinstance(source, str) or not source.startswith('sensor.'):
        raise BatteryPowerReadingError('instantaneous power sensor is not configured')
    states = []
    for entity in switches:
        report = read_entity(entity)
        state = report.get('state') if isinstance(report, dict) else None
        if state not in ('on', 'off'):
            raise BatteryPowerReadingError(f'{entity}: device power requires an available on/off switch state')
        states.append((entity, state))
    switch_states = tuple(states)
    if switch_states and all(state == 'off' for _, state in switch_states):
        # This is evidence from the actual switch state, not a zero substitute
        # for a failed sensor. The sensor's raw report remains in diagnostics.
        return DevicePower(0.0, now_ms, now_ms + capture_lifetime_ms, None, 'switch_off', switch_states)
    watts, reported = power(read_entity(source), source=source)
    # Availability is observed now; retain the sensor's timestamp as provenance.
    return DevicePower(watts, now_ms, now_ms + capture_lifetime_ms, reported,
                       'switched_ac' if switch_states else 'instantaneous_ac', switch_states)
