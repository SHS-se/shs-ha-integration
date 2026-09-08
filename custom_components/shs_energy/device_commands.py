"""Schema-7 executable decisions and local capability validation."""
from math import isfinite


def numeric(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and isfinite(value)


def validate_commands(commands, models):
    if not isinstance(commands, dict) or set(commands) != {m['key'] for m in models}:
        raise ValueError('device commands must name every planned device exactly once')
    kinds = {m['key']: m['control_type'] for m in models}
    for key, command in commands.items():
        if not isinstance(command, dict):
            raise ValueError(f'{key}: invalid device command')
        kind = command.get('type')
        if kind == 'unavailable':
            valid = set(command) == {'type', 'reason'} and isinstance(command['reason'], str) and bool(command['reason'])
        elif kind != kinds[key]:
            valid = False
        elif kind == 'setpoint':
            valid = (set(command) == {'type', 'target_c', 'minimum_c', 'maximum_c'}
                and all(numeric(command.get(k)) for k in ('target_c', 'minimum_c', 'maximum_c'))
                and 5 <= command['minimum_c'] <= command['target_c'] <= command['maximum_c'] <= 35)
        elif kind == 'permit_inhibit':
            valid = set(command) == {'type', 'permitted'} and isinstance(command['permitted'], bool)
        elif kind == 'switch_schedule':
            # Only whole-quarter relay runs are executable. Fractional energy
            # forecasts require a discrete optimiser, not local PWM inference.
            valid = set(command) == {'type', 'on_seconds'} and numeric(command.get('on_seconds')) and command['on_seconds'] in (0, 900)
        elif kind == 'variable_power':
            valid = set(command) == {'type', 'value', 'unit'} and numeric(command.get('value')) and 0 <= command['value'] <= 100 and command.get('unit') == 'A'
        else:
            valid = False
        if not valid:
            raise ValueError(f'{key}: invalid {kind} device command')


def actuator_targets(mapping):
    kind = mapping.get('control_type')
    if kind == 'setpoint':
        targets = [mapping['setpoint_entity_id']] if mapping.get('setpoint_entity_id') else mapping.get('actuator_entity_ids', [])
    else:
        targets = mapping.get('actuator_entity_ids', [])
    return list(dict.fromkeys([*targets, *mapping.get('companion_actuator_entity_ids', [])]))


def execution_setup_errors(mapping):
    kind = mapping.get('control_type')
    if kind not in ('setpoint', 'switch_schedule', 'permit_inhibit'):
        return ['this control method has no generic executor; EV current uses the EV controller']
    if mapping.get('companion_actuator_entity_ids'):
        return ['coupled actuators require an explicit interlock and handover contract']
    if kind == 'permit_inhibit':
        maximum = mapping.get('max_inhibit_slots')
        if not numeric(maximum) or maximum < 1 or maximum != int(maximum):
            return ['a whole-number maximum inhibit duration is required']
    targets = actuator_targets(mapping)
    if not targets:
        return ['no execution actuator is configured']
    if kind == 'setpoint':
        low, high = mapping.get('minimum_temperature_c'), mapping.get('maximum_temperature_c')
        if not numeric(low) or not numeric(high) or not 5 <= low < high <= 35:
            return ['reviewed minimum and maximum temperatures are required']
        if mapping.get('companion_actuator_entity_ids') or any(mapping.get(k) for k in ('permit_entity_id', 'mode_entity_id', 'offset_entity_id')):
            return ['alternative or coupled setpoint controls need an explicit command contract']
        if any(t.split('.')[0] not in ('climate', 'number', 'input_number') for t in targets):
            return ['setpoint execution requires climate or temperature number entities']
    else:
        if any(t.split('.')[0] not in ('switch', 'input_boolean') for t in targets):
            return ['scheduled execution requires switch or input_boolean actuators']
        if kind == 'switch_schedule' and any(not numeric(mapping.get(k)) or not 0 <= mapping[k] <= 900 for k in ('minimum_on_seconds', 'minimum_off_seconds')):
            return ['reviewed minimum on and off times are required']
    return []
