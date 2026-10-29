"""One native-number lattice for metadata, service values and acknowledgements.

Only binary floating representation noise is canonicalized. A neighbouring
hardware tick is a different setting, including an upper current overshoot.
"""
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN
from math import isfinite, ulp, nextafter, inf


def decimal(value):
    if isinstance(value, bool):
        raise ValueError('boolean is not a native number')
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ValueError('invalid native number') from error
    if not number.is_finite():
        raise ValueError('non-finite native number')
    return number


def canonical(value, minimum, maximum, step):
    number, low, high, quantum = map(decimal, (value, minimum, maximum, step))
    if quantum <= 0 or low > high:
        raise ValueError('invalid native number metadata')
    ticks = ((number-low)/quantum).to_integral_value(rounding=ROUND_HALF_EVEN)
    exact = low + ticks*quantum
    # Decimal(str(float)) can expose the last binary bit after unit conversion.
    # Bound forgiveness by representational precision, never by native step.
    floats = [float(v) for v in (number, exact)]
    if not all(isfinite(v) for v in floats):
        raise ValueError('native number is outside finite machine range')
    noise = Decimal(str(max(ulp(v) for v in floats)*2))
    if abs(number-exact) > noise or not low <= exact <= high:
        raise ValueError('native number is outside its range or supported step')
    return exact


def canonical_metadata(value, attributes):
    return canonical(value, attributes['min'], attributes['max'], attributes.get('step', 1))


def equal(value, expected, attributes=None):
    try:
        if attributes is not None and all(key in attributes for key in ('min','max')):
            return canonical_metadata(value, attributes) == canonical_metadata(expected, attributes)
        a, b = decimal(value), decimal(expected)
        if not all(isfinite(float(item)) for item in (a,b)):
            return False
        return abs(a-b) <= Decimal(str(max(ulp(float(a)),ulp(float(b)))*2))
    except (ValueError, KeyError, TypeError, OverflowError):
        return False


def watts(value, attributes):
    unit = attributes['unit_of_measurement']
    if unit not in ('W','kW'):
        raise ValueError('native power number requires W or kW')
    return float(canonical_metadata(value, attributes) * (1000 if unit == 'kW' else 1))


def service_watts(value, attributes):
    unit = attributes['unit_of_measurement']
    if unit not in ('W','kW'):
        raise ValueError('native power number requires W or kW')
    scale = 1000 if unit == 'kW' else 1
    exact = canonical_metadata(decimal(value)/scale, attributes)
    wire = float(exact)
    # The supported Sigen U32 registers encode int(kW * 1000). Some
    # representable quotients multiply one ULP below their intended integer.
    # Preserve the exact tick while choosing the adjacent binary encoding
    # whose truncating conversion reaches that tick, never a neighbouring one.
    integer = exact*scale
    if integer == integer.to_integral_value() and int(wire*scale) < int(integer):
        wire = nextafter(wire, inf)
    if integer == integer.to_integral_value() and int(wire*scale) != int(integer):
        raise ValueError('native wire value cannot encode the intended tick')
    if canonical_metadata(wire, attributes) != exact:
        raise ValueError('native wire encoding changed the intended tick')
    return wire


def floor_native(value, attributes):
    """Largest representable native tick at or below a real budget, or none.

    The origin is the hardware minimum, not zero. This does not forgive a
    budget shortfall or manufacture an extra tick at a physical upper bound.
    """
    from decimal import ROUND_FLOOR
    budget = decimal(value)
    low, high, step = map(decimal,(attributes['min'],attributes['max'],attributes.get('step',1)))
    if step <= 0 or low > high:
        raise ValueError('invalid native number metadata')
    if budget < low:
        return None
    ticks = ((min(budget,high)-low)/step).to_integral_value(rounding=ROUND_FLOOR)
    return low+ticks*step
