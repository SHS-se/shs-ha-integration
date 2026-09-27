"""Authenticated, multiplexed HA gateway client with durable delivery receipts."""
import asyncio
from uuid import uuid4

import aiohttp

from shs_core.command_transport import settled
from shs_core.gateway_journal import GatewayConflict, GatewayRejected, validate_identity


class GatewayClient:
    def __init__(self, session, url, token, identity, inbox, *, instance=None, executor=asyncio.to_thread):
        self.http, self.url, self.token = session, url, token
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
            self.connected = await self._call('connect',{'identity':self.identity,'instance':self.instance})
            return self.connected
        except BaseException:
            await self.close()
            raise

    async def _read(self, socket):
        try:
            while True:
                reply = await socket.receive_json()
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
        except Exception:
            await self.close()

    async def _call(self, operation, body):
        future = asyncio.get_running_loop().create_future()
        future.add_done_callback(lambda f:f.exception() if not f.cancelled() else None)
        try:
            async with self.lock:
                if self.socket is None:
                    raise GatewayConflict('Gateway is disconnected')
                self.request_id += 1
                request_id = self.request_id
                self.pending[request_id] = future
                await self.socket.send_json({'id':request_id,'type':'shs_energy/gateway','operation':operation,'body':body})
            # A slow recorder query must not stop current observation delivery or
            # final command admission on this same authenticated socket.
            return await asyncio.shield(future)
        except GatewayRejected:
            raise
        except BaseException as error:
            await self.close()
            if isinstance(error,(aiohttp.ClientError,TypeError,ValueError,KeyError)):
                raise GatewayConflict('Gateway request failed; reconnect explicitly') from error
            raise

    async def call(self, operation, body):
        return await self._call(operation,body)

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
