"""Await cloud wakeups serially; the durable request ID survives reconnects."""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable


async def listen_for_replans(
    wait: Callable[[str | None], Awaitable[str | None]],
    answer: Callable[[str], Awaitable[object]],
    report_error: Callable[[Exception], None],
    *,
    retry_seconds: float = 5,
) -> None:
    after: str | None = None
    while True:
        try:
            requested = await wait(after)
            if requested and requested != after:
                await answer(requested)
                after = requested
        except asyncio.CancelledError:
            raise
        except Exception as error:
            report_error(error)
            # Reconnect the same transport; never substitute a stale snapshot.
            await asyncio.sleep(retry_seconds)
