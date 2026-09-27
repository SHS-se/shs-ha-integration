"""Bounded operational measurements; detailed allocation tracing is opt-in."""
from __future__ import annotations

import asyncio
from collections import deque
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
from itertools import islice
from pathlib import Path
from time import monotonic, process_time, thread_time
import tracemalloc


def process_resources():
    """Linux process gauges, called in a worker. No heap traversal or GC."""
    status = dict(line.split(':', 1) for line in Path('/proc/self/status').read_text().splitlines())
    return {'process_cpu_seconds': process_time(),
            'rss_bytes': int(status['VmRSS'].split()[0]) * 1024,
            'rss_high_water_bytes': int(status['VmHWM'].split()[0]) * 1024,
            'swap_bytes': int(status['VmSwap'].split()[0]) * 1024,
            'threads': int(status['Threads'])}


class ResourceProfiler:
    """One integration lifetime, fixed operation vocabulary and two-hour ring.

    CPU belongs only to synchronous sections. An awaited operation is wall time
    only because other coroutines run on that same thread while it is suspended.
    """
    OPERATIONS = ('reduce', 'accounting_view', 'checkpoint_encode', 'checkpoint_save', 'refresh')
    ASYNC_OPERATIONS = frozenset(('checkpoint_save', 'refresh'))

    def __init__(self):
        self.started_at = datetime.now(timezone.utc).isoformat()
        self.started = monotonic()
        self.operations = {name: {'calls': 0, 'failures': 0, 'wall_ms': 0.,
            'max_wall_ms': 0., 'cpu_ms': 0., 'max_cpu_ms': 0.,
            'cpu_measured': name not in self.ASYNC_OPERATIONS} for name in self.OPERATIONS}
        self.samples = deque(maxlen=120)
        self.allocations = {'state': 'idle'}
        self._capture = None
        self._closed = False
        self._owns_tracer = False

    @contextmanager
    def measure(self, operation):
        totals = self.operations[operation]
        cpu = totals['cpu_measured']
        start = monotonic()
        cpu_start = thread_time() if cpu else None
        failed = False
        try:
            yield
        except BaseException:
            failed = True
            raise
        finally:
            wall = (monotonic() - start) * 1000
            totals['calls'] += 1
            totals['failures'] += int(failed)
            totals['wall_ms'] += wall
            totals['max_wall_ms'] = max(totals['max_wall_ms'], wall)
            if cpu_start is not None:
                elapsed = (thread_time() - cpu_start) * 1000
                totals['cpu_ms'] += elapsed
                totals['max_cpu_ms'] = max(totals['max_cpu_ms'], elapsed)

    def sample(self, process, retained):
        if self._closed:
            return
        elapsed = monotonic() - self.started
        process = dict(process)
        if self.samples and 'process_cpu_seconds' in process:
            previous = self.samples[-1]
            before = previous['process'].get('process_cpu_seconds')
            interval = elapsed - previous['elapsed_seconds']
            if before is not None and interval > 0:
                process['cpu_percent_one_core'] = 100 * (process['process_cpu_seconds'] - before) / interval
        self.samples.append({'at': datetime.now(timezone.utc).isoformat(),
            'elapsed_seconds': elapsed, 'process': process,
            'retained': dict(retained), 'operations': deepcopy(self.operations)})

    def snapshot(self, retained):
        return {'schema_version': 1, 'started_at': self.started_at,
            'elapsed_seconds': monotonic() - self.started,
            'basis': 'CPU is current-thread time in synchronous sections; async spans report wall time only. '
                     'Spans may overlap and must not be added. Process gauges include every integration. '
                     'Retained counts are record counts, not byte estimates. Storage worker CPU measures commits; '
                     'storage encoded bytes count JSON only, excluding typed SQL columns and physical disk writes. '
                     'Samples: latest 120, once per minute. '
                     'All profiler history resets on integration reload.',
            'operations': deepcopy(self.operations), 'retained': retained,
            'samples': deepcopy(list(self.samples)), 'allocations': deepcopy(self.allocations)}

    def start_allocations(self, seconds, run):
        """Start a single process-wide capture only if no other tracer owns it."""
        if self._closed:
            raise ValueError('Resource profiler is closed')
        if type(seconds) is not int or not 1 <= seconds <= 300:
            raise ValueError('Allocation capture must last 1–300 seconds')
        if (self._capture is not None and not self._capture.done()) or tracemalloc.is_tracing():
            raise ValueError('Allocation tracing is already active; existing tracing is left untouched')
        loop = asyncio.get_running_loop()
        tracemalloc.start(1)
        self._owns_tracer = True
        self.allocations = {'state': 'running', 'seconds': seconds,
            'started_at': datetime.now(timezone.utc).isoformat(),
            'basis': 'Process-wide allocations made after capture start and still alive at capture end; '
                     'does not measure pre-existing retained memory. Tracing adds overhead.'}
        self._capture = loop.create_task(self._capture_allocations(seconds, run))
        return deepcopy(self.allocations)

    async def _capture_allocations(self, seconds, run):
        try:
            await asyncio.sleep(seconds)
            # Cancellation waits for the snapshot worker before releasing the
            # process-wide tracer. No large snapshot is retained in diagnostics.
            worker = asyncio.ensure_future(run(self._allocation_summary))
            try:
                result = await asyncio.shield(worker)
            except asyncio.CancelledError:
                await worker
                raise
            self.allocations.update(state='complete', **result)
        except asyncio.CancelledError:
            self.allocations.update(state='cancelled')
            raise
        except Exception as error:
            self.allocations.update(state='failed', error=f'{type(error).__name__}: {error}')
        finally:
            tracemalloc.stop()
            self._owns_tracer = False

    @staticmethod
    def _allocation_summary():
        current, peak = tracemalloc.get_traced_memory()
        overhead = tracemalloc.get_tracemalloc_memory()
        snapshot = tracemalloc.take_snapshot()
        rows = snapshot.statistics('lineno')
        def describe(row):
            frame = row.traceback[0]
            return {'file': frame.filename, 'line': frame.lineno,
                    'bytes': row.size, 'count': row.count}
        return {'traced_current_bytes': current, 'traced_peak_bytes': peak,
                'tracer_overhead_bytes': overhead,
                'top_process_allocations': [describe(row) for row in rows[:20]],
                'top_shs_allocations': [describe(row) for row in
                    islice((row for row in rows
                        if '/shs_energy/' in row.traceback[0].filename), 20)]}

    async def close(self):
        self._closed = True
        if self._capture is not None and not self._capture.done():
            self._capture.cancel()
            await asyncio.gather(self._capture, return_exceptions=True)
        # A task cancelled before its first turn never enters its finally block.
        if self._owns_tracer:
            tracemalloc.stop()
            self._owns_tracer = False
            self.allocations.update(state='cancelled')
