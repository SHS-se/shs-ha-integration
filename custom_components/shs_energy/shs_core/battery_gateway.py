"""HA-owned battery grants and canonical native transition admission."""
from math import floor
from copy import deepcopy
from uuid import uuid4

from .battery_conversion import Conversion
from .battery_live import native_surface, CONTROL_OPTIONS
from .battery_native_adapter import SigenAdapter
from .battery_writer import BatteryWriterFence
from .command_journal import NativeAction, RoutedCommand, ObligationCommand
from .configuration_values import resolve_battery_quantities
from .gateway_journal import GatewayConflict, digest
from .gateway_stream import GatewayRecord
from . import native_records as rt
from .operating_modes import device_mode
from .runtime_json import decode_value, encode_value, runtime_digest

from .native_configuration import native_options

OWNER = 'shs-household-battery'


class BatteryGateway:
    def __init__(self, stream, physical, options, reports, clock, *, authorize, session, context):
        self.stream, self.physical, self.options, self.reports, self.now = stream, physical, options, reports, clock
        self.authorize, self.session, self.context = authorize, session, context
        self.record = GatewayRecord(stream, 'physical')
        self.obligation_store = GatewayRecord(stream, 'battery_obligation')
        self.obligation = {'pending':False, 'command':None, 'fault':None}
        self.installation = None
        self.fence = BatteryWriterFence(GatewayRecord(stream, 'battery_writer'), physical.lock,
            options, clock, self.identity)
        physical.battery_writer_fence = self.fence

    def surface(self, options):
        return native_surface(options, {options[k]: self.reports(options[k]) for k in CONTROL_OPTIONS})

    def identity(self):
        if self.installation is None or self.obligation['command'] or self.obligation['fault']:
            return None
        identity, catalog, _, options = self.installation
        try:
            if self.surface(options)['revision'] != catalog.control_surface_revision:
                return None
        except (ValueError, KeyError, TypeError):
            return None
        return identity

    async def open(self):
        persisted = await self.record.async_load()
        if persisted is not None:
            if set(persisted) != {'identity', 'catalog', 'conversion', 'options'}:
                raise ValueError('Invalid persisted battery installation')
            self.installation = (decode_value(persisted['identity'], rt.WriterIdentity),
                decode_value(persisted['catalog'], rt.NativeCatalog), Conversion.read(persisted['conversion']), persisted['options'])
        pending = await self.obligation_store.async_load()
        if pending is not None:
            if set(pending) != {'pending','command','fault'} or type(pending['pending']) is not bool:
                raise ValueError('Invalid battery physical obligation')
            self.obligation = pending
            await self._settle_obligation()
        await self.fence.open()

    async def grant(self, identity_wire, catalog_wire, conversion_wire, expires_at_ms):
        self.authorize('battery_grant')
        if self.obligation['fault']:
            raise GatewayConflict(self.obligation['fault'])
        identity = decode_value(identity_wire, rt.WriterIdentity)
        catalog = decode_value(catalog_wire, rt.NativeCatalog)
        conversion = Conversion.read(conversion_wire)
        options = self.options()
        surface = self.surface(options)
        expected = rt.WriterIdentity(OWNER, runtime_digest(native_options(options)), surface['revision'])
        if identity != expected:
            # An old, already admitted installation may only obtain release routes.
            if self.installation is None or identity != self.installation[0]:
                raise GatewayConflict('Battery installation differs from canonical configuration')
        else:
            ratings = resolve_battery_quantities(options, self.reports)
            keys = tuple(options[k] for k in CONTROL_OPTIONS)
            if ((catalog.mode_key, catalog.charge_key, catalog.discharge_key) != keys or
                    catalog.control_surface_revision != surface['revision'] or
                    catalog.mode_options != tuple(surface['mode_options']) or catalog.quantum_w != 1 or
                    catalog.charge_max_w != floor(ratings['battery_charge_max_w']) or
                    catalog.discharge_max_w != floor(ratings['battery_discharge_max_w']) or
                    any(surface['limits'][k]['quantum_w'] != 1 or surface['limits'][k]['minimum_w'] != 0
                        or surface['limits'][k]['maximum_w'] < ratings['battery_'+k+'_max_w'] for k in ('charge','discharge'))):
                raise GatewayConflict('Battery catalog differs from current native controls and ratings')
            await self.record.async_save({'identity':identity_wire, 'catalog':catalog_wire,
                'conversion':conversion_wire, 'options':deepcopy(options)})
            self.authorize('battery_grant')
            if options != self.options():
                raise GatewayConflict('Configuration changed during battery admission')
            self.installation = identity, catalog, conversion, deepcopy(options)
        async def released():
            if 'battery' in self.physical.ownership.records:
                raise GatewayConflict('Legacy battery ownership has not been released')
        grant = await self.fence.take_over(identity, expires_at_ms, released)
        self.obligation = {'pending':True, 'command':None, 'fault':None}
        try:
            await self.obligation_store.async_save(self.obligation)
            self.authorize('battery_grant')
        except BaseException:
            self.fence.revoke()
            raise
        return encode_value(grant)

    async def propose(self, effect_wire, grant_wire):
        self.authorize('battery_route')
        effect = decode_value(effect_wire, rt.NeedTransition)
        grant = decode_value(grant_wire, rt.WriterGrant)
        if self.installation is None or not self.fence.is_current(grant, self.identity()):
            raise GatewayConflict('Battery grant is no longer current')
        identity, catalog, conversion, installed_options = self.installation
        if effect.purpose != 'release' and (runtime_digest(native_options(self.options())) != identity.config_revision or device_mode(self.options(), 'battery') != 'controlling'):
            raise GatewayConflict('Battery optimisation permission changed')
        if device_mode(self.options(), 'battery') == 'control_verification':
            raise GatewayConflict('Verification never writes native battery settings')
        proposal = SigenAdapter(catalog, conversion).propose(effect)
        payload = {'effect':effect_wire, 'proposal':encode_value(proposal), 'grant':grant_wire, 'context':self.context()}
        route_id = digest({'session':self.session(), **payload})
        await self.stream.call('admit_route', self.session(), route_id, payload)
        return {'route_id':route_id, 'proposal':encode_value(proposal)}

    async def step(self, route_id, index, send_wire):
        async with self.physical.lock:
            row = await self.stream.call('read_route', self.session(), route_id)
            payload = row['payload']
            proposal = decode_value(payload['proposal'], rt.Proposed)
            effect = decode_value(payload['effect'], rt.NeedTransition)
            grant = decode_value(payload['grant'], rt.WriterGrant)
            if type(index) is not int or not 0 <= index < len(proposal.steps):
                raise ValueError('Invalid admitted battery step')
            send = decode_value(send_wire, rt.Send)
            step = proposal.steps[index]
            if ((send.group_id, send.generation, send.request_id, send.request_revision, send.key, send.value, send.grant)
                    != (proposal.group_id, proposal.generation, proposal.request_id, proposal.request_revision, step.key, step.value, grant)):
                raise GatewayConflict('Send differs from its admitted battery step')
            options = self.installation[3] if self.installation else {}
            action = self.action(step,options)
            command = RoutedCommand('route:'+route_id+':'+str(index), 'battery', effect.group_id, 'battery', action, route_id, index)
            def check():
                self.authorize('battery_step')
                if self.context() != payload['context'] or self.now() >= send.send_by_ms:
                    raise GatewayConflict('Battery policy or dispatch deadline changed')
                if not self.fence.is_current(grant, self.identity()):
                    raise GatewayConflict('Battery grant revoked before dispatch')
                current = self.options()
                if device_mode(current, 'battery') == 'control_verification':
                    raise GatewayConflict('Battery control relinquished to verification')
                if effect.purpose != 'release':
                    if (runtime_digest(native_options(current)) != grant.config_revision or device_mode(current, 'battery') != 'controlling'
                            or not current.get('battery_enabled', True) or '$battery' in current.get('excluded_device_readings', [])
                            or self.now() >= effect.request.valid_until_ms):
                        raise GatewayConflict('Battery request or configuration changed')
                    for key in ('control_override_entity', 'battery_control_override_entity'):
                        if current.get(key) and (self.reports(current[key]) or {}).get('state') != 'off':
                            raise GatewayConflict('Battery manual override active')
            self.authorize('battery_step')
            previous = await self.stream.call('command_outcome', command.id)
            if previous is not None:
                return {'status':previous['status'], 'command_id':command.id, 'attempt_id':send.attempt_id}
            try:
                check()
                await self.physical.native_executor.execute(command, authorize=check, timeout=75)
            except Exception:
                outcome = await self.stream.call('command_outcome', command.id)
                # No journal record means dispatch was not entered. A prepared
                # record without an outcome must be treated as uncertain.
                status = outcome['status'] if outcome else 'not_sent'
                return {'status':status, 'command_id':command.id, 'attempt_id':send.attempt_id}
            return {'status':'service_returned', 'command_id':command.id, 'attempt_id':send.attempt_id}

    def revoke(self):
        # Synchronous socket/configuration fence, before queued disk work.
        self.fence.revoke()

    async def _settle_obligation(self):
        if self.obligation['command']:
            outcome = await self.stream.call('command_outcome', self.obligation['command'])
            if outcome and outcome['status'] in ('prepared','uncertain'):
                self.obligation['fault'] = 'A battery baseline write has an uncertain outcome; inspect native controls before resuming'
            else:
                self.obligation['command'] = None
                self.obligation['fault'] = None
            await self.obligation_store.async_save(self.obligation)

    def action(self, step, options):
        if step.key == options['battery_mode_entity']:
            return NativeAction(step.key,'select_option',step.value)
        field = 'charge' if step.key == options['battery_charge_limit_entity'] else 'discharge'
        limit = self.surface(options)['limits'][field]
        return NativeAction(step.key,'set_value',step.value/(1000 if limit['unit']=='kW' else 1))

    async def maintain_obligation(self):
        """Release a revoked battery writer using the canonical native adapter.

        This is finite physical handback, with no plan, account or policy choice.
        Uncertain local writes remain a visible fence rather than being replayed.
        """
        async with self.physical.lock:
            if not self.obligation['pending'] or self.installation is None:
                return
            if self.obligation['fault']:
                raise GatewayConflict(self.obligation['fault'])
            await self._settle_obligation()
            if self.obligation['fault']:
                raise GatewayConflict(self.obligation['fault'])
            if device_mode(self.options(),'battery') == 'control_verification':
                self.obligation = {'pending':False,'command':None,'fault':None}
                await self.obligation_store.async_save(self.obligation)
                return
            if self.fence.snapshot()['grant_current']:
                return
            identity,catalog,conversion,options = installed = self.installation
            surface = self.surface(options)
            mode = self.reports(catalog.mode_key)['state']
            def watts(entity,field):
                return float(self.reports(entity)['state'])*(1000 if surface['limits'][field]['unit']=='kW' else 1)
            controls = ((catalog.mode_key,mode),(catalog.charge_key,watts(catalog.charge_key,'charge')),
                        (catalog.discharge_key,watts(catalog.discharge_key,'discharge')))
            target = ((catalog.mode_key,'Maximum Self Consumption'),(catalog.charge_key,catalog.charge_max_w),
                      (catalog.discharge_key,catalog.discharge_max_w))
            now = self.now()
            adapter = SigenAdapter(catalog,conversion)
            observation = rt.Observation(1,now,now+30000,controls,(),adapter.envelope(controls))
            request = rt.Request('physical-release-'+uuid4().hex,1,now+900000,target,())
            effect = rt.NeedTransition('battery',1,'release',request,observation,catalog.adapter_revision,1,now+900000,controls)
            route = adapter.propose(effect)
            def authorize():
                if (self.installation != installed or self.fence.snapshot()['grant_current']
                        or device_mode(self.options(),'battery') == 'control_verification'):
                    raise GatewayConflict('Battery handback authority changed')
            for index,step in enumerate(route.steps):
                command = ObligationCommand(request.id+':'+str(index),'battery','battery','handover',self.action(step,options))
                self.obligation['command'] = command.id
                await self.obligation_store.async_save(self.obligation)
                try:
                    await self.physical.native_executor.execute(command,authorize=authorize,timeout=75)
                finally:
                    await self._settle_obligation()
            self.obligation = {'pending':False,'command':None,'fault':None}
            await self.obligation_store.async_save(self.obligation)
