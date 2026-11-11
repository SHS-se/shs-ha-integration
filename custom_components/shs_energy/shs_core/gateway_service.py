"""Authenticated session authority and the closed app/device boundary.

Host adapters provide source reads and canonical configuration. Only this service
can activate a socket, admit policy revisions or reach physical owners.
"""
import asyncio
from contextvars import ContextVar
from copy import deepcopy
from uuid import uuid4

from .gateway_journal import AdmissionPending, GatewayConflict, digest
from .native_configuration import native_options
from .gateway_stream import GatewayConnection
from .command_transport import settled, settle_task
from .heating_overrides import DEFAULT_MINUTES, active_room_heating, heating_binding, heating_room
from .native_commands import finite
from .operating_modes import device_mode


class GatewayService:
    def __init__(self, stream, identity, source):
        self.stream, self.identity, self.source = stream, identity, source
        self.connection = None
        self.current = ContextVar('shs_gateway_socket', default=None)
        self.configuration_revision = 0
        self.configuration_pending = True
        self.installation_pending = lambda: False
        self.policy_revision = 0
        self.policy = None
        self.import_limit_w = None
        # The connected app's promised battery supply; memory only, per session.
        self.battery_cover = None
        self.battery_cover_authority = None
        self.active = False
        self.physical = None
        self.battery = None
        self.heating = None
        self.reconciliation = None
        self.pending = {}
        self.projection = None
        self.closed = False
        self.close_task = None
        self.obligation_event = asyncio.Event()
        self.obligation_task = None
        self.obligation_error = None
        self.updates = {'receipts':0, 'requests':0, 'heating':0, 'heating_capacity':0}
        self.update_event = asyncio.Event()
        self.stream.listeners.add(self.receipts_changed)

    def receipts_changed(self):
        self.updates['receipts'] += 1
        self.update_event.set()

    def wake_heating_capacity(self):
        self.updates['heating_capacity'] += 1
        self.update_event.set()

    def heating_changed(self):
        self.updates['heating'] += 1
        self.update_event.set()
        self.source.heating_waiting.clear()
        if self.source.projection:
            self.source.projection.async_update_listeners()

    def heating_inventory(self):
        return {row['permission']['controller_id']: row for row in
                (self.projection or {}).get('execution_devices', [])
                if row['planned'] and not row.get('system_member')}

    @staticmethod
    def heating_rooms(options, inventory):
        rooms = {}
        for owner, row in inventory.items():
            binding = heating_binding(options, owner)
            if not binding or (row.get('category') != 'heating'
                    and not (row.get('category') == 'pool_heating' and len(binding) == 1)):
                continue
            rooms.setdefault(heating_room(options, owner), {})[owner] = (row, binding)
        return rooms

    def heating_controls(self):
        options = self.source.options()
        book = self.heating.value
        now = self.heating.now()
        inventory = self.heating_inventory()
        rooms = self.heating_rooms(options, inventory)
        names = self.source.area_names() if any(room.startswith('room:') for room in rooms) else {}
        zones = {room: self.heating_control(room, members, options, book, now, names)
                 for room, members in rooms.items()}
        mappings = options.get('device_control_mappings', {})
        loads = {}
        for owner, row in inventory.items():
            mapping = mappings.get(row['key'], {})
            if owner in ('battery', 'ev', 'pool') or mapping.get('control_type') in ('switch_schedule', 'permit_inhibit'):
                loads[owner] = row['name']
            elif (members := rooms.get(heating_room(options, owner), {})) and owner in members and (
                    members[owner][1][0].startswith('climate.')):
                loads[owner] = row['name']
        return dict(zones=zones, loads=loads, order=book['order'])

    def heating_control(self, room, members, options, book, now, names):
        area = room.removeprefix('room:') if room.startswith('room:') else None
        thermostats, relay_states = {}, {}
        lower, upper, steps = [], [], []
        controlling_room = False
        for owner, (row, binding) in members.items():
            native = self.source.report(binding[0]) or {}
            state, attrs = native.get('state'), native.get('attributes', {})
            controlling = row['mode'] == 'controlling' and device_mode(options, owner) == 'controlling'
            if len(binding) == 2:
                relay_states[owner] = state if state in ('on', 'off') else None
                if controlling:
                    steps.append(.1)
            else:
                climate = binding[0].startswith('climate.')
                try:
                    reported = attrs.get('temperature') if climate else state
                    applied = finite(reported) if state not in (None, 'unknown', 'unavailable') else None
                except (TypeError, ValueError):
                    applied = None
                thermostats[owner] = applied
                if controlling:
                    minimum = attrs.get('min_temp' if climate else 'min')
                    maximum = attrs.get('max_temp' if climate else 'max')
                    if minimum is not None and maximum is not None:
                        lower.append(minimum)
                        upper.append(maximum)
                        steps.append(attrs.get('target_temp_step' if climate else 'step', .1))
            controlling_room |= controlling
        active = active_room_heating(book, room, now)
        room_target = self.single_heating_value(row.get('planned_room_target_c') for row, _ in members.values())
        planned = room_target if room_target is not None else self.single_heating_value(
            row.get('planned_target_c') for row, _ in members.values())
        applied = self.single_heating_value(thermostats.values()) if None not in thermostats.values() else None
        if active and relay_states.keys() & active.get('bindings', {}).keys():
            applied = active['target_c']
        anchor = min(thermostats, default=room)
        name = names.get(area, area) if area else next(iter(members.values()))[0]['name']
        return dict(name=name, area_id=area, area_name=names.get(area), entity_owner=anchor,
            target_c=active['target_c'] if active else planned, planned_target_c=planned,
            room_target_c=active['target_c'] if active and room_target is not None else room_target,
            expires_at=active['expires_at'] if active else None, applied_c=applied,
            min_c=max(lower, default=None), max_c=min(upper, default=None),
            step=steps[0] if len(steps) == 1 else .1,
            minutes=book['zones'].get(room, {}).get('minutes', DEFAULT_MINUTES),
            controlling=controlling_room, relay_states=relay_states, equipment_targets=thermostats)

    @staticmethod
    def single_heating_value(values):
        distinct = {value for value in values if value is not None}
        return next(iter(distinct)) if len(distinct) == 1 else None

    async def change_heating(self, body):
        if not self.active or self.closed:
            raise AdmissionPending('SHS is reconnecting')
        if type(body) is not dict or type(body.get('owner')) not in (str, type(None)):
            raise ValueError('Invalid heating control request')
        controls = self.heating_controls()
        options = self.source.options()
        owner = body.get('owner')
        zone = controls['zones'].get(owner)
        equipment = None
        if zone and zone['controlling']:
            rooms = self.heating_rooms(options, self.heating_inventory())
            bindings = {device: binding for device, (row, binding) in rooms.get(owner, {}).items()
                        if row['mode'] == 'controlling' and device_mode(options, device) == 'controlling'}
            if bindings:
                equipment = (bindings if zone['area_id'] else bindings[owner], zone['min_c'], zone['max_c'])
        await self.heating.change(body, options, equipment, set(controls['loads']))
        return self.heating_controls()

    async def wait_updates(self, after):
        if (type(after) is not dict or set(after) != set(self.updates)
                or any(type(value) is not int or value < 0 for value in after.values())):
            raise ValueError('Invalid gateway notification cursor')
        while True:
            self.require_socket()
            self.update_event.clear()
            if after != self.updates:
                return dict(self.updates)
            await self.update_event.wait()

    def session(self):
        return self.connection.session if self.connection else None

    def context(self):
        return dict(session=self.session(), configuration_revision=self.configuration_revision,
                    policy_revision=self.policy_revision)

    def revoke(self, connection=None):
        if connection is not None and self.connection is not connection:
            return
        self.active = False
        self.update_event.set()
        self.obligation_event.set()
        self.reconciliation = None
        self.clear_battery_cover()
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
        if not self.active or self.admission_pending():
            raise AdmissionPending('Reconcile and activate the current app session first')

    def admission_pending(self):
        return self.configuration_pending or self.installation_pending()

    def clear_battery_cover(self):
        self.battery_cover = self.battery_cover_authority = None

    def current_battery_cover(self):
        authority = self.battery_cover_authority
        if (authority is not None and not self.admission_pending() and authority[0] == self.context()
                and self.battery.fence.is_current(authority[1], self.battery.identity())):
            return self.battery_cover
        return None

    def authorize_device(self, intent):
        self.authorize('device')
        if (intent.configuration_revision != self.configuration_revision or intent.policy_revision != self.policy_revision):
            raise AdmissionPending('Device intention belongs to an older configuration or policy')

    async def configuration_changed(self, value):
        # Host invalidates synchronously before it creates this coroutine.
        self.configuration_revision = await self.stream.capture('configuration', value)
        if self.heating is not None:
            await self.heating.retire(self.source.options())
        self.configuration_pending = False

    def invalidate_configuration(self):
        # A settings revision changes what the battery writer was granted for.
        self.invalidate_context()
        if self.battery:
            self.battery.revoke()

    def invalidate_context(self):
        """Refuse everything admitted before a new context capture settles.

        Pending configuration and the new policy revision already stop every
        grant, route, step and device intention. The battery writer stands:
        HA hands a revoked battery back to its baseline, and names, areas or
        another integration's entities must never change a device's settings.
        """
        self.configuration_pending = True
        self.clear_battery_cover()
        self.reconciliation = None
        self.policy = None
        self.policy_revision += 1

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
        self.updates['requests'] += 1
        self.update_event.set()
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
        if self.close_task is None:
            # Quiesce producers and authority synchronously before any await.
            self.closed = True
            if self.heating is not None:
                self.heating.close()
            self.source.closed = True
            self.stream.listeners.discard(self.receipts_changed)
            self.revoke()
            self.obligation_event.set()
            self.close_task = asyncio.create_task(self._close())
        await settle_task(self.close_task)

    async def _close(self):
        try:
            if self.obligation_task:
                try:
                    await settle_task(self.obligation_task)
                except asyncio.CancelledError:
                    # HA cancels entry background tasks during shutdown. Its
                    # physical journal work has settled before the task exits.
                    if not self.obligation_task.cancelled():
                        raise
            if self.connection:
                await self.connection.close()
            await self.battery.maintain_obligation()
            await self.physical.maintain_obligations()
        finally:
            try:
                await self.stream.close()
            finally:
                await settled(self.stream.executor,self.stream.journal.close)


APP_REQUESTS = frozenset(('configuration', 'refresh', 'refresh_devices', 'cached_devices', 'cached_home', 'cached_planning',
    'cached_exchange', 'report_mapping', 'replan', 'backfill_prices', 'profile',
    'configuration_changed', 'tick', 'optimisation', 'runtime_report'))

FIELDS = {
    'updates': {'after'},
    'source': {'operation', 'body'},
    'reconcile': {'checkpoint_sha256'},
    'activate': {'activation_id', 'proof'},
    'resume': {'reconciliation_id'},
    'policy': {'value'},
    'ownership': set(),
    'synchronize': {'models'},
    'heating': {'edit'},
    'device': {'intent'},
    'battery_grant': {'identity', 'catalog', 'conversion', 'expires_at_ms'},
    'battery_route': {'effect', 'grant'},
    'battery_target': {'request', 'grant', 'context'},
    'battery_step': {'route_id', 'index', 'send'},
    'battery_cover': {'offer', 'grant', 'context'},
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
            if op in ('receipts', 'ack_delivery', 'ack_processed', 'snapshot'):
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
        if op == 'updates':
            return await s.wait_updates(body['after'])
        if op == 'requests':
            if not s.active:
                raise AdmissionPending('Activate the current app session first')
            batch = [{'id':key, 'operation':r['operation'], 'body':r['body']}
                     for key, r in s.pending.items() if not r['offered']]
            for row in batch:
                s.pending[row['id']]['offered'] = True
            return batch
        if op == 'source':
            return await s.source.request(body['operation'], body['body'])
        if op == 'reconcile':
            if s.admission_pending() or type(body['checkpoint_sha256']) is not str or len(body['checkpoint_sha256']) != 64:
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
            if (r is None or r['session'] != self.session or s.admission_pending()
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
            if s.admission_pending() or r['proof']['physical_reconciliation_sha256'] != s.physical_proof():
                raise GatewayConflict('Physical state changed during activation')
            s.active = True
            return result
        s.authorize(op)
        if op == 'policy':
            if type(body['value']) is not dict or body['value'].get('options') != digest(native_options(s.source.options())):
                raise ValueError('Policy identity must be an object')
            from .grid_capacity import import_limit
            # A session can collect inputs before its first website response.
            # Missing authority grants no headroom: final native checks refuse
            # every increase until a resolved house limit is admitted.
            raw_limit = body['value']['import_limit_w']
            limit = None if raw_limit is None else import_limit(raw_limit)
            context = s.context()
            if limit is not None and s.import_limit_w != limit:
                await s.stream.call('save_record', 'house_import_limit', {'import_limit_w':limit})
            s.authorize(op)
            if s.context() != context:
                raise GatewayConflict('House import policy changed during admission')
            s.import_limit_w = limit
            if s.policy != body['value']:
                s.policy = deepcopy(body['value'])
                s.policy_revision += 1
            return s.context()
        if op == 'ownership':
            return s.physical.ownership.snapshot()
        if op == 'synchronize':
            result = await s.physical.synchronize(body['models'])
            if s.heating is not None:
                result['heating'] = s.heating.value
            return result
        if op == 'heating':
            return (await s.change_heating(body['edit']) if body['edit'] is not None
                    else s.heating_controls())
        if op == 'device':
            if type(body['intent']) is dict and body['intent'].get('operation') == 'curtail':
                raise ValueError('Heating relief is selected by the gateway')
            return await s.physical.perform(body['intent'])
        if op == 'battery_grant':
            return await s.battery.grant(body['identity'], body['catalog'], body['conversion'], body['expires_at_ms'])
        if op == 'battery_target':
            return await s.battery.publish_target(body['request'],body['grant'],body['context'])
        if op == 'battery_route':
            return await s.battery.propose(body['effect'], body['grant'])
        if op == 'battery_cover':
            from .battery_mode import read_cover_offer
            from .native_records import WriterGrant, GRANT_NOT_CURRENT
            from .runtime_json import decode_value
            grant = decode_value(body['grant'], WriterGrant)
            if (body['context'] != s.context()
                    or not s.battery.fence.is_current(grant, s.battery.identity())
                    or device_mode(s.source.options(), 'battery') != 'controlling'):
                raise AdmissionPending(GRANT_NOT_CURRENT)
            cover = read_cover_offer(body['offer'])
            authority = (body['context'], grant)
            if cover != s.battery_cover or authority != s.battery_cover_authority:
                s.battery_cover = cover
                s.battery_cover_authority = authority
                s.wake_heating_capacity()
            return {}
        if op == 'battery_step':
            return await s.battery.step(body['route_id'], body['index'], body['send'])
        if op == 'projection':
            if type(body['value']) is not dict:
                raise ValueError('Projection must be an object')
            s.projection = deepcopy(body['value'])
            await s.source.publish(s.projection)
            return {}
        if op == 'reply':
            request = s.pending.get(body['request_id'])
            if request and not request['future'].done():
                if body['error']:
                    request['future'].set_exception(GatewayConflict(body['error']))
                else:
                    request['future'].set_result(body['result'])
            return {}
        raise ValueError('Unsupported gateway operation')
