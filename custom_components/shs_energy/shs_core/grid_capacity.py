"""Measured aggregate admission at the physical write boundary, not a planner."""
from math import isfinite

from .native_readings import power
from .const import EU_AC_PHASE_COUNT, EU_AC_PHASE_VOLTAGE

EV_WATTS_PER_AMP = EU_AC_PHASE_COUNT * EU_AC_PHASE_VOLTAGE
CHARGING_MODES = frozenset(('Command Charging (PV First)', 'Command Charging (Grid First)'))
NON_GRID_MODES = frozenset(('Standby', 'Maximum Self Consumption',
    'Command Discharging (PV First)', 'Command Discharging (Battery First)'))


def battery_import_bound(mode, ceiling_w, maximum_w, conversion):
    if mode == 'Command Charging (PV First)':
        return conversion.grid_charge.input(ceiling_w)
    if mode in NON_GRID_MODES:
        return 0.0
    # A valid but unsupported automatic mode has no commissioned no-import
    # promise. Reserve its rating rather than interpreting it as idle.
    return conversion.grid_charge.input(maximum_w)


def import_limit(value):
    if type(value) not in (int, float) or not isfinite(value) or value <= 0:
        raise ValueError('The website house import limit is unavailable')
    return float(value)


def battery_released(options, read):
    """An acknowledged ceiling alone does not prove physical charging stopped."""
    try:
        watts, _ = power(read(options['battery_power_measurement_entity']),
                         source=options['battery_power_measurement_entity'], signed=True)
        mode = read(options['battery_mode_entity'])['state']
        if mode in (None, 'unknown', 'unavailable'):
            return False
        limit = read(options['battery_charge_limit_entity'])
        unit = limit['attributes']['unit_of_measurement']
        if unit not in ('W', 'kW'):
            return False
        ceiling = float(limit['state']) * (1000 if unit == 'kW' else 1)
        if not isfinite(ceiling) or ceiling < 0:
            return False
        return mode in NON_GRID_MODES or (mode == 'Command Charging (PV First)' and ceiling == 0 and watts <= 100)
    except (ValueError, KeyError, TypeError):
        return False


def required_import(house_w, devices):
    """Union each measured load with its bound; sum independent partitions.

    Gross house demand excludes battery charging, whose measured partition is
    zero here. Solar and battery supply provide no discretionary import credit.
    """
    values = [house_w, *(watts for pair in devices for watts in pair)]
    if any(not isfinite(v) or v < 0 for v in values):
        raise ValueError('House import accounting requires non-negative power')
    return max(0.0, house_w-sum(measured for measured,_ in devices)) + sum(
        max(measured,bound) for measured,bound in devices)


def empty_capacity():
    return {'schema': 1, 'revision': 0, 'devices': {}}


def _superseded(effect, order, confirmed):
    return effect['order'] <= order and (effect['status'] == 'service_returned' or
        effect['witnessed'] is not None and effect['witnessed'] < confirmed)


def canonicalize_capacity(capacity):
    """Roll existing counter-keyed rows into their physical actuator identity.

    Keep every unresolved effect. A later confirmed setting can supersede only
    the same successful/witnessed writes that ordinary observation can retire.
    """
    if not any(key.startswith('device:') and '@' in key for key in capacity['devices']):
        return capacity
    devices = {}
    for key, row in capacity['devices'].items():
        identity = 'device@' + key.split('@', 1)[1] if key.startswith('device:') and '@' in key else key
        target = devices.setdefault(identity, {'effects': {}})
        for command, effect in row['effects'].items():
            target['effects'][command] = {**effect, 'device':identity}
        settled = row.get('settled')
        if settled and settled['order'] > target.get('settled', {}).get('order', -1):
            target['settled'] = {**settled, 'device':identity}
    for row in devices.values():
        if settled := row.get('settled'):
            row['effects'] = {key:effect for key,effect in row['effects'].items()
                if not _superseded(effect, settled['order'], settled['confirmed'])}
    capacity['devices'] = devices
    capacity['revision'] += 1
    return capacity


def capacity_bound(device):
    return max([device.get('settled', {}).get('after_w', 0.0)] +
               [effect['peak_w'] for effect in device.get('effects', {}).values()])


def commanded_controls(capacity, device):
    row = capacity['devices'].get(device, {})
    candidates = [*row.get('effects', {}).values(), *([row['settled']] if row.get('settled') else [])]
    latest = max(candidates, key=lambda e:e['order'], default={})
    return latest.get('controls', {})


def prepare_capacity(capacity, command_id, effect, through):
    """Called in the command's existing SQLite transaction, before dispatch."""
    if effect is None:
        return
    row = capacity['devices'].setdefault(effect['device'], {'effects': {}})
    peak = max(capacity_bound(row), effect['before_w'], effect['after_w'], effect['possible_w'])
    row['effects'][command_id] = {**effect, 'peak_w': peak, 'order': through,
                                  'status': 'prepared', 'witnessed': None, 'dispatch_through':None}
    capacity['revision'] += 1


def dispatch_capacity(capacity, command_id, through):
    for row in capacity['devices'].values():
        effect = row['effects'].get(command_id)
        if effect is not None:
            effect['dispatch_through'] = through
            effect['witnessed'] = None
            capacity['revision'] += 1
            return


def finish_capacity(capacity, command_id, status):
    for row in capacity['devices'].values():
        effect = row['effects'].get(command_id)
        if effect is not None:
            if status == 'not_sent':
                del row['effects'][command_id]
            else:
                effect['status'] = status
            capacity['revision'] += 1
            return


def _matches(row, value):
    if row is None:
        return False
    state = row.get('attributes', {}).get('temperature') if row.get('entity_id', '').startswith('climate.') else row.get('state')
    if type(value) in (int, float):
        try:
            return isfinite(float(state)) and abs(float(state)-value) < 1e-6
        except (ValueError, TypeError):
            return False
    return state == value


def observe_capacity(capacity, ordinal, latest):
    """Settle only received control acknowledgement plus physical response.

    Source timestamps are deliberately irrelevant. Service completion alone
    releases nothing. Never-observed uncertain higher writes survive a later
    lower command, since that timed-out write may still arrive at equipment.
    """
    changed = False
    for row in capacity['devices'].values():
        for identity, effect in list(row['effects'].items()):
            fence = effect['dispatch_through']
            if fence is None:
                continue
            controls = effect.get('acknowledgements',effect['controls'])
            if not all(_matches(latest(entity)[1], value) for entity, value in controls.items()):
                continue
            acknowledgement, _ = latest(effect['changed'])
            if acknowledgement <= fence:
                continue
            sensor = effect['sensor']
            if not sensor and effect.get('logical') in ('ev','battery'):
                continue
            if sensor:
                received, report = latest(sensor)
                if received < acknowledgement or received <= fence:
                    continue
                try:
                    watts, _ = power(report, source=sensor, signed=effect['signed'])
                    watts = max(0.0, watts)
                    watts = (watts + effect['overhead_w']) * effect['scale'] if watts else 0.0
                    if watts > effect['after_w'] + effect['response_tolerance_w']:
                        continue
                except (ValueError, KeyError, TypeError):
                    continue
            if effect['witnessed'] is None:
                effect['witnessed'] = ordinal
                changed = True
            if effect['status'] != 'service_returned':
                continue
            if effect['order'] >= row.get('settled', {}).get('order', -1):
                row['settled'] = {**effect, 'confirmed': ordinal}
                changed = True
            for previous_id, previous in list(row['effects'].items()):
                if _superseded(previous, effect['order'], ordinal):
                    del row['effects'][previous_id]
                    changed = True
    if changed:
        capacity['revision'] += 1


def _current(value):
    amps = float(value)
    if not isfinite(amps) or amps < 0:
        raise ValueError('EV current must be finite and non-negative')
    return amps


def positive_power(value, unit='W'):
    """Normalize an observed power sample; absent/invalid samples supply no peak."""
    if isinstance(value, bool) or unit not in ('W', 'kW'):
        return None
    try:
        watts = float(value) * (1000 if unit == 'kW' else 1)
    except (ValueError, TypeError):
        return None
    return watts if isfinite(watts) and watts > 0 else None


def merge_power_peaks(peaks, observed):
    """Monotonic max-merge, shared by history seeding and durable observations."""
    changed = False
    for entity, value in observed.items():
        watts = positive_power(value)
        if watts is not None and watts > peaks.get(entity, 0):
            peaks[entity] = watts
            changed = True
    return changed


def observe_power_peak(peaks, row):
    if not row:
        return False
    watts = positive_power(row.get('state'), row.get('attributes', {}).get('unit_of_measurement'))
    return bool(watts is not None and merge_power_peaks(peaks, {row['entity_id']: watts}))


def device_power_bound(mapping, peaks):
    """Use literal Power watts or the mapped sensor's greatest recorded watts."""
    source = mapping.get('power')
    if type(source) in (int, float):
        return positive_power(source)
    return positive_power(peaks.get(source)) if isinstance(source, str) else None


class GridCapacityConfigurationError(ValueError):
    pass


def capacity_key(logical, controls):
    """Different actuator sets are independent loads, even after a remapping."""
    if logical.startswith('device:'):
        logical = 'device'
    return logical + '@' + ','.join(sorted(controls))


def house_meter_errors(options):
    return {} if options.get('house_consumption_power_entity') else {
        'house_consumption_power_entity':['Select gross instantaneous house consumption to check the house limit before increasing demand']}


class HouseMeterError(GridCapacityConfigurationError):
    def __init__(self, options):
        fields = house_meter_errors(options)
        super().__init__('; '.join(message for messages in fields.values() for message in messages))
        self.fix = {'kind':'fields','fields':[{'scope':'configuration','key':key,
            'message':'; '.join(messages)} for key,messages in fields.items()]}
        self.next_step = 'Open Energy and select Instantaneous house consumption.'
