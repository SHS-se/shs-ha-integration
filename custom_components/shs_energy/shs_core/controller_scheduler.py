"""Device-scoped event scheduling with one-shot deadlines and no idle sweep."""
import asyncio
from datetime import datetime, timedelta, timezone
from time import perf_counter
from .source_admission import pool_paused, pool_temperature_sources, temperature_available, temperature_metadata


class ControllerScheduler:
    def __init__(self, controller, subscribe, at, spawn, *, now=None):
        self.controller = controller
        self.subscribe, self.at, self.spawn = subscribe, at, spawn
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.watchers = {}
        self.dependencies = {}
        self.reads = {}
        self.metadata = {}
        self.deadlines = {}
        self.pending = {}
        self.reasons = set()
        self.dispatched = {}
        self.task = None
        self.sample_task = None
        self.paused = False
        self.closed = False
        controller.scheduler = self

    def request(self, reason, devices=None):
        self.controller.observation_changed.set()
        if self.paused or self.closed or not self.controller.initialized:
            return
        stats = self.controller.metrics.scheduling
        stats["notifications"] += 1
        stats["queued_while_busy"] += int(self.controller.lock.locked())
        stats["coalesced_notifications"] += int(bool(self.pending))
        received = perf_counter()
        for device in (devices if devices is not None else (None,)):
            self.pending.setdefault(device, received)
        self.reasons.add(reason)
        if self.task is None or self.task.done():
            self.task = self.spawn(self._drain())

    async def _drain(self):
        # Combine the state/direction reports delivered in the same loop turn.
        await asyncio.sleep(0)
        while self.pending and not self.paused:
            # Unlike the old timer, events received while busy are not dropped.
            async with self.controller.lock:
                pass
            if self.paused:
                break
            self.dispatched, self.pending = self.pending, {}
            reasons, self.reasons = self.reasons, set()
            devices = None if None in self.dispatched else set(self.dispatched)
            trigger = next(iter(reasons)) if len(reasons) == 1 else "coalesced"
            try:
                await self.controller.async_tick(trigger=trigger, devices=devices)
            finally:
                self.dispatched = {}

    def begin_device(self, device):
        self.reads[device] = set()
        times = [self.dispatched[key] for key in (None, device) if key in self.dispatched]
        if times:
            elapsed = (perf_counter() - min(times)) * 1000
            stats = self.controller.metrics.scheduling
            stats["device_dispatches"] += 1
            stats["dispatch_latency_ms"] += elapsed
            stats["max_dispatch_latency_ms"] = max(stats["max_dispatch_latency_ms"], elapsed)

    def observe(self, device, entity, state):
        if self.closed or not entity:
            return
        self.reads.setdefault(device, set()).add(entity)
        self.dependencies.setdefault(device, set()).add(entity)
        if entity not in self.watchers:
            self.watchers[entity] = self.subscribe(entity, self.entity_event)
            self.metadata[entity] = temperature_metadata(state)

    def entity_event(self, entity, state, kind):
        if self.closed:
            return
        stats = self.controller.metrics.scheduling
        stats[kind + "_events"] += 1
        self.controller.observation_changed.set()
        affected = {device for device, entities in self.dependencies.items() if entity in entities}
        controller = self.controller
        metadata = temperature_metadata(state)
        metadata_changed = self.metadata.get(entity) != metadata
        self.metadata[entity] = metadata
        if ('pool' in affected and temperature_available(state) and not metadata_changed
                and entity in pool_temperature_sources(controller.options(), controller.inputs.read, controller.inputs.platform)
                and pool_paused(controller.status.get('pool',{}), controller.coordinator.current_plan_slot,
                    controller.coordinator.optimisation_plan, controller.inputs.read, controller.ownership)):
            affected.discard('pool')
        # Unchanged reports wake an active confirmer; only state changes need
        # a new decision. Source timestamps never expire an available value.
        targets = affected if kind != "state_report" else set()
        if targets:
            self.request("state_report" if kind == "state_report" else "state_change", targets)

    def end_device(self, device):
        old = self.dependencies.get(device, set())
        current = self.reads.pop(device, set())
        # A suppressed permanent failure still needs its recovery dependencies.
        if not current and device in self.controller.failed:
            current = old
        self.dependencies[device] = current
        in_use = set().union(*self.dependencies.values())
        for entity in set(self.watchers) - in_use:
            self.watchers.pop(entity)()
        if not self.controller.ownership.records.get(device, {}).get("restoration_pending"):
            self.cancel((device, "restoration_retry"))


    def retain_devices(self, devices):
        for device in set(self.dependencies) - devices:
            self.reads[device] = set()
            self.controller.failed.pop(device, None)
            self.end_device(device)
            self.dependencies.pop(device, None)
            for key in list(self.deadlines):
                if key[0] == device:
                    self.cancel(key)

    def _schedule(self, key, when, action):
        if self.paused or self.closed:
            return
        existing = self.deadlines.get(key)
        if existing and existing[0] == when:
            return
        self.cancel(key)

        def due(_now=None):
            self.deadlines.pop(key, None)
            if not self.paused:
                action()

        self.deadlines[key] = (when, self.at(when, due))

    def cancel(self, key):
        item = self.deadlines.pop(key, None)
        if item:
            item[1]()

    def device_deadline(self, device, name, when):
        if when is None:
            self.cancel((device, name))
        elif when > self.now():
            def due():
                self.controller.failed.pop(device, None)
                self.request("restoration_retry" if name == "restoration_retry" else "device_deadline", {device})
            self._schedule((device, name), when, due)

    def plan_deadlines(self):
        now = self.now()
        quarter = now.replace(minute=now.minute // 15 * 15, second=0, microsecond=0)

        def next_slot():
            self.request("slot_boundary")
            self.plan_deadlines()

        self._schedule((None, "slot"), quarter + timedelta(minutes=15), next_slot)
        self.cancel((None, "expiry"))
        plan = self.controller.coordinator.optimisation_plan or {}
        try:
            expires = datetime.fromisoformat(plan["valid_until"].replace("Z", "+00:00"))
            if expires > now:
                self._schedule((None, "expiry"), expires, lambda: self.request("plan_expiry"))
        except (KeyError, TypeError, ValueError):
            # The controller's contract validator owns invalid-plan reporting.
            pass

    def sample_deadline(self):
        # This timer only samples observations; it never requests a control tick.
        async def sample():
            try:
                await self.controller.async_sample_diagnostics()
            finally:
                self.sample_deadline()

        def due():
            self.sample_task = self.spawn(sample())

        self._schedule((None, "observations"), self.now() + timedelta(seconds=60), due)

    def coordinator_updated(self):
        self.plan_deadlines()
        self.request("coordinator_update")

    def pause(self):
        self.paused = True
        self.pending.clear()
        for key in list(self.deadlines):
            self.cancel(key)
        self.controller.observation_changed.set()

    def close(self):
        self.pause()
        self.closed = True
        if self.sample_task is not None and not self.sample_task.done():
            self.sample_task.cancel()
        for unsubscribe in self.watchers.values():
            unsubscribe()
        self.watchers.clear()
        self.dependencies.clear()
