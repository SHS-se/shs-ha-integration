"""Separate-process wire fixture; HA authentication/result envelope, real journal."""
import asyncio
import json
from pathlib import Path
import sys

from aiohttp import web
from shs_core.gateway_journal import GatewayJournal
from shs_core.gateway_stream import GatewayConnection, GatewayStream


async def serve():
    journal = GatewayJournal(sys.argv[1]).open()
    stream = GatewayStream(journal)
    stream.start()
    flag = Path(sys.argv[2])

    async def handler(request):
        if request.headers.get('Authorization') != 'Bearer fixture-token':
            return web.Response(status=401)
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        connection = GatewayConnection(stream)
        try:
            await ws.send_json({'type':'auth_required'})
            auth = await ws.receive_json()
            if auth != {'type':'auth', 'access_token':'fixture-token'}:
                await ws.send_json({'type':'auth_invalid'})
                return ws
            await ws.send_json({'type':'auth_ok'})
            async for frame in ws:
                value = json.loads(frame.data)
                request_id = value['id']
                assert value.pop('type') == 'shs_energy/gateway'
                try:
                    result = await connection.request(value)
                    reply = dict(type='result', success=True, **result)
                except ValueError as error:
                    reply = dict(id=request_id, type='result', success=False, error={'message':str(error)})
                if flag.exists() and flag.read_text() == 'pause' and value['operation'] == 'snapshot':
                    flag.unlink()
                    continue
                if flag.exists() and value['operation'] == 'ack_delivery':
                    flag.unlink()
                    await ws.close()  # committed ACK, lost reply
                    return ws
                await ws.send_json(reply)
        finally:
            await connection.close()
        return ws

    app = web.Application()
    app.router.add_get('/api/websocket', handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', 0)
    await site.start()
    print(site._server.sockets[0].getsockname()[1], flush=True)
    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()
        await stream.close()
        journal.close()


asyncio.run(serve())
