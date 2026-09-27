"""Resolve a setting against its native control surface, without dispatching it.

Execution and hypothetical verification use the same checks. Callers must resolve
again against fresh observations at the final dispatch boundary.
"""
from dataclasses import dataclass
from math import isfinite

from .command_journal import NativeAction


def finite(value):
    if isinstance(value, bool):
        raise ValueError('boolean is not a numeric command')
    result = float(value)
    if not isfinite(result):
        raise ValueError('non-finite value')
    return result


@dataclass(frozen=True)
class NativeCommand:
    action: NativeAction
    value: str | float
    equal: bool


def native_command(entity, value, state, *, temperature_unit):
    """Validate a closed actuator setting using current native metadata."""
    if state is None:
        raise ValueError(f'{entity} is unavailable')
    domain = entity.split('.')[0]
    if domain in ('number', 'input_number'):
        value = finite(value)
        low, high = finite(state.attributes['min']), finite(state.attributes['max'])
        step = finite(state.attributes.get('step', 1))
        if not low <= value <= high or step <= 0:
            raise ValueError(f'{entity}: target outside hardware bounds')
        if abs((value-low)/step - round((value-low)/step)) > 1e-5:
            raise ValueError(f'{entity}: target is not a supported step')
        service = 'set_value'
        try:
            equal = abs(finite(state.state) - value) < 1e-6
        except (TypeError, ValueError):
            equal = False
    elif domain == 'climate':
        value = finite(value)
        low, high = finite(state.attributes['min_temp']), finite(state.attributes['max_temp'])
        if state.state != 'heat' or temperature_unit != '°C':
            raise ValueError(f'{entity}: setpoint execution requires an active Celsius heating thermostat')
        if not low <= value <= high:
            raise ValueError(f'{entity}: target outside hardware bounds')
        service = 'set_temperature'
        equal = abs(finite(state.attributes['temperature']) - value) < 1e-6
    elif domain in ('select', 'input_select'):
        if value not in state.attributes.get('options', []):
            raise ValueError(f'{entity}: unsupported mode {value}')
        service, equal = 'select_option', state.state == value
    elif domain in ('switch', 'input_boolean'):
        if value not in ('on', 'off'):
            raise ValueError(f'{entity}: invalid switch state')
        service, equal = f'turn_{value}', state.state == value
    else:
        raise ValueError(f'{entity}: unsupported actuator domain')
    action = NativeAction(entity, service, None if domain in ('switch', 'input_boolean') else value)
    action.service_call()
    return NativeCommand(action, value, equal)


class NativeExecutor:
    """Durable native dispatch with metadata checked at the last local boundary.

    The owner supplies synchronous permission/decision authorization. Hardware
    validation happens after that authorization in the transport's scheduled
    coroutine, after journal preparation, without another intervening await.
    """
    def __init__(self, transport, read_state, temperature_unit, send):
        self.transport = transport
        self.read_state = read_state
        self.temperature_unit = temperature_unit
        self.send = send

    async def execute(self, command, *, authorize, timeout, on_sent=lambda: None):
        from .command_transport import CommandNotSent

        def check():
            authorize()
            action = command.action
            value = (action.service.removeprefix('turn_') if action.service in ('turn_on', 'turn_off')
                     else action.value)
            try:
                current = native_command(action.entity, value, self.read_state(action.entity),
                    temperature_unit=self.temperature_unit() if action.entity.startswith('climate.') else None)
                if current.action != action:
                    raise ValueError('Native command differs from its validated setting')
            except (KeyError, TypeError, ValueError) as error:
                raise CommandNotSent(str(error)) from error

        await self.transport.execute(command, authorize=check, send=self.send,
                                     timeout=timeout, on_sent=on_sent)
