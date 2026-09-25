"""Resource instrumentation remains bounded and never takes another tracer."""
import asyncio
from pathlib import Path
import sys
import tracemalloc
import unittest
from unittest.mock import patch

sys.path.append(str(Path(__file__).parents[1] / 'custom_components/shs_energy'))
from resource_profiling import ResourceProfiler


class ResourceMetricsTests(unittest.TestCase):
    def test_sync_cpu_failures_and_async_wall_have_distinct_meanings(self):
        profiler = ResourceProfiler()
        with patch('resource_profiling.monotonic', side_effect=[10, 12]), \
                patch('resource_profiling.thread_time', side_effect=[3, 3.25]):
            with self.assertRaises(ValueError), profiler.measure('reduce'):
                raise ValueError('bad event')
        self.assertEqual(profiler.operations['reduce']['cpu_ms'], 250)
        self.assertEqual(profiler.operations['reduce']['wall_ms'], 2000)
        self.assertEqual(profiler.operations['reduce']['failures'], 1)
        with patch('resource_profiling.thread_time', side_effect=AssertionError('CPU across await')):
            with profiler.measure('archive_save'):
                pass
        self.assertEqual(profiler.operations['archive_save']['cpu_ms'], 0)

    def test_samples_are_bounded_detached_and_include_growth_counters(self):
        profiler = ResourceProfiler()
        for index in range(150):
            profiler.sample({'rss_bytes': index}, {'meters': index * 10})
        report = profiler.snapshot({'meters': 1500})
        self.assertEqual(len(report['samples']), 120)
        self.assertEqual(report['samples'][0]['retained']['meters'], 300)
        report['samples'][0]['process']['rss_bytes'] = -1
        report['operations']['reduce']['calls'] = -1
        self.assertEqual(profiler.samples[0]['process']['rss_bytes'], 30)
        self.assertEqual(profiler.operations['reduce']['calls'], 0)


class AllocationCaptureTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self):
        if tracemalloc.is_tracing():
            tracemalloc.stop()

    async def test_immediate_unload_stops_capture_even_before_task_first_turn(self):
        profiler = ResourceProfiler()
        profiler.start_allocations(30, asyncio.to_thread)
        self.assertTrue(tracemalloc.is_tracing())
        await profiler.close()
        self.assertFalse(tracemalloc.is_tracing())
        with self.assertRaisesRegex(ValueError, 'closed'):
            profiler.start_allocations(1, asyncio.to_thread)

    async def test_another_profiler_is_never_replaced_or_stopped(self):
        tracemalloc.start()
        profiler = ResourceProfiler()
        with self.assertRaisesRegex(ValueError, 'already active'):
            profiler.start_allocations(1, asyncio.to_thread)
        await profiler.close()
        self.assertTrue(tracemalloc.is_tracing())

    async def test_capture_completes_with_bounded_source_locations_and_releases_tracer(self):
        profiler = ResourceProfiler()
        profiler.start_allocations(1, asyncio.to_thread)
        allocation = bytearray(20000)
        await profiler._capture
        self.assertEqual(len(allocation), 20000)
        self.assertEqual(profiler.allocations['state'], 'complete')
        self.assertLessEqual(len(profiler.allocations['top_process_allocations']), 20)
        self.assertLessEqual(len(profiler.allocations['top_shs_allocations']), 20)
        self.assertFalse(tracemalloc.is_tracing())
        await profiler.close()

    async def test_snapshot_failure_releases_tracer(self):
        profiler = ResourceProfiler()
        async def broken_run(_function):
            raise OSError('worker failed')
        profiler.start_allocations(1, broken_run)
        await profiler._capture
        self.assertEqual(profiler.allocations['state'], 'failed')
        self.assertFalse(tracemalloc.is_tracing())

    async def test_cancel_waits_for_in_flight_snapshot_worker(self):
        profiler = ResourceProfiler()
        started, finish = asyncio.Event(), asyncio.Event()
        async def run(_function):
            started.set()
            await finish.wait()
            self.assertTrue(tracemalloc.is_tracing())
            return {}
        profiler.start_allocations(1, run)
        await started.wait()
        closing = asyncio.create_task(profiler.close())
        await asyncio.sleep(0)
        self.assertFalse(closing.done())
        finish.set()
        await closing
        self.assertFalse(tracemalloc.is_tracing())
        self.assertEqual(profiler.allocations['state'], 'cancelled')
