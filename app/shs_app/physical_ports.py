"""App-side semantic device and battery ports; no native service RPC."""
from shs_core.native_configuration import native_options
from shs_core import home_runtime as rt
from shs_core.command_transport import CommandUncertain
from shs_core.device_port import device_intent
from shs_core.gateway_journal import AdmissionPending, GatewayConflict, GatewayRejected, digest
from shs_core.home_host import DispatchRejected
from shs_core.native_records import GRANT_NOT_CURRENT
from shs_core.runtime_json import encode_value, decode_value


class RemoteDevices:
    def __init__(self, gateway, household, *, wake_battery):
        self.gateway, self.household = gateway, household
        self.context = None
        self.wake_battery = wake_battery
        self.policy = None
        self.heating = {}
        self.control_notices = {}

    async def synchronize(self, models, *, options):
        # Update the command epoch before returning a physical ownership view.
        value = {'plan':digest(self.household.optimisation_plan),
                 'import_limit_w':self.household.house_import_limit_w(),
                 'options':digest(native_options(options))}
        self.context = await self.gateway.call('policy', {'value':value})
        self.policy = value
        result = await self.gateway.call('synchronize', {'models':models})
        self.heating = result.pop('heating', {})
        self.control_notices = result.pop('control_notices', {})
        await self.household.async_capacity_recovered(result.pop('capacity_recoveries'))
        return result

    async def ownership(self):
        return await self.gateway.call('ownership', {})

    async def perform(self, device, operation, plan, slot, *, heating=None):
        if self.context is None:
            raise AdmissionPending('Device policy has not been admitted')
        intent = device_intent(device, operation, plan, slot,
            configuration_revision=self.context['configuration_revision'], policy_revision=self.context['policy_revision'], heating=heating)
        response = await self.gateway.call('device', {'intent':intent})
        if response['reservation_required']:
            self.wake_battery()
        return response


class RemoteBattery:
    def __init__(self, gateway, runtime, now, *, context):
        self.gateway, self.runtime, self.now = gateway, runtime, now
        self.context = context
        self.grant = None
        self.routes = {}
        self.enabled = False
        self.cover = self.UNSENT

    UNSENT = object()

    def revoke(self):
        self.enabled = False
        self.grant = None
        self.routes.clear()
        # A new gateway session starts without any promise; publish it again.
        self.cover = self.UNSENT

    def close(self):
        self.revoke()

    async def import_capacity(self, charge_run=None):
        return await self.gateway.call('source', {'operation':'battery_capacity',
            'body':{'charge_run':charge_run} if charge_run else {}})

    async def publish_target(self, request):
        if not self.is_current(self.grant,self.runtime.identity()):
            raise AdmissionPending('Battery writer is not current')
        context = self.context()
        if context is None:
            raise AdmissionPending('Battery policy has not been admitted')
        await self.gateway.call('battery_target',{'request':encode_value(request),
            'grant':encode_value(self.grant),'context':context})

    async def publish_cover(self, offer):
        """Publish the capped instruction's promise to the gateway's final admission."""
        context = self.context()
        grant = self.grant
        if context is None or not self.is_current(grant, self.runtime.identity()):
            self.cover = self.UNSENT
            return
        publication = (context, grant, offer)
        if publication != self.cover:
            await self.gateway.call('battery_cover', {'offer':offer, 'context':context, 'grant':encode_value(grant)})
            self.cover = publication

    def is_current(self, grant, identity):
        return bool(self.enabled and self.gateway.connected and grant and grant == self.grant and identity
            and (grant.owner_id, grant.config_revision, grant.control_surface_revision) ==
                (identity.owner_id, identity.config_revision, identity.control_surface_revision)
            and self.now() < grant.expires_at_ms)

    def snapshot(self):
        return {'owner':'app' if self.enabled else 'fenced', 'epoch':self.grant.epoch if self.grant else 0,
                'grant_current':self.is_current(self.grant, self.runtime.identity()), 'fault':None}

    async def take_over(self, identity, expires_at_ms):
        if not self.enabled:
            raise GatewayConflict('Battery writer is dormant until receipt reconciliation completes')
        catalog = self.runtime.adapter.catalog
        wire = await self.gateway.call('battery_grant', dict(identity=encode_value(identity),
            catalog=encode_value(catalog), conversion=self.runtime.adapter.conversion.wire(), expires_at_ms=expires_at_ms))
        self.grant = decode_value(wire, rt.WriterGrant)
        return self.grant

    async def propose(self, effect):
        if not self.is_current(self.grant, self.runtime.identity()):
            raise GatewayConflict('Battery writer is not current')
        try:
            result = await self.gateway.call('battery_route', {'effect':encode_value(effect), 'grant':encode_value(self.grant)})
        except GatewayRejected as error:
            # HA owns the grant and can end it with this session still open (a
            # settings revision). Forget that grant
            # so the next refresh requests a new one, rather than proposing with
            # it until this side's copy would have expired. Any other refusal,
            # such as HA still settling a metadata capture, leaves it standing.
            if str(error) == GRANT_NOT_CURRENT:
                self.grant = None
                self.routes.clear()
                raise AdmissionPending(str(error)) from error
            raise
        proposal = decode_value(result['proposal'], rt.Proposed)
        key = (proposal.group_id, proposal.generation, proposal.request_id, proposal.request_revision)
        previous = self.routes.get(key)
        if previous is None or proposal.token >= previous[1].token:
            self.routes[key] = (result['route_id'], proposal)
        return proposal

    async def dispatch(self, effect, authorize):
        authorize()
        key = (effect.group_id, effect.generation, effect.request_id, effect.request_revision)
        route = self.routes.get(key)
        if route is None:
            raise DispatchRejected('Battery transition was not admitted on this connection')
        identity, proposal = route
        index = effect.step_index
        if type(index) is not int or not 0 <= index < len(proposal.steps) or (proposal.steps[index].key, proposal.steps[index].value) != (effect.key, effect.value):
            raise DispatchRejected('Battery send differs from the next admitted step')
        try:
            result = await self.gateway.call('battery_step', {'route_id':identity, 'index':index, 'send':encode_value(effect)})
        except AdmissionPending as error:
            # HA refused admission before the native call could start.
            raise DispatchRejected(str(error)) from error
        if result['status'] == 'not_sent':
            raise DispatchRejected(f"HA rejected the battery step before dispatch: {result['reason']}")
        if result['status'] != 'service_returned':
            raise CommandUncertain('Battery step outcome is uncertain; observe before replanning')
