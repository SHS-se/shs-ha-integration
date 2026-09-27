"""Authenticated session authority and the closed app/device boundary.

Host adapters provide source reads and canonical configuration. Only this service
can activate a socket, admit policy revisions or reach physical owners.
"""
import asyncio
from contextvars import ContextVar
from copy import deepcopy
from uuid import uuid4

from .gateway_journal import GatewayConflict, digest
from .gateway_stream import GatewayConnection


class GatewayService:
    def __init__(self, stream, identity, source):
        self.stream, self.identity, self.source = stream, identity, source
        self.connection = None
        self.current = ContextVar('shs_gateway_socket', default=None)
        self.configuration_revision = 0
        self.configuration_pending = True
        self.policy_revision = 0
        self.policy = None
        self.active = False
        self.physical = None
        self.battery = None
        self.reconciliation = None
        self.pending = {}
        self.projection = None
        self.closed = False
        self.obligation_event = asyncio.Event()
        self.obligation_task = None
        self.obligation_error = None

    def session(self):
        return self.connection.session if self.connection else None

    def context(self):
        return dict(session=self.session(), configuration_revision=self.configuration_revision,
                    policy_revision=self.policy_revision)

    def revoke(self, connection=None):
        if connection is not None and self.connection is not connection:
            return
        self.active = False
        self.obligation_event.set()
        self.reconciliation = None
        if self.battery:
            self.battery.revoke()
        for request in self.pending.values():
            if not request['future'].done():
                request['future'].set_exception(GatewayConflict('App disconnected; retry after reconnection'))
        self.pending.clear()

    def require_socket(self):
        connection = self.current.get()
        if (self.closed or not self.stream.accepting or self.stream.failure or connection is None
                or connection.closed or connection is not self.connection or not connection.session):
            raise GatewayConflict('App session is no longer current')
        return connection

    def authorize(self, operation):
        self.require_socket()
        if not self.active or self.configuration_pending:
            raise GatewayConflict('Reconcile and activate the current app session first')

    def authorize_device(self, intent):
        self.authorize('device')
        if (intent.configuration_revision != self.configuration_revision or intent.policy_revision != self.policy_revision):
            raise GatewayConflict('Device intention belongs to an older configuration or policy')

    async def configuration_changed(self, value):
        # Host invalidates synchronously before it creates this coroutine.
        self.configuration_revision = await self.stream.capture('configuration', value)
        self.configuration_pending = False

    def invalidate_configuration(self):
        self.configuration_pending = True
        self.reconciliation = None
        self.policy = None
        self.policy_revision += 1
        if self.battery:
            self.battery.revoke()

    def physical_proof(self):
        return digest({'ownership':self.physical.ownership.snapshot(),
                       'controls':self.source.physical_controls()})

    async def request_app(self, operation, body):
        if not self.active or self.connection is None:
            raise GatewayConflict('SHS app is not connected and active')
        if operation not in APP_REQUESTS:
            raise ValueError('Unsupported app request')
        if len(self.pending) >= 64:
            raise GatewayConflict('SHS app request queue is full')
        key = uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self.pending[key] = dict(operation=operation, body=deepcopy(body), future=future, offered=False)
        try:
            async with asyncio.timeout(180):
                return await future
        finally:
            self.pending.pop(key, None)

    async def maintain(self):
        while not self.closed:
            try:
                await self.battery.maintain_obligation()
                await self.physical.maintain_obligations()
                self.obligation_error = None
            except Exception as error:
                self.obligation_error = str(error)
            self.obligation_event.clear()
            try:
                await asyncio.wait_for(self.obligation_event.wait(),5)
            except TimeoutError:
                pass

    async def close(self):
        self.closed = True
        self.revoke()
        self.obligation_event.set()
        if self.obligation_task:
            await self.obligation_task
        if self.connection:
            await self.connection.close()
        await self.battery.maintain_obligation()
        await self.physical.maintain_obligations()


APP_REQUESTS = frozenset(('configuration', 'refresh', 'refresh_devices', 'cached_devices', 'cached_home', 'cached_planning',
    'cached_exchange', 'report_mapping', 'replan', 'backfill_prices', 'profile',
    'configuration_changed', 'tick', 'optimisation', 'runtime_report'))

FIELDS = {
    'source': {'operation', 'body'},
    'reconcile': {'checkpoint_sha256'},
    'activate': {'activation_id', 'proof'},
    'resume': {'reconciliation_id'},
    'policy': {'value'},
    'ownership': set(),
    'synchronize': {'models'},
    'minimum_runs': {'models'},
    'device': {'intent'},
    'battery_grant': {'identity', 'catalog', 'conversion', 'expires_at_ms'},
    'battery_route': {'effect', 'grant'},
    'battery_step': {'route_id', 'index', 'send'},
    'projection': {'value'},
    'requests': set(),
    'reply': {'request_id', 'result', 'error'},
}


class AppConnection(GatewayConnection):
    def __init__(self, service):
        super().__init__(service.stream)
        self.service = service

    def disconnected(self):
        """Called synchronously by HA's socket-close callback before disk work."""
        self.closed = True
        self.service.revoke(self)

    async def close(self):
        self.disconnected()
        await super().close()

    async def _request(self, value):
        if type(value) is not dict:
            raise ValueError('Malformed gateway request')
        if value.get('operation') == 'connect':
            return await super()._request(value)
        if self.closed:
            raise GatewayConflict('Gateway socket is closed')
        return await self._dispatch(value)

    async def _dispatch(self, value):
        service = self.service
        token = service.current.set(self)
        try:
            op, body = value.get('operation'), value.get('body')
            if op == 'connect':
                service.revoke()
                service.connection = self
                return await super()._dispatch(value)
            service.require_socket()
            if op in ('receipts', 'ack_delivery', 'snapshot'):
                return await super()._dispatch(value)
            if (set(value) != {'id', 'operation', 'body'} or type(value['id']) is not int
                    or type(body) is not dict or op not in FIELDS or set(body) != FIELDS[op]):
                raise ValueError('Unsupported app gateway operation')
            result = await self._operation(op, body)
            return {'id':value['id'], 'result':result}
        finally:
            service.current.reset(token)

    async def _operation(self, op, body):
        s = self.service
        if op == 'source':
            return await s.source.request(body['operation'], body['body'])
        if op == 'reconcile':
            if s.configuration_pending or type(body['checkpoint_sha256']) is not str or len(body['checkpoint_sha256']) != 64:
                raise GatewayConflict('A loaded app checkpoint and settled configuration are required')
            snapshot = await s.stream.call('snapshot', self.session)
            proof = dict(identity=s.identity, configuration_revision=s.configuration_revision,
                through=snapshot['through'], app_checkpoint_sha256=body['checkpoint_sha256'],
                physical_reconciliation_sha256=s.physical_proof())
            s.reconciliation = dict(id=uuid4().hex, session=self.session, proof=proof)
            return dict(reconciliation=s.reconciliation, snapshot=snapshot,
                        ownership=s.physical.ownership.snapshot())
        if op in ('activate', 'resume'):
            r = s.reconciliation
            if (r is None or r['session'] != self.session or s.configuration_pending
                    or r['proof']['configuration_revision'] != s.configuration_revision
                    or r['proof']['physical_reconciliation_sha256'] != s.physical_proof()):
                raise GatewayConflict('Physical or configuration reconciliation changed')
            if op == 'activate':
                if body['proof'] != r['proof']:
                    raise GatewayConflict('Activation differs from the reconciled checkpoint')
                result = await s.stream.call('activate', self.session, body['activation_id'], body['proof'])
            else:
                if body['reconciliation_id'] != r['id']:
                    raise GatewayConflict('Reconciliation identity changed')
                result = await s.stream.call('resume', self.session, r['proof']['configuration_revision'], r['proof']['through'])
            s.require_socket()
            if s.configuration_pending or r['proof']['physical_reconciliation_sha256'] != s.physical_proof():
                raise GatewayConflict('Physical state changed during activation')
            s.active = True
            return result
        s.authorize(op)
        if op == 'policy':
            if type(body['value']) is not dict or body['value'].get('options') != digest(s.source.options()):
                raise ValueError('Policy identity must be an object')
            if s.policy != body['value']:
                s.policy = deepcopy(body['value'])
                s.policy_revision += 1
            return s.context()
        if op == 'ownership':
            return s.physical.ownership.snapshot()
        if op == 'synchronize':
            return await s.physical.synchronize(body['models'])
        if op == 'minimum_runs':
            return await s.physical.minimum_run_snapshot(s.physical.options(), body['models'])
        if op == 'device':
            return await s.physical.perform(body['intent'])
        if op == 'battery_grant':
            return await s.battery.grant(body['identity'], body['catalog'], body['conversion'], body['expires_at_ms'])
        if op == 'battery_route':
            return await s.battery.propose(body['effect'], body['grant'])
        if op == 'battery_step':
            return await s.battery.step(body['route_id'], body['index'], body['send'])
        if op == 'projection':
            if type(body['value']) is not dict:
                raise ValueError('Projection must be an object')
            s.projection = deepcopy(body['value'])
            s.source.publish(s.projection)
            return {}
        if op == 'requests':
            batch = [{'id':key, 'operation':r['operation'], 'body':r['body']}
                     for key, r in s.pending.items() if not r['offered']]
            for row in batch:
                s.pending[row['id']]['offered'] = True
            return batch
        if op == 'reply':
            request = s.pending.get(body['request_id'])
            if request and not request['future'].done():
                if body['error']:
                    request['future'].set_exception(GatewayConflict(body['error']))
                else:
                    request['future'].set_result(body['result'])
            return {}
        raise ValueError('Unsupported gateway operation')
