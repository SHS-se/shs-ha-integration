"""Durable settings agreement and volatile, clock-bounded execution authority.

No actuator calls live here. Adapters must call guard immediately before every
write and after awaits, then report physical acknowledgement separately.
"""
import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import time

if __package__:
    from .contract_v1.validate import validate, revision, utc, VALIDATOR
    from .control_setup import PRESETS
else:
    from contract_v1.validate import validate, revision, utc, VALIDATOR
    from control_setup import PRESETS

POLL_SECONDS = 15
LEASE_SECONDS = 120
CLOCK_TOLERANCE_SECONDS = 5


def local_advertisement(control_id, record, report):
    """Public semantic projection; registry identities never leave the home."""
    spec = record['spec']
    limits = deepcopy(spec['limits'])
    if spec['contract']['name'] in ('adjustable_output', 'temperature_target'):
        limits['unit'] = next(c['unit'] for c in record['capabilities'] if c['role'] == 'output')
    local = dict(binding_revision=record['binding_revision'], capabilities=deepcopy(record['capabilities']),
                 limits=limits, handover=spec['handover'], setup_gaps=deepcopy(report['errors']))
    if spec['contract']['name'] == 'battery_dispatch':
        local['supported_intents'] = list(PRESETS['sigenergy']['modes'])
    return dict(control_id=control_id, contract=deepcopy(spec['contract']), local=local,
                ready=report['status'] == 'ready')


class ControlAgreement:
    def __init__(self, store, setup, exchange, *, wall=None, monotonic=None, permission=None):
        self.store, self.setup, self.exchange = store, setup, exchange
        self.wall = wall or (lambda: datetime.now(timezone.utc))
        self.monotonic = monotonic or time.monotonic
        # Commissioning/executors provide an explicit local permission source.
        self.permission = permission or (lambda _control_id: {'revision': 0, 'enabled': False})
        self.lock = asyncio.Lock()
        self.saved = {'schema_version': 1, 'home_id': setup.home_id, 'epoch': -1,
                      'edit_revision': -1, 'definition': None, 'accepted': {}}
        self.plan = self.lease = self.active = None
        self.deadline = None
        self.closed = False
        self.error = None
        self.server_acknowledged = {}
        self.last_clock = None
        self.lease_permissions = {}

    async def async_load(self):
        saved = await self.store.async_load()
        if saved is not None:
            if (not isinstance(saved, dict) or set(saved) != set(self.saved) or
                saved['schema_version'] != 1 or saved['home_id'] != self.setup.home_id or
                type(saved['epoch']) is not int or saved['epoch'] < -1 or
                type(saved['edit_revision']) is not int or saved['edit_revision'] < -1 or
                not isinstance(saved['accepted'], dict)):
                raise ValueError('invalid agreement store')
            if saved['definition'] is not None:
                if not VALIDATOR.is_valid(saved['definition']) or saved['definition']['home_id'] != self.setup.home_id:
                    raise ValueError('invalid persisted definition')
                controls = {c['control_id']: c for c in saved['definition']['controls']}
                if any(k not in controls or v != revision(controls[k]) for k, v in saved['accepted'].items()):
                    raise ValueError('invalid persisted acceptance')
            elif saved['accepted']:
                raise ValueError('accepted definition missing')
            self.saved = deepcopy(saved)
        # Permission, active observations, plans and authority are never restored.
        self.plan = None
        self.server_acknowledged = {}
        self.revoke()

    def revoke(self):
        self.lease = self.deadline = None
        self.active = None

    def close(self):
        self.closed = True
        self.revoke()

    def advertisements(self):
        result = []
        inventory = self.setup.inventory()
        for key, record in self.setup.records.items():
            report = self.setup.report(record['spec'], inventory)
            item = local_advertisement(key, record, report)
            item['interface_summary'] = {role: {**{field: inventory[entity].get(field) for field in ('manufacturer', 'model')},
                                                        'platform': inventory[entity].get('registry', {}).get('platform')}
                                         for role, entity in report['resolved'].items()}
            result.append(item)
        return result

    def _clock(self):
        wall, mono = self.wall(), self.monotonic()
        if wall.tzinfo is None or wall.utcoffset().total_seconds() != 0:
            self.revoke()
            raise ValueError('UTC clock required')
        if self.last_clock:
            last_wall, last_mono = self.last_clock
            if mono < last_mono or abs((wall - last_wall).total_seconds() - (mono - last_mono)) > CLOCK_TOLERANCE_SECONDS:
                self.revoke()
                self.last_clock = (wall, mono)
                raise ValueError('clock discontinuity; authority revoked')
        self.last_clock = (wall, mono)
        return wall, mono

    async def async_poll(self, _now=None):
        """One in-flight exchange, independent of telemetry/recorder locks."""
        if self.closed or self.lock.locked():
            return
        async with self.lock:
            try:
                start_wall, start_mono = self._clock()
                ads = self.advertisements()
                self._check_bindings(ads)
                request = {'action': 'sync', 'schema_version': 1, 'home_id': self.setup.home_id,
                           'bindings': ads, 'acknowledged': deepcopy(self.saved['accepted']),
                           'accepted_epoch': self.saved['epoch'],
                           'plan_id': self.plan['plan_id'] if self.plan else None,
                           'active': deepcopy(self.active)}
                response = await self.exchange(request)
                if self.closed:
                    return
                await self._receive(response, start_wall, start_mono)
                self.error = None
            except Exception as err:
                # A transport failure cannot create an edit or renew a deadline.
                # Known invalid content/time is revoked by _receive/_clock.
                self.error = str(err)

    def _check_bindings(self, ads):
        by_id = {b['control_id']: b for b in ads}
        for c in (self.saved['definition'] or {}).get('controls', []):
            b = by_id.get(c['control_id'])
            if not b or not b['ready'] or c['local'] != b['local'] or c['contract'] != b['contract']:
                self.revoke()
                return False
        return True

    async def _receive(self, response, start_wall, start_mono):
        try:
            wall, mono = self._clock()
            if response['schema_version'] != 1 or response['home_id'] != self.setup.home_id:
                raise ValueError('foreign or unsupported agreement')
            epoch, edit_revision = response['epoch'], response['edit_revision']
            if type(epoch) is not int or type(edit_revision) is not int or epoch < self.saved['epoch'] or edit_revision < self.saved['edit_revision']:
                raise ValueError('out-of-order agreement')
            server = utc(response['server_time_utc'])
            if server < start_wall - timedelta(seconds=CLOCK_TOLERANCE_SECONDS) or server > wall + timedelta(seconds=CLOCK_TOLERANCE_SECONDS):
                raise ValueError('server clock outside request interval')
            definition = deepcopy(response['definition'])
            if not VALIDATOR.is_valid(definition) or definition['home_id'] != self.setup.home_id:
                raise ValueError('invalid desired definition')
            prior_controls = {c['control_id']: c for c in (self.saved['definition'] or {}).get('controls', [])}
            for c in definition['controls']:
                prior = prior_controls.get(c['control_id'])
                if prior:
                    if c['desired']['revision'] < prior['desired']['revision']:
                        raise ValueError('desired revision regressed')
                    if c['desired']['revision'] == prior['desired']['revision'] and any(
                            c[key] != prior[key] for key in ('desired', 'contract', 'sources')):
                        raise ValueError('desired revision reused for different settings')
            if epoch == self.saved['epoch'] and self.saved['definition'] is not None:
                def semantics(document):
                    return {**document, 'controls': [{k: v for k, v in c.items() if k != 'presentation'}
                                                    for c in document['controls']]}
                if semantics(definition) != semantics(self.saved['definition']):
                    raise ValueError('execution definition changed without a new epoch')
            if epoch != self.saved['epoch']:
                self.revoke()
                self.plan = None
            async with self.setup.lock:
                accepted = {}
                ads = {b['control_id']: b for b in self.advertisements()}
                for c in definition['controls']:
                    b = ads.get(c['control_id'])
                    if b and b['ready'] and c['contract'] == b['contract'] and c['local'] == b['local']:
                        try:
                            validate({**definition, 'controls': [c]})
                            if c['desired']['objective'] is not None:
                                accepted[c['control_id']] = revision(c)
                        except ValueError:
                            pass  # Explicit pending agreement; never accept partial field edits.
                if len(accepted) == len(definition['controls']):
                    validate(definition)  # Household accounting/association invariants.
                next_saved = {'schema_version': 1, 'home_id': self.setup.home_id, 'epoch': epoch,
                              'edit_revision': edit_revision, 'definition': definition, 'accepted': accepted}
                if next_saved != self.saved:
                    # Stop before an await that may expose a newly incompatible definition.
                    if accepted != self.saved['accepted']:
                        self.revoke()
                    await self.store.async_save(next_saved)
                    self.saved = next_saved
                self.server_acknowledged = deepcopy(response['acknowledged'])
                self._check_bindings(self.advertisements())
            if self.closed:
                self.revoke()
                return
            plan = response['plan']
            if plan is None:
                self.plan = None
                self.revoke()
                return
            validate(plan, definition)
            expected = {c['control_id'] for c in definition['controls'] if c['desired']['included']}
            if {c['control_id'] for c in plan['commands']} != expected:
                raise ValueError('incomplete household plan')
            if self.saved['accepted'] != self.server_acknowledged or len(accepted) != len(definition['controls']):
                raise ValueError('plan requires complete persisted acknowledgement')
            if not utc(plan['valid_from_utc']) <= wall < utc(plan['valid_until_utc']):
                raise ValueError('plan not current')
            if self.plan and self.plan != plan:
                self.revoke()
            self.plan = deepcopy(plan)
            lease = response['lease']
            if lease is None:
                self.revoke()
                return
            issued, expires = utc(lease['issued_at_utc']), utc(lease['expires_at_utc'])
            lifetime = (expires - issued).total_seconds()
            if (lease['epoch'] != epoch or lease['plan_id'] != plan['plan_id'] or issued != server or
                not 0 < lifetime <= LEASE_SECONDS or expires > utc(plan['valid_until_utc']) or expires <= wall):
                raise ValueError('invalid authority lease')
            # Starting at request dispatch is conservative under network latency.
            # Replayed/delayed responses never move the UTC expiry forward.
            self.deadline = min(start_mono + lifetime, mono + (expires - wall).total_seconds())
            if mono >= self.deadline or not self._check_bindings(self.advertisements()):
                raise ValueError('authority expired or binding changed in flight')
            self.lease_permissions = {c['control_id']: deepcopy(self.permission(c['control_id'])) for c in plan['commands']}
            self.lease = deepcopy(lease)
        except BaseException:
            self.revoke()
            raise

    def guard(self, control_id, accepted, plan_id):
        """Return the exact authorized command; never resolve it from forecast W."""
        wall, mono = self._clock()
        if self.closed or not self.lease or mono >= self.deadline or wall >= utc(self.lease['expires_at_utc']):
            self.revoke()
            raise ValueError('execution authority expired')
        if not self._check_bindings(self.advertisements()):
            raise ValueError('binding changed')
        permission = self.permission(control_id)
        if not permission.get('enabled') or permission != self.lease_permissions.get(control_id):
            self.revoke()
            raise ValueError('local permission off or revision changed')
        if plan_id != self.plan['plan_id'] or accepted != self.saved['accepted'].get(control_id):
            raise ValueError('command revision mismatch')
        return deepcopy(next(c for c in self.plan['commands'] if c['control_id'] == control_id))

    def status(self):
        try:
            wall, mono = self._clock()
            if self.lease and (wall >= utc(self.lease['expires_at_utc']) or mono >= self.deadline):
                self.revoke()
            bindings_current = self._check_bindings(self.advertisements())
        except ValueError:
            bindings_current = False
            self.revoke()
        return {'desired_epoch': self.saved['epoch'], 'edit_revision': self.saved['edit_revision'],
                'accepted': deepcopy(self.saved['accepted']), 'server_acknowledged': deepcopy(self.server_acknowledged),
                'synchronization': 'acknowledged' if bindings_current and self.saved['epoch'] >= 0 and self.saved['accepted'] == self.server_acknowledged and
                    len(self.saved['accepted']) == len((self.saved['definition'] or {}).get('controls', [])) else 'pending',
                'authority': 'leased' if self.lease else 'expired', 'lease': deepcopy(self.lease),
                'active': deepcopy(self.active), 'error': self.error, 'control_enabled': False}
