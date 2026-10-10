"""Ordered async receipt persistence and a closed, session-bound read protocol.

The host calls capture synchronously from source callbacks. It never awaits before
queuing an event. One worker persists in that order; failed persistence faults the
stream and prevents publication. There is no native-service or activation RPC here.
"""
import asyncio
from copy import deepcopy
import logging

from .command_transport import settled
from .gateway_journal import GatewayConflict

_LOGGER = logging.getLogger(__name__)


class GatewayStream:
    def __init__(self, journal, executor=asyncio.to_thread, *, capacity=1024):
        self.journal, self.executor = journal, executor
        if type(capacity) is not int or capacity < 1:
            raise ValueError('Positive receipt queue capacity required')
        self.queue = asyncio.Queue(maxsize=capacity)
        self.task = None
        self.failure = None
        self.accepting = False
        self.listeners = set()
        self.metrics = {'fact_commits':0, 'facts':0, 'max_batch':0}

    def start(self):
        if self.task is not None:
            raise RuntimeError('Receipt stream already started')
        self.accepting = True
        self.task = asyncio.create_task(self._run())

    def capture(self, kind, payload, *, ownership=None):
        """Assign local queue order in the source callback, including equal timestamps."""
        if not self.accepting or self.failure:
            raise self.unavailable('source coverage interrupted') from self.failure
        # Copy now: mutable source attributes must not change a queued receipt.
        future = asyncio.get_running_loop().create_future()
        future.add_done_callback(lambda f: f.exception() if not f.cancelled() else None)
        args = (kind, deepcopy(payload)) if ownership is None else (kind, deepcopy(payload), deepcopy(ownership))
        self._enqueue(('record', args, future))
        return future

    async def call(self, operation, *args):
        if not self.accepting or self.failure:
            raise self.unavailable('reload the Home Assistant companion after correcting the cause') from self.failure
        if operation not in ('begin', 'disconnect', 'read', 'acknowledge_delivery', 'acknowledge_processed', 'snapshot',
                             'load_record', 'save_record', 'prepare_command', 'finish_command', 'admit_route', 'read_route', 'begin_operation', 'finish_operation', 'command_outcome', 'activate', 'resume'):
            raise ValueError('Unsupported gateway operation')
        future = asyncio.get_running_loop().create_future()
        future.add_done_callback(lambda f: f.exception() if not f.cancelled() else None)
        self._enqueue((operation, deepcopy(args), future))
        # Connection loss may cancel the request, never its durable journal work.
        return await asyncio.shield(future)

    def _enqueue(self, item):
        try:
            self.queue.put_nowait(item)
        except asyncio.QueueFull:
            self.fail(GatewayConflict('Receipt queue exhausted; source coverage interrupted'), item[0])
            raise self.failure from None

    def unavailable(self, reason):
        cause = f'; {type(self.failure).__name__}: {self.failure}' if self.failure else ''
        return GatewayConflict(f'Receipt stream unavailable; {reason}{cause}')

    def fail(self, error, operation):
        if self.failure is None:
            self.failure = error
            _LOGGER.error('SHS receipt stream failed during %s; queued=%s; %s: %s',
                operation, self.queue.qsize(), type(error).__name__, error,
                exc_info=(type(error), error, error.__traceback__))
        self.accepting = False

    async def _run(self):
        pending = None
        has_pending = False
        while True:
            item = pending if has_pending else await self.queue.get()
            has_pending = False
            batch = [item]
            try:
                if item is None:
                    return
                name, args, future = item
                if name == 'record':
                    while not self.queue.empty():
                        following = self.queue.get_nowait()
                        if following is None or following[0] != 'record':
                            pending, has_pending = following, True
                            break
                        batch.append(following)
                if self.failure:
                    for _, _, receiver in batch:
                        if not receiver.done(): receiver.set_exception(self.unavailable('receipt persistence failed'))
                    continue
                try:
                    if len(batch) > 1:
                        records = [values if len(values)==3 else (*values,None) for _,values,_ in batch]
                        results = await settled(self.executor, self.journal.record_many, records)
                    else:
                        results = [await settled(self.executor, getattr(self.journal, name), *args)]
                except (GatewayConflict, ValueError) as error:
                    if name == 'record':
                        self.fail(error, name)
                    for _, _, receiver in batch:
                        if not receiver.done(): receiver.set_exception(error)
                except Exception as error:
                    self.fail(error, name)
                    for _, _, receiver in batch:
                        if not receiver.done(): receiver.set_exception(error)
                else:
                    if name == 'record':
                        self.metrics['fact_commits'] += 1
                        self.metrics['facts'] += len(batch)
                        self.metrics['max_batch'] = max(self.metrics['max_batch'],len(batch))
                    if name in ('record', 'begin', 'disconnect', 'activate', 'finish_command'):
                        for listener in tuple(self.listeners):
                            listener()
                    for (_, _, receiver), result in zip(batch, results):
                        if not receiver.done():
                            if self.failure:
                                receiver.set_exception(self.unavailable('stream faulted during persistence'))
                            else:
                                receiver.set_result(result)
            finally:
                for _ in batch:
                    self.queue.task_done()

    async def close(self):
        self.accepting = False
        if self.task:
            await self.queue.put(None)
            await self.task
            self.task = None


class GatewayConnection:
    """One authenticated socket; a session cannot be borrowed from another socket."""
    def __init__(self, stream):
        self.stream = stream
        self.session = None
        self.closed = False
        self.lock = asyncio.Lock()

    async def request(self, value):
        # Settle a cancelled connect so its newly committed session can be revoked.
        task = asyncio.create_task(self._request(value))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            try:
                await task
            finally:
                await self.close()
            raise

    async def _request(self, value):
        async with self.lock:
            if self.closed:
                raise GatewayConflict('Gateway socket is closed')
            return await self._dispatch(value)

    async def _dispatch(self, value):
        if type(value) is not dict or set(value) != {'id', 'operation', 'body'} or type(value['id']) is not int or value['id'] < 1 or type(value['body']) is not dict:
            raise ValueError('Malformed gateway request')
        op, body = value['operation'], value['body']
        fields = {'connect': {'identity', 'instance'}, 'receipts': {'after', 'limit'}, 'ack_delivery': {'through'}, 'ack_processed':{'through'}, 'snapshot': set()}
        if type(op) is not str or op not in fields or set(body) != fields[op]:
            raise ValueError('Unsupported gateway request')
        if op == 'connect':
            if self.session is not None:
                raise GatewayConflict('Socket already has a session')
            result = await self.stream.call('begin', body['identity'], body['instance'])
            self.session = result['session']
        else:
            if self.session is None:
                raise GatewayConflict('Connect the exact migration/release pair first')
            if op == 'receipts': result = await self.stream.call('read', self.session, body['after'], body['limit'])
            elif op == 'ack_processed': result = {'processed': await self.stream.call('acknowledge_processed', self.session, body['through'])}
            elif op == 'ack_delivery': result = {'delivered': await self.stream.call('acknowledge_delivery', self.session, body['through'])}
            else: result = await self.stream.call('snapshot', self.session)
        return {'id': value['id'], 'result': result}

    async def close(self):
        async with self.lock:
            self.closed = True
            if self.session is not None:
                try:
                    await self.stream.call('disconnect', self.session)
                finally:
                    self.session = None


class GatewayRecord:
    """Local physical state is serialized with observations on the same worker."""
    def __init__(self, stream, name):
        self.stream, self.name = stream, name

    async def async_load(self):
        return await self.stream.call('load_record', self.name)

    async def async_save(self, value):
        await self.stream.call('save_record', self.name, value)


class GatewayCommands:
    """Bind CommandTransport to gateway commands without repurposing the source."""
    def __init__(self, stream):
        self.stream = stream

    def prepare(self, command):
        raise RuntimeError('Gateway commands must run on the receipt worker')

    def finish(self, command, status, reason=None):
        raise RuntimeError('Gateway commands must run on the receipt worker')

    async def execute(self, function, *args):
        if function == self.prepare:
            return await self.stream.call('prepare_command', *args)
        if function == self.finish:
            return await self.stream.call('finish_command', *args)
        raise ValueError('Unsupported gateway journal operation')


class GatewayOperations:
    def __init__(self, stream, session):
        self.stream, self.session = stream, session

    async def begin(self, operation):
        return await self.stream.call('begin_operation', self.session(), operation)

    async def finish(self, operation, result):
        await self.stream.call('finish_operation', operation, result)
