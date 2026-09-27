"""Authenticated HA gateway receipt client, separate from browser Ingress.

This client never activates an engine. It persists receipts before acknowledging
delivery, and leaves domain processing watermarks in execution storage.
"""
import asyncio
from uuid import uuid4

import aiohttp

from shs_core.command_transport import settled
from shs_core.gateway_journal import GatewayConflict, validate_identity


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

    async def connect(self):
        if self.socket is not None:
            raise RuntimeError('Gateway connection already open')
        socket = await self.http.ws_connect(self.url, headers={'Authorization': 'Bearer '+self.token})
        self.socket = socket
        try:
            # Home Assistant authenticates the WebSocket independently of HTTP.
            challenge = await socket.receive_json()
            if challenge.get('type') != 'auth_required':
                raise GatewayConflict('Expected HA WebSocket authentication challenge')
            await socket.send_json({'type': 'auth', 'access_token': self.token})
            if (await socket.receive_json()).get('type') != 'auth_ok':
                raise GatewayConflict('Gateway authentication rejected')
            self.connected = await self._call('connect', {'identity': self.identity, 'instance': self.instance})
            return self.connected
        except BaseException:
            await self.close()
            raise

    async def _call(self, operation, body):
        async with self.lock:
            if self.socket is None:
                raise GatewayConflict('Gateway is disconnected')
            socket = self.socket
            self.request_id += 1
            try:
                await socket.send_json({'id': self.request_id, 'type': 'shs_energy/gateway', 'operation': operation, 'body': body})
                reply = await socket.receive_json()
                if type(reply) is not dict or reply.get('id') != self.request_id or reply.get('type') != 'result':
                    raise GatewayConflict('Unexpected gateway reply')
                if reply.get('success') is not True:
                    raise GatewayConflict('Gateway rejected the request')
                return reply['result']
            except BaseException as error:
                # After cancellation/reply loss, this socket cannot safely correlate
                # another request. Recovery always negotiates a new session.
                await self.close()
                if isinstance(error, (aiohttp.ClientError, TypeError, ValueError, KeyError)):
                    raise GatewayConflict('Gateway request failed; reconnect explicitly') from error
                raise

    async def receive(self, limit=256):
        async with self.receiving:
            after = await settled(self.executor, self.inbox.through)
            page = await self._call('receipts', {'after': after, 'limit': limit})
            through = await settled(self.executor, self.inbox.receive, page)
            await self._call('ack_delivery', {'through': through})
            return page

    async def snapshot(self):
        return await self._call('snapshot', {})

    async def close(self):
        socket, self.socket = self.socket, None
        self.connected = None
        if socket is not None:
            await socket.close()
