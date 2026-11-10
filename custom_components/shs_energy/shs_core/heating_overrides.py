"""Durable Home Assistant inputs for temporary room heating and load flexibility."""
import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from math import isfinite

from .device_commands import actuator_targets, execution_setup_errors
from .native_commands import finite
from .operating_modes import device_mode


DEFAULT_MINUTES = 60
CHARGE_SETBACK_C = 1.0


def utcnow():
    return datetime.now(timezone.utc)


def heating_binding(options, owner):
    if not owner.startswith('device:'):
        return None
    key = owner.removeprefix('device:')
    mapping = options.get('device_control_mappings', {}).get(key, {})
    targets = actuator_targets(mapping)
    kind = mapping.get('control_type')
    if (key in options.get('excluded_device_readings', ()) or kind not in ('setpoint', 'switch_schedule')
            or execution_setup_errors(mapping)):
        return None
    if kind == 'switch_schedule':
        sensor = mapping.get('temperature_entity_id')
        if not sensor or not mapping.get('room_area_id'):
            return None
        return [targets[0], sensor]
    return [targets[0]]


def heating_room(options, owner):
    mapping = options.get('device_control_mappings', {}).get(owner.removeprefix('device:'), {})
    area = mapping.get('room_area_id')
    return f'room:{area}' if area else owner


def heating_members(book):
    return {owner for zone, row in book.get('zones', {}).items() if row.get('expires_at')
            for owner in row.get('bindings', {zone: row.get('binding')})}


def relay_heat(current_c, target_c, state):
    """Start below the setpoint; allow a small deadband before turning off."""
    return 'on' if current_c < target_c + (.25 if state == 'on' else 0) else 'off'


def room_temperature(entity, state, attributes, climate_unit):
    if entity.startswith('climate.'):
        unit, value = climate_unit, attributes.get('current_temperature')
    else:
        unit, value = attributes.get('unit_of_measurement'), state
    if state in (None, 'unknown', 'unavailable') or unit != '°C':
        raise ValueError(f'{entity} must report a room temperature in Celsius')
    return finite(value)


def active_room_heating(book, room, now):
    row = book.get('zones', {}).get(room, {})
    if row.get('expires_at') and now < datetime.fromisoformat(row['expires_at']):
        return row
    return None


def active_heating(book, options, owner, now):
    row = active_room_heating(book, heating_room(options, owner), now)
    binding = heating_binding(options, owner)
    if (row and binding and row.get('bindings', {owner: row.get('binding')}).get(owner) == binding
            and device_mode(options, owner) == 'controlling'):
        return row
    return None


class HeatingOverrides:
    def __init__(self, store, changed, *, now=utcnow):
        self.store, self.changed, self.now = store, changed, now
        self.lock = asyncio.Lock()
        self.value = dict(revision=0, order=[], zones={})
        self.expiry = None
        self.closed = False

    async def load(self, options=None):
        saved = await self.store.async_load()
        if saved is not None:
            self.value = saved
            if options is not None:
                await self.move_device_overrides(options)
        self.schedule_expiry()

    async def move_device_overrides(self, options):
        """Move earlier device controls into rooms without adding heater demand."""
        updated = deepcopy(self.value)
        zones = updated['zones']
        now = self.now()
        for owner, row in self.value['zones'].items():
            room = heating_room(options, owner)
            if room == owner:
                continue
            del zones[owner]
            prior = zones.get(room, {})
            active = bool(active_room_heating(self.value, owner, now)
                          and row.get('binding') == heating_binding(options, owner)
                          and device_mode(options, owner) == 'controlling')
            prior_active = bool(active_room_heating(updated, room, now))
            if (prior_active, prior.get('revision', 0)) >= (active, row.get('revision', 0)) and prior:
                continue
            migrated = {key: row[key] for key in ('revision', 'minutes') if key in row}
            if active:
                migrated.update(bindings={owner: row['binding']}, target_c=row['target_c'],
                                expires_at=row['expires_at'])
            zones[room] = migrated
        if updated != self.value:
            await self.store.async_save(updated)
            self.value = updated

    def schedule_expiry(self):
        self.cancel_expiry()
        if self.closed:
            return
        now = self.now()
        upcoming = [datetime.fromisoformat(row['expires_at']) for row in self.value['zones'].values()
                    if row.get('expires_at') and datetime.fromisoformat(row['expires_at']) > now]
        if upcoming:
            self.expiry = asyncio.get_running_loop().call_later(
                (min(upcoming) - now).total_seconds(), self.expired)

    def expired(self):
        self.schedule_expiry()
        self.changed()

    def close(self):
        self.closed = True
        self.cancel_expiry()

    def cancel_expiry(self):
        if self.expiry is not None:
            self.expiry.cancel()
            self.expiry = None

    async def change(self, body, options, equipment, valid_owners):
        if type(body) is not dict or body.keys() != {'owner', 'field', 'value'}:
            raise ValueError('Invalid heating control request')
        owner, field, value = body['owner'], body['field'], body['value']
        async with self.lock:
            new = deepcopy(self.value)
            if field == 'order':
                if (owner is not None or type(value) is not list or any(type(item) is not str for item in value)
                        or len(set(value)) != len(value) or not set(value) <= valid_owners):
                    raise ValueError('Choose each flexible load once from the available devices')
                new['order'] = value[:]
            else:
                if not equipment or type(owner) is not str:
                    raise ValueError('This heating zone has no available temperature control')
                binding, lower, upper = equipment
                now = self.now()
                active = (active_room_heating(new, owner, now) if isinstance(binding, dict)
                          else active_heating(new, options, owner, now))
                row = new['zones'].setdefault(owner, {})
                if field == 'target':
                    if type(value) not in (int, float) or not isfinite(value):
                        raise ValueError('Enter a valid temperature')
                    if (lower is not None and value < lower) or (upper is not None and value > upper):
                        raise ValueError('Choose a temperature within the equipment range')
                    row['bindings' if isinstance(binding, dict) else 'binding'] = binding
                    row['target_c'] = value
                    row['expires_at'] = (active['expires_at'] if active else
                        (now + timedelta(minutes=row.get('minutes', DEFAULT_MINUTES))).isoformat())
                elif field == 'minutes':
                    if type(value) not in (int, float) or not isfinite(value) or value < 1 or not float(value).is_integer():
                        raise ValueError('Enter a whole number of minutes of at least 1')
                    try:
                        end = now + timedelta(minutes=value)
                    except OverflowError as error:
                        raise ValueError('The duration is too large') from error
                    if active:
                        row['expires_at'] = end.isoformat()
                    else:
                        row['minutes'] = int(value)
                elif field == 'resume' and value is None:
                    row.pop('expires_at', None)
                    row.pop('target_c', None)
                else:
                    raise ValueError('Invalid heating control')
                row['revision'] = new['revision'] + 1
            new['revision'] += 1
            await self.store.async_save(new)
            self.value = new
            self.schedule_expiry()
            if not self.closed:
                self.changed()
            return new

    async def retire(self, options, *, released=None):
        """Only an explicit binding or permission change retires an instruction."""
        async with self.lock:
            new = deepcopy(self.value)
            for room, row in new['zones'].items():
                if not row.get('expires_at'):
                    continue
                bindings = row.get('bindings', {room: row.get('binding')})
                current = {owner: binding for owner, binding in bindings.items()
                           if binding and owner != released and heating_room(options, owner) == room
                           and binding == heating_binding(options, owner) and device_mode(options, owner) == 'controlling'}
                if not current:
                    row.pop('expires_at', None)
                    row.pop('target_c', None)
                if 'bindings' in row:
                    row['bindings'] = current
            if new == self.value:
                return
            new['revision'] += 1
            await self.store.async_save(new)
            self.value = new
            self.schedule_expiry()
            self.changed()
