"""Local, durable service transport. Policy remains in its two callers."""
import asyncio


class CommandNotSent(RuntimeError):
    pass


class CommandUncertain(RuntimeError):
    pass


async def settled(executor, function, *args):
    """Do not abandon an executor write when its awaiting task is cancelled."""
    future = asyncio.ensure_future(executor(function, *args))
    cancelled = False
    while not future.done():
        try:
            await asyncio.shield(future)
        except asyncio.CancelledError:
            cancelled = True
    result = future.result()
    if cancelled:
        raise asyncio.CancelledError
    return result


class CommandTransport:
    def __init__(self, journal, executor):
        self.journal, self.executor = journal, executor
        self.lock = asyncio.Lock()
        self.fault = None

    async def execute(self, command, *, authorize, send, timeout, on_sent=lambda: None):
        async with self.lock:
            if self.fault:
                raise CommandNotSent('Command journal failed; inspect storage and restart SHS') from self.fault
            started = False
            prepared = False
            try:
                try:
                    status = await settled(self.executor, self.journal.prepare, command)
                except asyncio.CancelledError:
                    # The worker has settled: it may have committed prepare, but
                    # this task has not entered the service coroutine.
                    prepared = True
                    raise
                except Exception as error:
                    self.fault = error
                    raise CommandNotSent('Could not persist command intent') from error
                if status != 'new':
                    if status in ('prepared', 'uncertain', 'service_returned'):
                        on_sent()
                    if status == 'service_returned':
                        return
                    if status == 'not_sent':
                        raise CommandNotSent('Recorded command was not sent')
                    raise CommandUncertain('Recorded command may already have been sent; it will not be replayed')
                prepared = True

                async def dispatch():
                    nonlocal started
                    # wait_for schedules this coroutine. Authorizing outside it
                    # leaves an event-loop turn for a permission/grant change.
                    authorize()
                    started = True
                    on_sent()
                    await send(*command.action.service_call())

                await asyncio.wait_for(dispatch(), timeout)
            except BaseException as error:
                if prepared:
                    await self._finish(command, 'uncertain' if started else 'not_sent', type(error).__name__)
                raise
            await self._finish(command, 'service_returned')

    async def _finish(self, command, status, reason=None):
        try:
            await settled(self.executor, self.journal.finish, command, status, reason)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.fault = error
            outcome = CommandNotSent if status == 'not_sent' else CommandUncertain
            raise outcome('Command outcome could not be persisted; inspect storage before restarting SHS') from error

    async def stopping(self):
        await settled(self.executor, self.journal.stopping)

    async def stopped(self):
        async with self.lock:
            if self.fault:
                raise RuntimeError('Cannot mark a faulted command transport clean') from self.fault
            await settled(self.executor, self.journal.stopped)
