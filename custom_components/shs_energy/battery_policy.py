"""Sigenergy intent policy. Forecast watts are never executable input."""
from math import floor

if __package__:
    from .control_setup import PRESETS
    from .control_capabilities import finite_number, validate_value
else:
    from control_setup import PRESETS
    from control_capabilities import finite_number, validate_value

WRITE_ROLES = ('authority', 'mode', 'charge_ceiling', 'discharge_ceiling')
FLOW_ROLES = ('battery_power', 'pv_power', 'load_power', 'grid_import_power', 'grid_export_power', 'soc', 'soc_floor')
SENTINEL_W = 4294967295


def native_ceiling(capability, watts):
    """Round down at the native boundary, never exceeding the authorized watts."""
    watts = finite_number(watts)
    if watts < 0 or watts == SENTINEL_W:
        raise ValueError('ESS limit must be non-negative and not the unset sentinel')
    scale = 1000 if capability['unit'] == 'kW' else 1
    native = watts / scale
    origin, step = capability['minimum'], capability['step']
    if origin != 0 or step <= 0:
        raise ValueError('ESS ceiling must support zero and a positive step')
    result = round(floor(native / step + 1e-9) * step, 9)
    validate_value(capability, result)
    return result


def normal_state(binding):
    caps = {c['role']: c for c in binding['capabilities']}
    limits = binding['spec']['limits']
    mode = PRESETS['sigenergy']['normal_mode']
    if binding['spec']['normal_profile'] != {'mode': mode}:
        raise ValueError('reviewed normal profile required')
    result = {'mode': mode}
    for direction in ('charge', 'discharge'):
        watts = finite_number(limits['normal_' + direction + '_w'])
        maximum = finite_number(limits[direction + '_max_w'])
        if not 0 <= watts <= maximum or maximum == SENTINEL_W:
            raise ValueError('normal ESS limit exceeds reviewed rating')
        result[direction + '_ceiling'] = native_ceiling(caps[direction + '_ceiling'], watts)
    return result


def validate_measurements(measurements):
    m = {key: finite_number(measurements[key]) for key in FLOW_ROLES}
    if any(m[k] < 0 for k in ('pv_power', 'load_power', 'grid_import_power', 'grid_export_power')) or not 0 <= m['soc'] <= 100 or not 0 <= m['soc_floor'] <= 100:
        raise ValueError('invalid battery or household observation')
    return m


def desired_state(control, instruction, measurements, binding, scope, seconds):
    """Narrow a complete accepted instruction using live SOC and site headroom."""
    if set(instruction) != {'intent', 'charge_ceiling_w', 'discharge_ceiling_w'}:
        raise ValueError('explicit battery intent and both ceilings are required')
    intent = instruction['intent']
    modes = PRESETS['sigenergy']['modes']
    if intent not in modes or intent not in control['local']['supported_intents']:
        raise ValueError('unsupported battery intent')
    charge = finite_number(instruction['charge_ceiling_w'])
    discharge = finite_number(instruction['discharge_ceiling_w'])
    limits, objective = binding['spec']['limits'], control['desired']['objective']
    if not 0 <= charge <= limits['charge_max_w'] or not 0 <= discharge <= limits['discharge_max_w']:
        raise ValueError('battery ceiling exceeds reviewed rating')
    if intent in ('charge_pv_first', 'charge_grid_first') and (not objective['allow_grid_charge'] or discharge):
        raise ValueError('grid charging is not permitted or has a discharge ceiling')
    if intent == 'discharge_pv_first' and (not objective['allow_battery_export'] or charge):
        raise ValueError('battery export is not permitted or has a charge ceiling')
    if intent == 'hold' and (charge or discharge):
        raise ValueError('hold requires both ceilings to be zero')
    capacity = finite_number(scope['battery_capacity_kwh'])
    charge_eff, discharge_eff = (finite_number(scope[k]) for k in ('battery_charge_efficiency', 'battery_discharge_efficiency'))
    grid_import, grid_export = (finite_number(scope[k]) for k in ('grid_import_limit_w', 'grid_export_limit_w'))
    if capacity <= 0 or not 0 < charge_eff <= 1 or not 0 < discharge_eff <= 1 or min(grid_import, grid_export) < 0 or seconds <= 0:
        raise ValueError('reviewed capacity, efficiencies, grid limits and remaining interval required')
    m = validate_measurements(measurements)
    reserve = max(limits['minimum_soc_pct'], objective['reserve_soc_pct'], m['soc_floor'])
    if intent == 'discharge_pv_first':
        reserve = max(reserve, objective['export_reserve_soc_pct'])
    upper = min(limits['maximum_soc_pct'], objective['maximum_soc_pct'])
    charge = min(charge, max(0, upper - m['soc']) / 100 * capacity / charge_eff * 3600000 / seconds)
    discharge = min(discharge, max(0, m['soc'] - reserve) / 100 * capacity * discharge_eff * 3600000 / seconds)
    # Use both independent balances conservatively; asynchronous readings must
    # not manufacture extra connection headroom.
    net_load = m['load_power'] - m['pv_power']
    net_grid = m['grid_import_power'] - m['grid_export_power'] - m['battery_power']
    charge = min(charge, max(0, grid_import - max(net_load, net_grid)))
    discharge = min(discharge, max(0, grid_export + min(net_load, net_grid)))
    if intent in ('self_consumption', 'solar_charge'):
        charge = min(charge, max(0, -net_load))
        discharge = min(discharge, max(0, net_load))
    caps = {c['role']: c for c in binding['capabilities']}
    return {'mode': modes[intent], 'charge_ceiling': native_ceiling(caps['charge_ceiling'], charge),
            'discharge_ceiling': native_ceiling(caps['discharge_ceiling'], discharge)}


def operation(intent, settings, measurements, binding):
    """A low flow is an observation, not a failed ceiling command."""
    measurements = validate_measurements(measurements)
    caps = {c['role']: c for c in binding['capabilities']}
    limits = {direction: settings[direction + '_ceiling'] * (1000 if caps[direction + '_ceiling']['unit'] == 'kW' else 1)
              for direction in ('charge', 'discharge')}
    power = measurements['battery_power']
    tolerance = 100  # Observation tolerance only; never added to a write ceiling.
    if power > limits['charge'] + tolerance or -power > limits['discharge'] + tolerance:
        raise ValueError('measured battery flow exceeds the acknowledged ceiling')
    if intent in ('self_consumption', 'solar_charge'):
        if power > max(0, measurements['pv_power'] - measurements['load_power']) + tolerance or -power > max(0, measurements['load_power'] - measurements['pv_power']) + tolerance:
            raise ValueError('measured flow conflicts with self-consumption source/export policy')
    ceiling = limits['charge'] if power > 0 else limits['discharge'] if power < 0 else max(limits.values())
    return {'state': 'observed', 'delivery': 'below_ceiling' if abs(power) + tolerance < ceiling else 'within_ceiling',
            'measured_power_w': power, 'applicable_ceiling_w': ceiling,
            'reason': 'Settings acknowledged; observed flow is not a promise of delivery'}
