"""Wakeups must survive active work, duplicate delivery and reconnects."""
import asyncio
from pathlib import Path
import sys
import unittest

sys.path.append(str(Path(__file__).parents[1] / 'custom_components' / 'shs_energy'))
from replan_listener import listen_for_replans


class ReplanListenerTests(unittest.IsolatedAsyncioTestCase):
    async def test_waits_for_active_push_and_does_not_answer_duplicates(self):
        push = asyncio.Lock()
        await push.acquire()
        received = asyncio.Event()
        finished = asyncio.Event()
        answered = []
        calls = 0

        async def wait(after):
            nonlocal calls
            calls += 1
            if calls < 3:
                return 'one'
            self.assertEqual(after, 'one')
            finished.set()
            await asyncio.Future()

        async def answer(requested):
            received.set()
            async with push:
                answered.append(requested)

        task = asyncio.create_task(listen_for_replans(wait, answer, self.fail))
        await received.wait()
        self.assertEqual(answered, [])
        push.release()
        await asyncio.wait_for(finished.wait(), 1)
        self.assertEqual(answered, ['one'])
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

    async def test_reconnect_keeps_last_completed_id_and_cancel_stops_wait(self):
        calls = 0
        errors = []
        afters = []
        connected = asyncio.Event()

        async def wait(after):
            nonlocal calls
            calls += 1
            afters.append(after)
            if calls == 1:
                return 'one'
            if calls == 2:
                raise ConnectionError('disconnected')
            connected.set()
            await asyncio.Future()

        async def answer(_requested):
            pass

        task = asyncio.create_task(listen_for_replans(wait, answer, errors.append, retry_seconds=0))
        await asyncio.wait_for(connected.wait(), 1)
        self.assertEqual(afters, [None, 'one', 'one'])
        self.assertEqual(len(errors), 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
