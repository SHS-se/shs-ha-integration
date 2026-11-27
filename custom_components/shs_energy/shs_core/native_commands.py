"""Resolve a setting against its native control surface, without dispatching it.

Execution and hypothetical verification use the same checks. Callers must resolve
again against fresh observations at the final dispatch boundary.
"""
from dataclasses import dataclass
from math import isfinite

from .command_journal import NativeAction, OffTemperature
from .native_numbers import canonical_metadata, equal as numbers_equal


def finite(value):
    if isinstance(value, bool):
        raise ValueError('boolean is not a numeric command')
    result = float(value)
    if not isfinite(result):
        raise ValueError('non-finite value')
    return result


def supports_heat_activation(attributes):
    modes = attributes.get('hvac_modes')
    return isinstance(modes, (list, tuple)) and 'heat' in modes and 'off' in modes


def native_thermostat_facts(entity, state):
    if state is None or state.state not in ("heat", "off"):
        return None
    attrs = state.attributes
    fields = dict(minimum_c="min_temp", maximum_c="max_temp", step_c="target_temp_step",
                  current_temperature_c="current_temperature", target_temperature_c="temperature")
    values = {key: attrs.get(attribute) for key, attribute in fields.items()}
    if (any(type(v) not in (int, float) or not isfinite(v) for v in values.values())
            or values["step_c"] <= 0 or values["maximum_c"] < values["minimum_c"]
            or not values["minimum_c"] <= values["target_temperature_c"] <= values["maximum_c"]
            or (state.state == "heat" and attrs.get("hvac_action") not in ("heating", "idle", "off"))):
        return None
    # Mode and the native capability to resume Heat are independent of the latch.
    return dict(entity_id=entity, **values,
                **({'can_activate_heat': supports_heat_activation(attrs)} if state.state == 'off' else {}),
                hvac_action="off" if state.state == "off" else
                "heating" if attrs["hvac_action"] == "heating" else "idle")


@dataclass(frozen=True)
class NativeCommand:
    action: NativeAction
    value: str | float
    equal: bool


def native_command(entity, value, state, *, temperature_unit, restore_off_target=False):
    """Validate a closed actuator setting using current native metadata."""
    if state is None:
        raise ValueError(f'{entity} is unavailable')
    domain = entity.split('.')[0]
    if domain in ('number', 'input_number'):
        try:
            value = float(canonical_metadata(value, state.attributes))
        except ValueError as error:
            raise ValueError(f'{entity}: target outside hardware bounds or supported step: {error}') from error
        service = 'set_value'
        equal = numbers_equal(state.state, value, state.attributes)
    elif domain == 'climate':
        if value in ('off', 'heat'):
            if value not in state.attributes.get('hvac_modes', ()) or temperature_unit != '°C':
                raise ValueError(f'{entity}: thermostat mode is unavailable')
            if value == 'heat' and (state.state not in ('off', 'heat') or
                                    state.state == 'off' and not supports_heat_activation(state.attributes)):
                raise ValueError(f'{entity}: thermostat cannot activate Heat from its current mode')
            return NativeCommand(NativeAction(entity, 'set_hvac_mode', value), value, state.state == value)
        value = finite(value)
        low, high = finite(state.attributes['min_temp']), finite(state.attributes['max_temp'])
        required_mode = 'off' if restore_off_target else 'heat'
        if restore_off_target and 'off' not in state.attributes.get('hvac_modes', ()):
            raise ValueError(f'{entity}: thermostat Off mode is unavailable')
        if state.state != required_mode or temperature_unit != '°C':
            raise ValueError(f'{entity}: setpoint execution requires an active Celsius heating thermostat')
        if not low <= value <= high:
            raise ValueError(f'{entity}: target outside hardware bounds')
        service = 'set_temperature'
        measured = state.attributes.get('temperature')
        equal = measured is not None and abs(finite(measured) - value) < 1e-6
        if restore_off_target:
            return NativeCommand(OffTemperature(entity, service, value), value, equal)
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
    def __init__(self, transport, read_state, temperature_unit, send, *, capacity=None):
        self.transport = transport
        self.read_state = read_state
        self.temperature_unit = temperature_unit
        self.send = send
        self.capacity = capacity

    def requires_command(self, entity, value):
        return self.capacity is not None and self.capacity.requires_command(entity, value)

    def command_in_flight(self, entity, value):
        """A write of this value that the control has not reported yet."""
        check = getattr(self.capacity, 'command_in_flight', None)
        return check is not None and check(entity, value)

    async def execute(self, command, *, authorize, timeout, on_sent=lambda: None):
        from .command_transport import CommandNotSent

        def check():
            authorize()
            if self.capacity is not None:
                self.capacity.check_command(command)
            action = command.action
            value = (action.service.removeprefix('turn_') if action.service in ('turn_on', 'turn_off')
                     else action.value)
            try:
                if isinstance(action, OffTemperature) and command.phase != 'handover':
                    raise ValueError('The dormant target can only be restored during handover')
                current = native_command(action.entity, value, self.read_state(action.entity),
                    restore_off_target=isinstance(action, OffTemperature),
                    temperature_unit=self.temperature_unit() if action.entity.startswith('climate.') else None)
                if current.action != action:
                    # Keep the adapter's exact wire scalar (Sigen truncates its
                    # gain conversion) while validating its canonical intent.
                    if (current.action.entity != action.entity or current.action.service != action.service
                            or action.service != 'set_value' or not numbers_equal(current.value, action.value,
                                self.read_state(action.entity).attributes)):
                        raise ValueError('Native command differs from its validated setting')
            except (KeyError, TypeError, ValueError) as error:
                raise CommandNotSent(str(error)) from error

        async def send(*args):
            if self.capacity is not None:
                self.capacity.mark_dispatch(command)
            await self.send(*args)
        await self.transport.execute(command, authorize=check, send=send,
                                     timeout=timeout, on_sent=on_sent)
