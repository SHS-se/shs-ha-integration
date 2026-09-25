"""Async effect host for the durable home reducer; transport contains no economics.

Ports bind this host to an installation's commissioned adapter and exclusive grant
arbiter. A service acknowledgment is not a physical observation. There is no
schedule-command alternative when a policy is unavailable.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

if __package__:
    from . import home_runtime as runtime
    from .home_runtime_checkpoint import encode_checkpoint, restore_checkpoint, restore_state
    from .resource_profiling import ResourceProfiler
else:
    import home_runtime as runtime
    from home_runtime_checkpoint import encode_checkpoint, restore_checkpoint, restore_state
    from resource_profiling import ResourceProfiler


class DispatchRejected(Exception):
    """The transport port rejected a write before calling its service."""


@dataclass(frozen=True)
class HostPorts:
    persist: Callable[[bytes], Awaitable[None]]
    dispatch: Callable[[runtime.Send], Awaitable[None]]
    observe: Callable[[str], Awaitable[tuple[runtime.Event, ...]]]
    confirm_authority: Callable[[str], Awaitable[tuple[runtime.Event, ...]]]
    transition: Callable[[runtime.NeedTransition], Awaitable[runtime.Event]]
    renew: Callable[[runtime.HomeState, str], Awaitable[tuple[runtime.Event, ...]]]
    report: Callable[[str, str], None]
    now_ms: Callable[[], int]
    persist_state: Optional[Callable[[runtime.HomeState], Awaitable[None]]] = None


class HomeHost:
    """One serialized reducer; slow transport returns identity-bound events.

    The dispatch port must revalidate after any awaited household lock. Final
    grant validation and starting the transport must share the event-loop turn.
    The external arbiter must fence the old writer before confirming a grant.
    """
    def __init__(self, state: runtime.HomeState, ports: HostPorts, profiler=None):
        self.state = state
        self.ports = ports
        self._queue = asyncio.Queue()
        self._work = {}
        self._wake: Optional[asyncio.TimerHandle] = None
        self._wake_at = None
        self._closed = False
        self._runner = None
        self._fault = None
        self.profiler = profiler if profiler is not None else ResourceProfiler()

    async def start(self, checkpoint: Optional[bytes] = None, *, resume=False):
        if self._runner is not None or self._closed:
            raise RuntimeError("home host can only start once")
        effects = ()
        if checkpoint is not None:
            self.state, effects = restore_checkpoint(checkpoint, self.ports.now_ms())
        elif resume:
            self.state, effects = restore_state(self.state, self.ports.now_ms())
        self._runner = asyncio.create_task(self._run())
        await self._effects(effects)

    async def accept(self, event: runtime.Event):
        if self._closed or self._runner is None or self._fault:
            raise RuntimeError("home host is not running") from self._fault
        completion = asyncio.get_running_loop().create_future()
        self._queue.put_nowait((event, completion))
        return await completion

    def _enqueue(self, event):
        if not self._closed:
            self._queue.put_nowait((event, None))

    async def _run(self):
        while True:
            event, completion = await self._queue.get()
            try:
                with self.profiler.measure('reduce'):
                    state, effects = runtime.reduce_home(self.state, event, self.ports.now_ms())
                self.state = state
                await self._effects(effects)
                if self._fault is not None:
                    raise RuntimeError("home journal could not persist the transition") from self._fault
                if completion is not None and not completion.done():
                    completion.set_result(state)
            except Exception as error:
                # Invalid inputs are rejected atomically by the pure reducer.
                # Host failures are visible and must never start another writer.
                self.ports.report("home", f"{type(error).__name__}: {error}")
                if completion is not None and not completion.done():
                    completion.set_exception(error)
            finally:
                self._queue.task_done()

    def _launch(self, key, operation):
        if key in self._work:
            return
        async def work():
            try:
                events = await operation()
                for event in events:
                    self._enqueue(event)
            except Exception as error:
                self.ports.report("home", f"{key[0]} failed: {type(error).__name__}: {error}")
            finally:
                self._work.pop(key, None)
        self._work[key] = asyncio.create_task(work())

    async def _effects(self, effects):
        for effect in effects:
            if self._fault is not None:
                return
            if isinstance(effect, runtime.Persist):
                try:
                    if self.ports.persist_state:
                        await self.ports.persist_state(effect.state)
                    else:
                        await self.ports.persist(encode_checkpoint(effect.state))
                except Exception as error:
                    self._fault = error
                    self.ports.report("home", f"Checkpoint failed: {error}")
                    self._enqueue(runtime.JournalFailed(effect.state.revision))
                    # No other effects from this decision can dispatch after a
                    # failed durable write. The reducer retains issued effects.
                    return
                self._enqueue(runtime.JournalDurable(effect.state.revision))
            elif isinstance(effect, runtime.Send):
                self._launch(("send", effect.attempt_id), lambda e=effect: self._send(e))
            elif isinstance(effect, runtime.Observe):
                self._launch(("observe", effect.group_id), lambda e=effect: self.ports.observe(e.group_id))
            elif isinstance(effect, runtime.ConfirmAuthority):
                self._launch(("authority", effect.group_id), lambda e=effect: self.ports.confirm_authority(e.group_id))
            elif isinstance(effect, runtime.NeedTransition):
                self._launch(("transition", effect.group_id, effect.token), lambda e=effect: self._transition(e))
            elif isinstance(effect, runtime.NeedPlan):
                context = self.state.authority.identity if self.state.authority else None
                state = self.state
                self._launch(("plan", context), lambda e=effect, s=state: self.ports.renew(s, e.reason))
            elif isinstance(effect, runtime.WakeAt):
                if self._wake_at is None or effect.at_ms < self._wake_at:
                    if self._wake is not None:
                        self._wake.cancel()
                    self._wake_at = effect.at_ms
                    def wake():
                        self._wake_at = self._wake = None
                        self._enqueue(runtime.Tick())
                    self._wake = asyncio.get_running_loop().call_later(
                        max(0, (effect.at_ms - self.ports.now_ms()) / 1000), wake)
            elif isinstance(effect, runtime.Report):
                self.ports.report(effect.group_id, effect.reason)
            else:
                raise TypeError("unhandled home runtime effect")

    async def _transition(self, effect):
        try:
            # Start the adapter clock when its work actually starts, after
            # durability and queue waits. Admission still checks live identity
            # and observation expiry when the result reaches the reducer.
            async with asyncio.timeout(self.state.limits.transition_timeout_ms / 1000):
                proposal = await self.ports.transition(effect)
            return (proposal,)
        except TimeoutError:
            return (runtime.TransitionFailed(effect.group_id, effect.token, "retryable",
                "Battery command preparation timed out; it will be retried."),)
        except Exception as error:
            reason=f"Transition failed: {type(error).__name__}: {error}"
            self.ports.report(effect.group_id, reason)
            return (runtime.TransitionFailed(effect.group_id, effect.token, "unsupported",
                                             reason[:2000]),)

    async def _send(self, effect):
        if self._closed or self._fault is not None:
            return (runtime.TransportResult(effect.group_id, effect.attempt_id, "not_sent", "host_stopped"),)
        try:
            await self.ports.dispatch(effect)
        except DispatchRejected as error:
            self.ports.report(effect.group_id, f"Write not sent: {effect.key}: {error}")
            return (runtime.TransportResult(effect.group_id,effect.attempt_id,"not_sent","transport_final_fence"),)
        except (Exception, asyncio.CancelledError) as error:
            # Once transport has been invoked, an error/timeout/cancellation is
            # ambiguous. Never retry as if no physical write could have happened.
            self.ports.report(effect.group_id, f"Write uncertain: {effect.key}={effect.value}: {type(error).__name__}: {error}")
            return (runtime.TransportResult(effect.group_id, effect.attempt_id, "ambiguous", "host_transport_uncertain"),)
        return (runtime.TransportResult(effect.group_id, effect.attempt_id, "accepted", "ha_service_completed"),)

    async def idle(self):
        """Drain current work in deterministic tests, without waiting for timers."""
        while True:
            await self._queue.join()
            pending = tuple(self._work.values())
            if not pending and self._queue.empty():
                return
            await asyncio.gather(*pending)

    def resource_counts(self):
        return {'queued_events': self._queue.qsize(), 'active_effects': len(self._work)}

    async def close(self):
        self._closed = True
        if self._wake:
            self._wake.cancel()
        # Do not erase or reclassify in-flight writes on unload. Their durable
        # sent records are restored as ambiguous and re-observed on restart.
        for task in tuple(self._work.values()):
            task.cancel()
        await asyncio.gather(*tuple(self._work.values()), return_exceptions=True)
        if self._runner:
            self._runner.cancel()
            await asyncio.gather(self._runner, return_exceptions=True)
        while not self._queue.empty():
            _, completion = self._queue.get_nowait()
            if completion is not None and not completion.done():
                completion.set_exception(RuntimeError("home host closed"))
            self._queue.task_done()
