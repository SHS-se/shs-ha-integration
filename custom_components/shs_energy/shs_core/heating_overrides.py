"""Durable Home Assistant inputs for temporary room heating and load flexibility."""
import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from math import isfinite

from .device_commands import actuator_targets, execution_setup_errors
from .operating_modes import device_mode


DEFAULT_MINUTES = 60


def utcnow():
    return datetime.now(timezone.utc)


def heating_binding(options, owner):
    if not owner.startswith('device:'):
        return None
    key = owner.removeprefix('device:')
    mapping = options.get('device_control_mappings', {}).get(key, {})
    targets = actuator_targets(mapping)
    if (key in options.get('excluded_device_readings', ()) or mapping.get('control_type') != 'setpoint'
            or execution_setup_errors(mapping)):
        return None
    return [targets[0]]


def active_heating(book, options, owner, now):
    row = book.get('zones', {}).get(owner, {})
    binding = heating_binding(options, owner)
    if (binding is not None and row.get('binding') == binding and row.get('expires_at')
            and device_mode(options, owner) == 'controlling'
            and now < datetime.fromisoformat(row['expires_at'])):
        return row
    return None


class HeatingOverrides:
    def __init__(self, store, changed, *, now=utcnow):
        self.store, self.changed, self.now = store, changed, now
        self.lock = asyncio.Lock()
        self.value = dict(revision=0, order=[], zones={})
        self.expiry = None
        self.closed = False

    async def load(self):
        saved = await self.store.async_load()
        if saved is not None:
            self.value = saved
        self.schedule_expiry()

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
                active = active_heating(new, options, owner, now)
                row = new['zones'].setdefault(owner, {})
                row['binding'] = binding
                if field == 'target':
                    if type(value) not in (int, float) or not isfinite(value):
                        raise ValueError('Enter a valid temperature')
                    if (lower is not None and value < lower) or (upper is not None and value > upper):
                        raise ValueError('Choose a temperature within the equipment range')
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
            obsolete = [owner for owner, row in self.value['zones'].items() if row.get('expires_at')
                        and (owner == released or heating_binding(options, owner) != row['binding']
                             or device_mode(options, owner) != 'controlling')]
            if not obsolete:
                return
            new = deepcopy(self.value)
            for owner in obsolete:
                new['zones'][owner].pop('expires_at', None)
                new['zones'][owner].pop('target_c', None)
            new['revision'] += 1
            await self.store.async_save(new)
            self.value = new
            self.schedule_expiry()
            self.changed()
