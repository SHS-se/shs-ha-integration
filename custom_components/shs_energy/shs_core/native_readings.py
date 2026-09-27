"""Validation of native power observations, with no controller dependencies."""
from datetime import datetime
from math import isfinite

AGE_MS = 30000

def stamp(value):
    parsed=datetime.fromisoformat(value.replace('Z','+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('timestamp needs timezone')
    return round(parsed.timestamp()*1000)

class BatteryPowerReadingError(ValueError):
    """A failed live source is distinct from an actuator configuration error."""
    fix = {'kind': 'diagnostics'}
    next_step = 'Check that the named power sensor is reporting current measurements. Control retries automatically when fresh readings arrive.'

def power(report, *, source, now_ms, signed=False):
    if not isinstance(report,dict):
        raise BatteryPowerReadingError(f'{source}: configured power source is unavailable')
    attrs=report['attributes'];unit=attrs.get('unit_of_measurement')
    if attrs.get('state_class')!='measurement' or unit not in ('W','kW'):
        raise BatteryPowerReadingError(f'{source}: instantaneous W or kW power required')
    try:
        at=stamp(report['last_reported']);value=float(report['state'])*(1000 if unit=='kW' else 1)
    except (KeyError, TypeError, ValueError) as error:
        raise BatteryPowerReadingError(f'{source}: invalid physical power reading') from error
    if not isfinite(value) or (value<0 and not signed) or not at<=now_ms<at+AGE_MS:
        raise BatteryPowerReadingError(f'{source}: power source is invalid or stale (last report: {report["last_reported"]})')
    return value,at
