"""Serial durable wakeups with independent wait/answer failures and bounded retries."""
import asyncio
from datetime import datetime, timedelta, timezone
from .connection_health import RetryBackoff


async def listen_for_replans(wait, answer, report_error, *, retry_seconds=5, progress=None):
    after = None
    retries = RetryBackoff(initial=retry_seconds)
    update_progress = progress or (lambda state, retry_at=None: None)
    while True:
        update_progress('connecting')
        try:
            requested = await wait(after)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            report_error(error)
            delay = retries.next()
            update_progress('waiting', (datetime.now(timezone.utc)+timedelta(seconds=delay)).isoformat())
            await asyncio.sleep(delay)
            continue
        update_progress('disconnecting')
        retries.reset()
        try:
            if requested and requested != after:
                await answer(requested)
                after = requested
        except asyncio.CancelledError:
            raise
        except Exception as error:
            # Answer failure belongs to replanning, never notification health.
            report_error(error)
            delay = retries.next()
            update_progress('waiting', (datetime.now(timezone.utc)+timedelta(seconds=delay)).isoformat())
            await asyncio.sleep(delay)
