"""Failed history stages retry independently and clear only on recovery."""
import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import unittest
from unittest.mock import AsyncMock, patch

sys.path.append(str(Path(__file__).parents[2] / 'tests'))
from household_fixture import Rig
from shs_app.engine import AppEngine
from shs_app.profiling import AppProfiler
from shs_core.api import ShsApiError
from shs_core.const import CATEGORIES


class HistoryRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_quarter_retries_failed_pricing_until_success_without_repeating_healthy_backend(self):
        start = datetime(2026, 10, 16, 10, tzinfo=timezone.utc)
        rigs = [Rig(now=start), Rig(now=start)]
        for rig in rigs:
            h = rig.household
            h._configured_entities = lambda: {key: [] for key in CATEGORIES}
            h._daily_changes = AsyncMock(return_value={})
            h._tariff_calculations = AsyncMock(return_value=([], None, False))
            h._supplier_daily_costs = AsyncMock(return_value=[])
            h.client.push_readings = AsyncMock(return_value={})
            h.async_scheduled_push = AsyncMock(wraps=h.async_scheduled_push)
            h.async_price_refresh = AsyncMock()
        failed, healthy = [rig.household for rig in rigs]
        costs = [{'date': '2026-10-15', 'import_cost_sek': 12}]
        failed._supplier_daily_costs.side_effect = [
            ShsApiError('price lookup failed'), ShsApiError('price lookup failed'), costs]
        engine = AppEngine.__new__(AppEngine)
        engine.household = failed
        engine.households = {'production': failed, 'test': healthy}
        engine.profiler = AppProfiler()
        engine.wake_projection = asyncio.Event()
        times = iter([5, 900, 905, 1800, 2700])
        checks = []

        async def next_time(_delay):
            checks.append(('pipeline:history_pricing' in {i['key'] for i in failed.attention_items},
                           failed.async_scheduled_push.await_count))
            try:
                elapsed = next(times)
            except StopIteration:
                raise asyncio.CancelledError
            for rig in rigs:
                rig.now = start + timedelta(seconds=elapsed)

        with patch('shs_app.engine.asyncio.sleep', side_effect=next_time):
            with self.assertRaises(asyncio.CancelledError):
                await engine.calendar()
        self.assertEqual(checks, [(True, 1), (True, 1), (True, 2), (True, 2), (False, 3), (False, 3)])
        healthy.async_scheduled_push.assert_awaited_once()
        failed.client.push_readings.assert_awaited_once_with([], [], costs)

    async def test_history_upload_failure_retries_even_when_pricing_is_healthy(self):
        rig = Rig(now=datetime(2026, 10, 16, 10, tzinfo=timezone.utc))
        h = rig.household
        h.async_price_refresh = AsyncMock()
        async def upload():
            h._pipeline_error('history_upload', ShsApiError('upload failed') if push.await_count == 1 else None)
        push = h.async_scheduled_push = AsyncMock(side_effect=upload)
        engine = AppEngine.__new__(AppEngine)
        engine.household = h
        engine.households = {'production': h}
        engine.profiler = AppProfiler()
        engine.wake_projection = asyncio.Event()
        async def next_quarter(_delay):
            if push.await_count == 2:
                raise asyncio.CancelledError
            rig.now += timedelta(minutes=15)
        with patch('shs_app.engine.asyncio.sleep', side_effect=next_quarter):
            with self.assertRaises(asyncio.CancelledError):
                await engine.calendar()
        self.assertFalse(h.attention_items)
        self.assertEqual(push.await_count, 2)
