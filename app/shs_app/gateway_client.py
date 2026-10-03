"""Authenticated, multiplexed HA gateway client with durable delivery receipts."""
import asyncio
from contextlib import asynccontextmanager
import json
import logging
from hashlib import sha256
from uuid import uuid4

import aiohttp

from shs_core.command_transport import settled
from shs_core.gateway_journal import GatewayConflict, GatewayRejected, validate_identity
from shs_wire.protocol import admit, hello
from .profiling import AppProfiler, profiled


LOGGER = logging.getLogger(__name__)


class GatewayClient:
    def __init__(self, session, url, token, identity, inbox, *, instance=None, executor=asyncio.to_thread, paired_release=None, profiler=None):
        self.profiler = profiler if profiler is not None else AppProfiler()
        self.http, self.url, self.token = session, url, token
        self.paired_release = paired_release
        self.publishing = asyncio.Lock()
        self.identity, self.inbox = validate_identity(identity), inbox
        self.instance = instance or uuid4().hex
        self.executor = executor
        self.socket = None
        self.connected = None
        self.request_id = 0
        self.lock = asyncio.Lock()
        self.receiving = asyncio.Lock()
        self.pending = {}
        self.reader = None
        self.configuration_writer = asyncio.Lock()
        self.admission = asyncio.Condition()
        self.installing_configuration = False
        self.admitted_calls = 0

    @asynccontextmanager
    async def configuration_boundary(self, operation, body):
        installing = operation == 'source' and body.get('operation') == 'configure'
        if not installing and (operation == 'source' or operation in
                ('connect','receipts','ack_delivery','ack_processed','snapshot','reconcile','activate','resume','updates')):
            # Recorder/history requests and receipt delivery remain multiplexed.
            yield
            return
        if installing:
            async with self.configuration_writer:
                try:
                    async with self.admission:
                        self.installing_configuration = True
                        await self.admission.wait_for(lambda: self.admitted_calls == 0)
                    yield
                finally:
                    async with self.admission:
                        self.installing_configuration = False
                        self.admission.notify_all()
        else:
            async with self.admission:
                await self.admission.wait_for(lambda: not self.installing_configuration)
                self.admitted_calls += 1
            try:
                yield
            finally:
                async with self.admission:
                    self.admitted_calls -= 1
                    self.admission.notify_all()

    async def connect(self):
        if self.socket is not None:
            raise RuntimeError('Gateway connection already open')
        socket = await self.http.ws_connect(self.url, headers={'Authorization':'Bearer '+self.token}, max_msg_size=32*1024*1024)
        self.socket = socket
        try:
            challenge = await socket.receive_json()
            if challenge.get('type') != 'auth_required':
                raise GatewayConflict('Expected HA WebSocket authentication challenge')
            await socket.send_json({'type':'auth','access_token':self.token})
            if (await socket.receive_json()).get('type') != 'auth_ok':
                raise GatewayConflict('Gateway authentication rejected')
            self.reader = asyncio.create_task(self._read(socket))
            requirement = hello(self.paired_release['app_version'])
            body = {'identity':self.identity,'instance':self.instance,'contract':requirement}
            self.connected = await self._call('connect',body)
            admit(requirement, self.connected.get('contract'))
            return self.connected
        except BaseException:
            await self.close()
            raise

    async def _read(self, socket):
        try:
            while True:
                reply = await socket.receive_json(loads=self.decode)
                if type(reply) is not dict or reply.get('type') != 'result' or reply.get('id') not in self.pending:
                    raise GatewayConflict('Unexpected gateway reply')
                future = self.pending.pop(reply['id'])
                if reply.get('success') is True:
                    future.set_result(reply['result'])
                elif reply.get('error',{}).get('code') == 'shs_gateway_rejected':
                    future.set_exception(GatewayRejected(reply['error'].get('message','Gateway rejected the request')))
                else:
                    future.set_exception(GatewayConflict('Gateway session rejected; reconnect explicitly'))
                    raise GatewayConflict('Gateway session is no longer current')
        except asyncio.CancelledError:
            raise
        except Exception as error:
            LOGGER.warning('HA gateway socket closed: %s: %s', type(error).__name__, error)
            await self.close()

    def encode(self, value):
        with self.profiler.measure('gateway_encode'):
            return json.dumps(value)

    def decode(self, content):
        with self.profiler.measure('gateway_decode'):
            return json.loads(content)

    @profiled('gateway_exchange')
    async def _call(self, operation, body):
        # HA revokes command admission while it durably captures a new native
        # configuration. Finish admitted calls first, then keep app operations
        # outside that interval until HA acknowledges the installed revision.
        async with self.configuration_boundary(operation, body):
            return await self._exchange(operation, body)

    async def _exchange(self, operation, body):
        future = asyncio.get_running_loop().create_future()
        future.add_done_callback(lambda f:f.exception() if not f.cancelled() else None)
        try:
            async with self.lock:
                if self.socket is None:
                    raise GatewayConflict('Gateway is disconnected')
                self.request_id += 1
                request_id = self.request_id
                self.pending[request_id] = future
                await self.socket.send_json({'id':request_id,'type':'shs_energy/gateway','operation':operation,'body':body},dumps=self.encode)
            # A slow recorder query must not stop current observation delivery or
            # final command admission on this same authenticated socket.
            return await asyncio.shield(future)
        except (GatewayRejected, asyncio.CancelledError):
            # Replies are correlated by request identity, so the reader settles
            # an abandoned request whenever HA answers it. A caller that stops
            # waiting (a bounded battery preparation, an ending task) is not a
            # transport failure: closing here would revoke HA's battery grant and
            # stop every other job on this session.
            raise
        except BaseException as error:
            await self.close()
            if isinstance(error,(aiohttp.ClientError,TypeError,ValueError,KeyError)):
                raise GatewayConflict('Gateway request failed; reconnect explicitly') from error
            raise

    async def call(self, operation, body):
        return await self._call(operation,body)

    async def project(self, value):
        with self.profiler.measure('gateway_encode'):
            content = json.dumps(value,separators=(',',':'),allow_nan=False)
            digest, transfer = sha256(content.encode()).hexdigest(), uuid4().hex
        if LOGGER.isEnabledFor(logging.DEBUG):
            LOGGER.debug('Publishing HA projection: bytes=%s chunks=%s',len(content.encode()),(len(content)+256*1024-1)//(256*1024))
        async with self.publishing:
            for index, offset in enumerate(range(0,len(content),256*1024)):
                chunk = content[offset:offset+256*1024]
                await self._call('projection_chunk',dict(transfer=transfer,index=index,
                    last=offset+len(chunk)==len(content),data=chunk,sha256=digest))

    async def receive(self, limit=256):
        async with self.receiving:
            after = await settled(self.executor,self.inbox.through)
            page = await self._call('receipts',{'after':after,'limit':limit})
            through = await settled(self.executor,self.inbox.receive,page)
            await self._call('ack_delivery',{'through':through})
            return page

    async def snapshot(self):
        return await self._call('snapshot',{})

    async def close(self):
        socket,self.socket = self.socket,None
        self.connected = None
        reader,self.reader = self.reader,None
        for future in self.pending.values():
            if not future.done():future.set_exception(GatewayConflict('Gateway disconnected; reconcile a new session'))
        self.pending.clear()
        if reader is not None and reader is not asyncio.current_task():
            reader.cancel()
            await asyncio.gather(reader,return_exceptions=True)
        if socket is not None:await socket.close()
