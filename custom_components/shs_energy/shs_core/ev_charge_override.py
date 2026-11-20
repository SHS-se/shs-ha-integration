"""Durable manual EV charge override (docs/ev-charge-override.md).

The override is app only: the planner never sees it. It charges the car to a
target state of charge within a number of hours, then returns the car to the
plan. A deadline of None means as fast as the house allows.
"""
import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from math import ceil, isfinite

from .const import EV_CHARGE_EFFICIENCY
from .grid_capacity import EV_WATTS_PER_AMP
from .operating_modes import device_mode


def utcnow():
    return datetime.now(timezone.utc)


def empty():
    return dict(revision=0, active=None, hours=None)


def _percent(value):
    if type(value) not in (int, float) or not isfinite(value):
        raise ValueError('Enter a valid charge level')
    return float(value)


def charge_need_kwh(soc, target, capacity_kwh, efficiency):
    """Grid energy still needed; SOC values are fractions."""
    return max(0.0, target - soc) * capacity_kwh / efficiency


def fastest_hours(need_kwh, maximum_a):
    """Whole hours at the charger's maximum current, at least one when any charge is needed."""
    if need_kwh <= 0:
        return 0
    return max(1, ceil(need_kwh * 1000 / (maximum_a * EV_WATTS_PER_AMP) - 1e-9))


def override_current(need_kwh, deadline, now, minimum_a, maximum_a):
    """The current that finishes by the deadline, within the charger's range.

    Below the charger's minimum the car charges at the minimum and finishes
    early (user requirement, 20 November 2026).
    """
    if deadline is None or need_kwh <= 0:
        return maximum_a
    left_h = (deadline - now).total_seconds() / 3600
    if left_h <= 0:
        return maximum_a
    current = ceil(need_kwh * 1000 / left_h / EV_WATTS_PER_AMP - 1e-9)
    return float(min(maximum_a, max(minimum_a, current)))


# Live priority slack for an override that needs full power now: below every
# other run, so heaters and battery charging yield to the car.
URGENT_SLACK_H = -1000.0


def override_slack_h(need_kwh, deadline, now, maximum_a):
    """Hours the override could still wait at full power; URGENT once it cannot.

    A long override yields to more pressing loads and catches up later. Once
    the time left is what full power needs, the car comes first (user
    requirement, 20 November 2026).
    """
    if deadline is None or need_kwh is None:
        return URGENT_SLACK_H
    slack = (deadline - now).total_seconds() / 3600 - need_kwh * 1000 / (maximum_a * EV_WATTS_PER_AMP)
    return slack if slack > 0 else URGENT_SLACK_H


def capacity_kwh(soc, remaining_kwh):
    """The battery size the car's own readings imply, or None."""
    if remaining_kwh is None or soc is None or soc < 0.05:
        return None
    size = remaining_kwh / soc
    return size if isfinite(size) and size > 0 else None


def efficiency(options):
    value = options.get('ev_charge_efficiency', EV_CHARGE_EFFICIENCY)
    return value if type(value) in (int, float) and 0 < value <= 1 else EV_CHARGE_EFFICIENCY


class EvChargeOverride:
    """Single owner of the override record; methods run on the HA event loop."""

    def __init__(self, store, changed, *, now=utcnow):
        self.store, self.changed, self.now = store, changed, now
        self.lock = asyncio.Lock()
        self.value = empty()
        # What execution last asked live priority for; memory only.
        self.live = None

    async def load(self):
        saved = await self.store.async_load()
        if type(saved) is dict and set(saved) == set(empty()):
            self.value = saved

    @property
    def active(self):
        return self.value['active']

    def deadline(self):
        row = self.active
        return datetime.fromisoformat(row['deadline']) if row and row.get('deadline') else None

    async def _save(self, new):
        new['revision'] = self.value['revision'] + 1
        await self.store.async_save(new)
        self.value = new
        self.changed()

    async def change(self, field, value, *, soc, need_kwh, maximum_a, options):
        """Apply one user edit. SOC values are fractions; target is a percentage."""
        async with self.lock:
            if device_mode(options, 'ev') != 'controlling':
                raise ValueError('Set the car charger to Controlling to override its charging')
            new = deepcopy(self.value)
            row = new['active']
            now = self.now()
            if field == 'end':
                if row is None:
                    return self.value
                row['ending'] = True
            elif field == 'target':
                target = _percent(value)
                if not 0 <= target <= 100:
                    raise ValueError('Choose a charge level between 0 and 100%')
                if target <= soc * 100:
                    if row is None:
                        return self.value
                    row['ending'] = True
                elif row is None:
                    hours = new['hours']
                    fastest = fastest_hours(need_kwh(target / 100), maximum_a)
                    deadline = (now + timedelta(hours=hours)).isoformat() if hours and hours > fastest else None
                    new['active'] = dict(target_soc=target, deadline=deadline, started_at=now.isoformat(),
                                         saved_limit=None, raised_limit=None)
                    new['hours'] = None
                else:
                    row['target_soc'] = target
                    row.pop('ending', None)
            elif field == 'hours':
                if type(value) not in (int, float) or not isfinite(value) or value < 0:
                    raise ValueError('Enter a number of hours of at least 0')
                hours = ceil(value)
                if hours > 10_000:
                    raise ValueError('The duration is too large')
                if row is None:
                    new['hours'] = hours or None
                else:
                    fastest = fastest_hours(need_kwh(row['target_soc'] / 100), maximum_a)
                    row['deadline'] = (now + timedelta(hours=hours)).isoformat() if hours > fastest else None
            else:
                raise ValueError('Invalid charge override control')
            await self._save(new)
            return self.value

    async def record_limit(self, saved, raised):
        async with self.lock:
            if self.active is None:
                return
            new = deepcopy(self.value)
            new['active'].update(saved_limit=saved, raised_limit=raised)
            await self._save(new)

    async def clear(self):
        async with self.lock:
            if self.active is None:
                return
            new = deepcopy(self.value)
            new['active'] = None
            self.live = None
            await self._save(new)

    async def retire(self, options):
        """Leaving Controlling ends the override and writes nothing (control continuity)."""
        excluded = '$ev' in options.get('excluded_device_readings', ()) or not options.get('ev_enabled', True)
        if self.active is not None and (excluded or device_mode(options, 'ev') != 'controlling'):
            await self.clear()
