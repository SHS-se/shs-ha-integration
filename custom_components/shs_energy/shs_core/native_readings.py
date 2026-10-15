"""Validation of native power observations, with no controller dependencies."""
from datetime import datetime
from math import isfinite

def stamp(value):
    parsed=datetime.fromisoformat(value.replace('Z','+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('timestamp needs timezone')
    return round(parsed.timestamp()*1000)

class BatteryPowerReadingError(ValueError):
    """A failed live source is distinct from an actuator configuration error."""
    fix = {'kind': 'diagnostics'}
    next_step = 'Inspect the named power sensor for an unavailable value, invalid number or incorrect unit. Control retries automatically when it is usable.'

def power(report, *, source, signed=False):
    if not isinstance(report,dict):
        raise BatteryPowerReadingError(f'{source}: configured power source is unavailable')
    attrs=report['attributes'];unit=attrs.get('unit_of_measurement')
    if attrs.get('state_class')!='measurement' or unit not in ('W','kW'):
        raise BatteryPowerReadingError(f'{source}: instantaneous W or kW power required')
    try:
        at=stamp(report['last_reported']);value=float(report['state'])*(1000 if unit=='kW' else 1)
    except (KeyError, TypeError, ValueError) as error:
        raise BatteryPowerReadingError(f'{source}: invalid physical power reading') from error
    if not isfinite(value) or (value<0 and not signed):
        raise BatteryPowerReadingError(f'{source}: power source has an invalid value')
    return value,at
