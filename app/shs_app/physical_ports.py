"""App-side semantic device and battery ports; no native service RPC."""
from uuid import uuid4
from shs_core.native_configuration import native_options
from shs_core import home_runtime as rt
from shs_core.command_transport import CommandUncertain
from shs_core.device_port import device_intent
from shs_core.gateway_journal import GatewayConflict, GatewayRejected, digest
from shs_core.home_host import DispatchRejected
from shs_core.native_records import GRANT_NOT_CURRENT
from shs_core.runtime_json import encode_value, decode_value


class RemoteDevices:
    def __init__(self, gateway, household):
        self.gateway, self.household = gateway, household
        self.context = None
        self.reserve = None
        self.policy = None

    async def synchronize(self, models):
        # Update the command epoch before returning a physical ownership view.
        value = {'plan':digest(self.household.optimisation_plan),
                 'options':digest(native_options(self.household.resolved_options()))}
        self.context = await self.gateway.call('policy', {'value':value})
        self.policy = value
        return await self.gateway.call('synchronize', {'models':models})

    async def ownership(self):
        return await self.gateway.call('ownership', {})

    async def abandon(self, device):
        # Only HA's canonical Verification mode can relinquish captured settings.
        return await self.synchronize((self.household.optimisation_plan or {}).get('device_models', []))


    async def perform(self, device, operation, plan, slot):
        if self.context is None:
            raise GatewayConflict('Device policy has not been admitted')
        intent = device_intent(device, operation, plan, slot,
            configuration_revision=self.context['configuration_revision'], policy_revision=self.context['policy_revision'])
        response = await self.gateway.call('device', {'intent':intent})
        if response['reservation_required']:
            if self.reserve is None:
                raise GatewayConflict('External demand reservation is not connected')
            if self.reserve(device):
                intent.update(request_id=uuid4().hex, headroom_reserved=True)
                response = await self.gateway.call('device', {'intent':intent})
        return response


class RemoteBattery:
    def __init__(self, gateway, runtime, now):
        self.gateway, self.runtime, self.now = gateway, runtime, now
        self.grant = None
        self.routes = {}
        self.enabled = False

    def revoke(self):
        self.enabled = False
        self.grant = None
        self.routes.clear()

    def close(self):
        self.revoke()

    def is_current(self, grant, identity):
        return bool(self.enabled and self.gateway.connected and grant and grant == self.grant and identity
            and (grant.owner_id, grant.config_revision, grant.control_surface_revision) ==
                (identity.owner_id, identity.config_revision, identity.control_surface_revision)
            and self.now() < grant.expires_at_ms)

    def snapshot(self):
        return {'owner':'app' if self.enabled else 'fenced', 'epoch':self.grant.epoch if self.grant else 0,
                'grant_current':self.is_current(self.grant, self.runtime.identity()), 'fault':None}

    async def take_over(self, identity, expires_at_ms, release):
        if not self.enabled:
            raise GatewayConflict('Battery writer is dormant until receipt reconciliation completes')
        await release()
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
            # settings revision), then hands the battery back. Forget that grant
            # so the next refresh requests a new one, rather than proposing with
            # it until this side's copy would have expired. Any other refusal,
            # such as HA still settling a metadata capture, leaves it standing.
            if str(error) == GRANT_NOT_CURRENT:
                self.grant = None
                self.routes.clear()
            raise
        proposal = decode_value(result['proposal'], rt.Proposed)
        key = (proposal.group_id, proposal.generation, proposal.request_id, proposal.request_revision)
        self.routes[key] = [result['route_id'], proposal, 0]
        return proposal

    async def dispatch(self, effect, authorize):
        authorize()
        key = (effect.group_id, effect.generation, effect.request_id, effect.request_revision)
        route = self.routes.get(key)
        if route is None:
            raise DispatchRejected('Battery transition was not admitted on this connection')
        identity, proposal, index = route
        if index >= len(proposal.steps) or (proposal.steps[index].key, proposal.steps[index].value) != (effect.key, effect.value):
            raise DispatchRejected('Battery send differs from the next admitted step')
        result = await self.gateway.call('battery_step', {'route_id':identity, 'index':index, 'send':encode_value(effect)})
        if result['status'] == 'not_sent':
            raise DispatchRejected('HA rejected the battery step before dispatch')
        if result['status'] != 'service_returned':
            raise CommandUncertain('Battery step outcome is uncertain; observe before replanning')
        route[2] += 1
