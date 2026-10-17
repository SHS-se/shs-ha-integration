"""Observed connection outages, independent of retry cadence and device control."""
import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import logging
from time import monotonic

WARNING_SECONDS = 3600
_LOGGER = logging.getLogger(__name__)


class ConnectionHealth:
    def __init__(self, store, publish, *, clock=monotonic, call_later=None):
        self.store, self.publish, self.clock = store, publish, clock
        self.call_later = call_later
        self.episodes, self.anchors, self.timers = {}, {}, {}
        self.state = {'state': 'connecting', 'retry_at': None}

    async def restore(self):
        self.episodes = await self.store.async_load() or {}

    def elapsed(self, lane):
        episode = self.episodes[lane]
        return episode['observed_seconds'] + (self.clock()-self.anchors[lane] if lane in self.anchors else 0)

    def persistent(self, lane):
        return lane in self.episodes and self.elapsed(lane) >= WARNING_SECONDS

    def _save(self):
        self.store.async_delay_save(self.checkpoint, 0)

    def checkpoint(self):
        return {lane: {**deepcopy(value), 'observed_seconds': self.elapsed(lane)}
                for lane, value in self.episodes.items()}

    def failed(self, lane, error):
        _LOGGER.warning('%s connection failed: %s', lane, error)
        if lane not in self.episodes:
            self.episodes[lane] = dict(observed_seconds=0, first_failure_at=datetime.now(timezone.utc).isoformat(), detail=str(error))
        self.episodes[lane]['detail'] = str(error)
        if lane not in self.anchors:
            self.anchors[lane] = self.clock()
            remaining = max(0, WARNING_SECONDS-self.elapsed(lane))
            schedule = self.call_later or asyncio.get_running_loop().call_later
            self.timers[lane] = schedule(remaining, self.publish)
        self._save()
        self.publish()

    def verified(self, lane):
        if lane not in self.episodes:
            return
        _LOGGER.info('%s connection recovered after %.1fs of observed failure', lane, self.elapsed(lane))
        self.episodes.pop(lane)
        self.anchors.pop(lane, None)
        if timer := self.timers.pop(lane, None):
            timer.cancel()
        self._save()
        self.publish()

    def visible_error(self, detail):
        return None if any(value["detail"] == detail and not self.persistent(lane)
                           for lane, value in self.episodes.items()) else detail

    def progress(self, state, retry_at=None):
        self.state = dict(state=state, retry_at=retry_at)
        self.publish()

    def snapshot(self, lane='notifications'):
        return {**self.state, 'persistent': self.persistent(lane), 'warning_after_seconds': WARNING_SECONDS}

    async def close(self):
        for timer in self.timers.values():
            timer.cancel()
        self.timers.clear()
        self.episodes = self.checkpoint()
        self.anchors.clear()
        await self.store.async_save(self.episodes)


class RetryBackoff:
    def __init__(self, initial=5, maximum=60, jitter=None):
        from random import uniform
        self.initial, self.maximum = initial, maximum
        self.jitter = jitter or uniform
        self.reset()

    def reset(self):
        self.ceiling = self.initial

    def next(self):
        delay = self.jitter(self.ceiling*.8, self.ceiling)
        self.ceiling = min(self.maximum, self.ceiling*2)
        return delay
