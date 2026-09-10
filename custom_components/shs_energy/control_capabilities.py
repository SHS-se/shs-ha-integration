"""Pure primitive discovery and command validation shared by setup and execution."""
from math import isfinite


def finite_number(value):
    if isinstance(value, bool):
        raise ValueError('boolean is not a numeric command')
    try:
        result = float(value)
    except (ValueError, TypeError):
        raise ValueError('a finite number is required') from None
    if not isfinite(result):
        raise ValueError('a finite number is required')
    return result


def primitive(domain, attributes, *, temperature_unit='°C'):
    """Describe only operations the entity advertises; never infer number meaning."""
    if domain in ('switch', 'input_boolean'):
        return {'operation': 'switch', 'unit': 'state', 'options': ['on', 'off']}
    if domain in ('select', 'input_select'):
        options = attributes.get('options')
        if not isinstance(options, list) or not options or not all(isinstance(v, str) for v in options):
            raise ValueError('select options are missing')
        return {'operation': 'select', 'unit': 'state', 'options': list(options)}
    if domain in ('number', 'input_number', 'climate'):
        climate = domain == 'climate'
        if climate and not int(attributes.get('supported_features', 0)) & 1:
            raise ValueError('thermostat does not support a single target temperature')
        keys = ('min_temp', 'max_temp', 'target_temp_step') if climate else ('min', 'max', 'step')
        try:
            low, high, step = (finite_number(attributes[k]) for k in keys)
        except KeyError as err:
            raise ValueError(f'hardware {err.args[0]} is missing') from None
        if low > high or step <= 0:
            raise ValueError('invalid hardware bounds or step')
        return {'operation': 'set_number', 'unit': temperature_unit if climate else attributes.get('unit_of_measurement'),
                'minimum': low, 'maximum': high, 'step': step}
    if domain == 'script':
        return {'operation': 'request_state', 'unit': 'state'}
    if domain in ('sensor', 'binary_sensor'):
        return {'operation': 'observe', 'unit': attributes.get('unit_of_measurement', 'state')}
    raise ValueError('unsupported interface domain')


def validate_value(capability, value):
    """Validate a complete primitive request, with no rounding or unit guessing."""
    if capability['operation'] == 'set_number':
        value = finite_number(value)
        low, high, step = (capability[k] for k in ('minimum', 'maximum', 'step'))
        if not low <= value <= high:
            raise ValueError('target outside hardware bounds')
        ticks = (value - low) / step
        if abs(ticks - round(ticks)) > 1e-5:
            raise ValueError('target is not a supported step')
    elif capability['operation'] in ('select', 'switch'):
        if value not in capability['options']:
            raise ValueError('unsupported mode or switch state')
    else:
        raise ValueError('this operation does not accept a scalar command')
    return value


def build_command(entity_id, state, attributes, value, *, temperature_unit='°C'):
    """Build an ordinary command without issuing it. Return normalized value too."""
    domain = entity_id.split('.')[0]
    try:
        capability = primitive(domain, attributes, temperature_unit=temperature_unit)
        value = validate_value(capability, value)
        if domain == 'climate':
            if state != 'heat' or temperature_unit != '°C':
                raise ValueError('setpoint execution requires an active Celsius heating thermostat')
            service, data = 'set_temperature', {'temperature': value}
            equal = abs(finite_number(attributes['temperature']) - value) < 1e-6
        elif domain in ('number', 'input_number'):
            service, data = 'set_value', {'value': value}
            equal = abs(finite_number(state) - value) < 1e-6
        elif domain in ('select', 'input_select'):
            service, data, equal = 'select_option', {'option': value}, state == value
        else:
            service, data, equal = f'turn_{value}', {}, state == value
    except (ValueError, KeyError) as err:
        raise ValueError(f'{entity_id}: {err}') from None
    return domain, service, data, value, equal
