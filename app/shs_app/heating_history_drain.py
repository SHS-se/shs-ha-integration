"""Drain the existing durable native-history cursor without requesting plans."""
import asyncio
from contextlib import suppress
from copy import copy, deepcopy
from datetime import datetime, timedelta, timezone

from shs_core.api import ShsApiClient
from shs_core.connection_health import RetryBackoff
from shs_core.heating_history import accepted_heating_cursor, probe_repair_windows
from shs_core.optimisation import quarter_start


SOURCE_FIELDS = ('heating_history_cursor', 'heating_history_sources',
                 'heating_power_peaks_supported', 'counter_peak_evidence')


class CaptureSuperseded(Exception):
    """Another source owner advanced while this read or upload was in flight."""


class CaptureRecord:
    """Merge only native counter evidence, never a stale whole household record."""
    def __init__(self, drain, revision, stored):
        self.drain, self.revision = drain, revision
        self.baseline = {key:deepcopy(stored.get(key)) for key in SOURCE_FIELDS}

    async def current(self):
        stored = await self.drain.household._store.async_load() or {}
        if (self.revision != self.drain.revision()
                or any(stored.get(key) != value for key,value in self.baseline.items())):
            raise CaptureSuperseded()
        return stored

    async def async_save(self, captured):
        # The shared collector persists its counter qualification before reading
        # derived rows. Preserve that ordering with a short, source-fenced merge.
        h = self.drain.household
        async with h._push_lock:
            stored = await self.current()
            stored['counter_peak_evidence'] = deepcopy(captured['counter_peak_evidence'])
            await h._store.async_save(stored)
            self.baseline['counter_peak_evidence'] = deepcopy(stored['counter_peak_evidence'])


class HeatingHistoryDrain:
    def __init__(self, household, revision, report_error, critical_error):
        self.household = household
        self.revision = revision
        self.report_error = report_error
        self.critical_error = critical_error
        self.wake = asyncio.Event()
        self.foreground = 0
        self.checked_revision = None
        self.backoff = RetryBackoff()

    def pending(self, stored, end):
        cursor = stored.get('heating_history_cursor') or {}
        if probe_repair_windows(deepcopy(cursor.get('probe_evidence') or {})):
            return True
        if stored.get('heating_history_sources') is None:
            return False  # Historical source ownership has not arrived yet.
        rooms = cursor.get('rooms') or {}
        if not rooms:
            return self.checked_revision != self.revision()
        earliest = end.replace(minute=0, second=0, microsecond=0) - timedelta(days=400)
        for room in rooms.values():
            before = room.get('before')
            if not before or datetime.fromisoformat(before).astimezone(timezone.utc) > earliest:
                return True
        return False

    async def plan_exchange(self):
        # The core periodic poll intentionally skips a busy push lock. Give it
        # priority before waiting for a short record merge, so repair cannot lose that tick.
        self.foreground += 1
        try:
            async with self.household._push_lock:
                pass
            await self.household.async_replan_poll()
        finally:
            self.foreground -= 1
            self.wake.set()

    async def step(self):
        try:
            return await self._step()
        except CaptureSuperseded:
            return True

    async def _step(self):
        h = self.household
        if self.foreground or h._recovering or h._push_lock.locked():
            return None
        async with h._push_lock:
            revision = self.revision()
            stored = await h._store.async_load() or {}
            end = quarter_start(h.ports.utcnow())
            if revision == self.checked_revision and not self.pending(stored, end):
                return False
            record = CaptureRecord(self, revision, stored)
        # The existing collector only reads ports and saves counter evidence.
        # A shallow view replaces that save owner without mutating the live
        # Household; execution and foreground cloud operations keep their store.
        capture = copy(h)
        capture._store = record
        async with asyncio.timeout(8):
            devices = await capture._prepared_device_inventory(stored)
            rows, probes, pending = await capture._heating_history_upload(
                h.resolved_options(), devices, end, stored.get('heating_history_cursor'),
                lineage=stored.get('heating_history_sources'), stored=stored)
        async with h._push_lock:
            await record.current()
        if self.foreground:
            return True
        if pending is None:
            self.checked_revision = revision
            return False
        body = ShsApiClient.prepare_optimisation_submission([], heating_observations=rows,
            heating_probe_observations=probes)
        async with asyncio.timeout(20):
            result = await h.client.submit_optimisation(body)
        async with h._push_lock:
            stored = await record.current()
            accepted = accepted_heating_cursor(result, rows, pending,
                stored.get('heating_history_cursor'), probe_observations=probes)
            if accepted is not None:
                stored['heating_history_cursor'] = accepted
            if result.get('heating_power_peaks_supported') is True:
                stored['heating_power_peaks_supported'] = True
            await h._store.async_save(stored)
        complete = (accepted is not None and accepted.get('rooms') == pending.get('rooms')
            and type(result.get('heating_observations_accepted')) is int
            and result['heating_observations_accepted'] == len(rows or [])
            and type(result.get('heating_probe_observations_accepted')) is int
            and result['heating_probe_observations_accepted'] == len(probes or [])
            and not pending.get('probe_capture', {}).get('read_failed_windows')
            and not pending.get('deferred_rooms'))
        if not complete:
            raise RuntimeError('Some heating history could not be read or confirmed by the server; '
                               'SHS will retry automatically. Planning and execution continue.')
        self.checked_revision = revision
        return self.pending(stored, end)

    async def run(self):
        while True:
            self.wake.clear()
            try:
                pending = await self.step()
            except Exception as error:
                self.report_error(error)
                if self.critical_error(error):
                    raise
                # A wake cannot defeat failure backoff and create a hot retry loop.
                await asyncio.sleep(self.backoff.next())
                continue
            if pending is not None:
                self.report_error(None)
                self.backoff.reset()
            with suppress(TimeoutError):
                await asyncio.wait_for(self.wake.wait(), 1 if pending else 60)
