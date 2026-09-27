"""Ordered async receipt persistence and a closed, session-bound read protocol.

The host calls capture synchronously from source callbacks. It never awaits before
queuing an event. One worker persists in that order; failed persistence faults the
stream and prevents publication. There is no native-service or activation RPC here.
"""
import asyncio
from copy import deepcopy

from .command_transport import settled
from .gateway_journal import GatewayConflict


class GatewayStream:
    def __init__(self, journal, executor=asyncio.to_thread, *, capacity=1024):
        self.journal, self.executor = journal, executor
        if type(capacity) is not int or capacity < 1:
            raise ValueError('Positive receipt queue capacity required')
        self.queue = asyncio.Queue(maxsize=capacity)
        self.task = None
        self.failure = None
        self.accepting = False

    def start(self):
        if self.task is not None:
            raise RuntimeError('Receipt stream already started')
        self.accepting = True
        self.task = asyncio.create_task(self._run())

    def capture(self, kind, payload):
        """Assign local queue order in the source callback, including equal timestamps."""
        if not self.accepting or self.failure:
            raise GatewayConflict('Receipt stream unavailable; source coverage interrupted')
        # Copy now: mutable source attributes must not change a queued receipt.
        future = asyncio.get_running_loop().create_future()
        future.add_done_callback(lambda f: f.exception() if not f.cancelled() else None)
        self._enqueue(('record', (kind, deepcopy(payload)), future))
        return future

    async def call(self, operation, *args):
        if not self.accepting or self.failure:
            raise GatewayConflict('Receipt stream unavailable; reconnect after recovery')
        if operation not in ('begin', 'disconnect', 'read', 'acknowledge_delivery', 'snapshot'):
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
            self.failure = GatewayConflict('Receipt queue exhausted; source coverage interrupted')
            self.accepting = False
            raise self.failure from None

    async def _run(self):
        while True:
            item = await self.queue.get()
            try:
                if item is None:
                    return
                name, args, future = item
                if self.failure:
                    if not future.done(): future.set_exception(GatewayConflict('Receipt persistence failed'))
                    continue
                try:
                    result = await settled(self.executor, getattr(self.journal, name), *args)
                except (GatewayConflict, ValueError) as error:
                    if name == 'record':
                        self.failure = error
                        self.accepting = False
                    if not future.done(): future.set_exception(error)
                except Exception as error:
                    self.failure = error
                    self.accepting = False
                    if not future.done(): future.set_exception(error)
                else:
                    if not future.done():
                        if self.failure:
                            future.set_exception(GatewayConflict('Receipt stream faulted during persistence'))
                        else:
                            future.set_result(result)
            finally:
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
        fields = {'connect': {'identity', 'instance'}, 'receipts': {'after', 'limit'}, 'ack_delivery': {'through'}, 'snapshot': set()}
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
