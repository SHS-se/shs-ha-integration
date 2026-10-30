"""Measured aggregate admission at the physical write boundary, not a planner."""
from math import isfinite

from .native_readings import power
from .native_numbers import equal as numbers_equal
from .const import EU_AC_PHASE_COUNT, EU_AC_PHASE_VOLTAGE

EV_WATTS_PER_AMP = EU_AC_PHASE_COUNT * EU_AC_PHASE_VOLTAGE
CHARGING_MODES = frozenset(('Command Charging (PV First)', 'Command Charging (Grid First)'))
NON_GRID_MODES = frozenset(('Standby', 'Maximum Self Consumption',
    'Command Discharging (PV First)', 'Command Discharging (Battery First)'))
DISCHARGE_MODES = frozenset(('Maximum Self Consumption',
    'Command Discharging (PV First)', 'Command Discharging (Battery First)'))


def battery_import_bound(mode, ceiling_w, maximum_w, conversion):
    if mode == 'Command Charging (PV First)':
        return conversion.grid_charge.input(ceiling_w)
    if mode in NON_GRID_MODES:
        return 0.0
    # A valid but unsupported automatic mode has no commissioned no-import
    # promise. Reserve its rating rather than interpreting it as idle.
    return conversion.grid_charge.input(maximum_w)


def battery_discharge_ceiling(mode, ceiling_w):
    """DC discharge a native setting permits; other modes promise none."""
    return ceiling_w if mode in DISCHARGE_MODES else 0.0


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
    zero here. Solar supplies no import credit; admission subtracts measured
    battery discharge separately, as the planner's grid balance does.
    """
    values = [house_w, *(watts for pair in devices for watts in pair)]
    if any(not isfinite(v) or v < 0 for v in values):
        raise ValueError('House import accounting requires non-negative power')
    return max(0.0, house_w-sum(measured for measured,_ in devices)) + sum(
        max(measured,bound) for measured,bound in devices)


def empty_capacity():
    return {'schema': 2, 'revision': 0, 'devices': {}, 'receipts': {}}


def _superseded(effect, order, confirmed):
    return effect['order'] <= order and (effect['status'] == 'service_returned' or
        effect['witnessed'] is not None and effect['witnessed'] < confirmed)


def canonicalize_capacity(capacity):
    """Roll existing counter-keyed rows into their physical actuator identity.

    Keep every unresolved effect. A later confirmed setting can supersede only
    the same successful/witnessed writes that ordinary observation can retire.
    """
    if capacity['schema'] == 1:
        capacity.update(schema=2, receipts={})
        capacity['revision'] += 1
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
            return numbers_equal(state, value, row.get('attributes'))
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
    reconciled = []
    for row in capacity['devices'].values():
        settled = row.get('settled')
        if settled and settled.get('native_model'):
            changed_receipt = max((latest(entity)[0] for entity in settled['controls']),default=0)
            sensor_receipt = latest(settled['sensor'])[0]
            if (changed_receipt > settled['confirmed'] and sensor_receipt >= changed_receipt
                    and any(not _matches(latest(entity)[1],value) for entity,value in settled['controls'].items())):
                actual = _native_observation(settled,latest)
                if actual is not None:
                    row['settled'] = {**settled,'controls':actual[0],'after_w':actual[1],'confirmed':ordinal}
                    changed = True
        for identity, effect in list(row['effects'].items()):
            fence = effect['dispatch_through']
            if fence is None:
                continue
            actual = _bounded_observation(capacity, effect, latest)
            if actual is not None:
                matching = all(_matches(latest(entity)[1], value) for entity,value in effect['controls'].items())
                observed = {**effect, 'controls':actual[0], 'after_w':actual[1], 'confirmed':ordinal}
                if effect['order'] >= row.get('settled', {}).get('order', -1):
                    row['settled'] = observed
                # A native response bound covers only effects whose own bound
                # has elapsed and whose dispatch precedes this observation.
                for previous_id, previous in list(row['effects'].items()):
                    if previous['order'] <= effect['order'] and (_superseded(previous,effect['order'],ordinal)
                            or _bounded_observation(capacity, previous, latest) is not None):
                        del row['effects'][previous_id]
                reconciled.append((effect, matching))
                changed = True
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
    return reconciled


def _bounded_observation(capacity, effect, latest):
    model, deadline = effect.get('native_model'), effect.get('latest_effect_ms')
    if model is None or deadline is None or effect['dispatch_through'] is None:
        return None
    receipts = capacity.get('receipts', {})
    # The changed control and physical response must genuinely arrive after
    # the native bound. Old cached reports cannot acquire a fresh timestamp.
    for entity in (effect['changed'], effect['sensor']):
        receipt = receipts.get(entity, {})
        if receipt.get('ordinal', 0) <= effect['dispatch_through'] or receipt.get('at_ms', -1) < deadline:
            return None
    return _native_observation(effect,latest)


def _native_observation(effect, latest):
    model = effect['native_model']
    from .native_numbers import canonical_metadata, watts
    try:
        controls = {}
        for entity, expected in effect['controls'].items():
            report = latest(entity)[1]
            controls[entity] = (float(canonical_metadata(report['state'], report['attributes']))
                if type(expected) in (float,int) else report['state'])
            if type(expected) is str and report['state'] not in report['attributes'].get('options', (expected,)):
                return None
        mode = controls[model['mode']]
        ceiling = watts(controls[model['charge']], latest(model['charge'])[1]['attributes'])
        if mode == 'Command Charging (PV First)':
            bound = (ceiling + effect['overhead_w'])*effect['scale'] if ceiling else 0.0
        elif mode in NON_GRID_MODES:
            bound = 0.0
        else:
            bound = (model['charge_max_w']+effect['overhead_w'])*effect['scale']
        measured, _ = power(latest(effect['sensor'])[1], source=effect['sensor'], signed=True)
        actual = (max(0.0, measured)+effect['overhead_w'])*effect['scale'] if measured>0 else 0.0
        return controls, max(bound, actual)
    except (TypeError,ValueError,KeyError):
        return None


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


def power_setting_value(state):
    """Canonical text of a numeric native power setting; other states have no key."""
    if isinstance(state, bool):
        return None
    try:
        value = float(state)
    except (ValueError, TypeError):
        return None
    return f'{value:g}' if isfinite(value) else None


def setting_peak_key(sensor, setting, value):
    return f'{sensor}@{setting}={value}'


def observe_setting_peak(peaks, row, setting, setting_state):
    """Attribute a power sample to the native power setting holding when it arrived."""
    if not row:
        return False
    value = power_setting_value(setting_state)
    watts = positive_power(row.get('state'), row.get('attributes', {}).get('unit_of_measurement'))
    return bool(value is not None and watts is not None and merge_power_peaks(
        peaks, {setting_peak_key(row['entity_id'], setting, value): watts}))


def device_power_bound(mapping, peaks, setting_state=None):
    """Use literal Power watts or the mapped sensor's greatest recorded watts.

    Equipment with a native power setting draws according to that setting, so
    the greatest watts recorded at its current value bound it. A value without
    recorded evidence falls back to the sensor's greatest watts at any setting.
    """
    source = mapping.get('power')
    if type(source) in (int, float):
        return positive_power(source)
    if not isinstance(source, str):
        return None
    setting = mapping.get('power_setting_entity_id')
    value = power_setting_value(setting_state) if isinstance(setting, str) else None
    if value is not None:
        watts = positive_power(peaks.get(setting_peak_key(source, setting, value)))
        if watts is not None:
            return watts
    return positive_power(peaks.get(source))


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


def ev_current_ceiling(maximum_w, attributes, logical_minimum_a):
    from .native_numbers import floor_native, decimal
    current = floor_native(decimal(maximum_w)/decimal(EV_WATTS_PER_AMP),attributes)
    return 0.0 if current is None or current < decimal(logical_minimum_a) else float(current)


def capped_intention(value, maximum_w, *, native=None, logical_minimum_a=0):
    """Apply a physical ceiling to a current semantic intent, preserving service clocks."""
    from copy import deepcopy
    result = deepcopy(value)
    p = result['parameters']
    if result['operation'] != 'apply':
        return result
    owner = result['device']
    if owner == 'ev':
        if native is None:
            raise ValueError('EV capacity requires its native current metadata')
        current = ev_current_ceiling(maximum_w,native,logical_minimum_a)
        p['current_a'] = min(p['current_a'], current)
        p['minimum_a'] = min(p['minimum_a'], p['current_a'])
    elif owner == 'pool':
        p.update(heating_w=0, pool_permitted=False)
    else:
        key = owner.removeprefix('device:')
        command = p['commands'][key]
        if command['type'] == 'switch_schedule':
            command['on_seconds'] = 0
        elif command['type'] == 'permit_inhibit':
            command['permitted'] = False
        else:
            raise ValueError('This native owner has no supported demand reduction')
    return result
