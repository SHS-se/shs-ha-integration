"""One journalled desired-state executor for the accepted Sigenergy contract.

Only mode, authority and the two ESS ceilings are writable. Registry identities,
not entity names, survive restart and binding edits. Permission is supplied by the
agreement and remains off in the integration until commissioning.
"""
import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import time

if __package__:
    from .battery_policy import WRITE_ROLES, FLOW_ROLES, SENTINEL_W, desired_state, normal_state, operation
    from .control_capabilities import build_command, finite_number, validate_value
    from .control_setup import PRESETS, resolve, identity, validate_binding_record
    from .contract_v1.validate import revision, utc, validate
else:
    from battery_policy import WRITE_ROLES, FLOW_ROLES, SENTINEL_W, desired_state, normal_state, operation
    from control_capabilities import build_command, finite_number, validate_value
    from control_setup import PRESETS, resolve, identity, validate_binding_record
    from contract_v1.validate import revision, utc, validate

SCOPE_KEYS = ('battery_capacity_kwh', 'battery_charge_efficiency', 'battery_discharge_efficiency',
              'grid_import_limit_w', 'grid_export_limit_w')


class BatteryController:
    def __init__(self, hass, setup, agreement, store, options, *, report=None, wall=None, monotonic=None,
                 sleep=None, confirm_seconds=15, legacy_ready=None):
        self.hass, self.setup, self.agreement, self.store, self.options = hass, setup, agreement, store, options
        self.report_callback = report or (lambda **_status: None)
        self.wall = wall or (lambda: datetime.now(timezone.utc))
        self.monotonic = monotonic or time.monotonic
        self.sleep = sleep or asyncio.sleep
        self.confirm_seconds = confirm_seconds
        self.legacy_ready = legacy_ready or (lambda: None)
        self.records, self.overrides, self.status = {}, {}, {'state': 'disabled'}
        self.lock = asyncio.Lock()
        self.closed = False
        self.loaded = False
        self.storage_fault = False
        self.context = None
        self.remote_guard = None
        self.plan_scope = None

    def report(self, state, **details):
        key = details.get('control_id') or (next(iter(self.records)) if len(self.records) == 1 else None)
        if key in self.records:
            binding = self.records[key]['binding']
            details.update(control_id=key, owned_binding_revision=binding['binding_revision'],
                           owned_normal_settings=normal_state(binding))
        self.status = {'state': state, **details}
        self.report_callback(**self.status)

    async def save(self, records=None, overrides=None):
        records = deepcopy(self.records if records is None else records)
        overrides = deepcopy(self.overrides if overrides is None else overrides)
        try:
            await self.store.async_save({'schema_version': 1, 'home_id': self.setup.home_id,
                                         'records': records, 'overrides': overrides})
        except BaseException:
            self.storage_fault = True
            self.agreement.revoke()
            raise
        self.records, self.overrides = records, overrides

    async def load(self):
        self.storage_fault = True
        payload = await self.store.async_load()
        if payload is None:
            self.records, self.overrides = {}, {}
        else:
            if (not isinstance(payload, dict) or set(payload) != {'schema_version', 'home_id', 'records', 'overrides'} or payload['schema_version'] != 1
                    or payload['home_id'] != self.setup.home_id or not isinstance(payload['records'], dict)
                    or not isinstance(payload['overrides'], dict)):
                raise ValueError('invalid battery ownership journal')
            for key, r in payload['records'].items():
                if set(r) != {'binding', 'expected', 'pending', 'phase', 'last_issued'} or r['phase'] not in ('owned', 'handover'):
                    raise ValueError('invalid battery ownership record')
                binding = r['binding']
                validate_binding_record(key, binding)
                required = set(PRESETS['contracts']['battery_dispatch']['roles'])
                if set(binding['spec']['roles']) != required or len(binding['capabilities']) != len(required) or {c['role'] for c in binding['capabilities']} != required:
                    raise ValueError('incomplete battery role capabilities')
                if {tuple(c) for c in binding['claims']} != {identity(binding['spec']['roles'][role]) for role in WRITE_ROLES}:
                    raise ValueError('invalid battery actuator reservations')
                if binding['spec']['control_id'] != key or binding['spec']['contract'] != {'name': 'battery_dispatch', 'version': 1}:
                    raise ValueError('foreign battery binding in journal')
                for registry in binding['spec']['roles'].values(): identity(registry)
                if set(r['expected']) != set(WRITE_ROLES):
                    raise ValueError('incomplete battery ownership state')
                normal_state(binding)
                for role, value in r['expected'].items(): self.valid_value(binding, role, value)
                if r['pending'] is not None:
                    if set(r['pending']) != {'role', 'value', 'issued_at'} or r['pending']['role'] not in WRITE_ROLES:
                        raise ValueError('invalid pending battery write')
                    self.valid_value(binding, r['pending']['role'], r['pending']['value'])
                    utc(r['pending']['issued_at'])
                if r['last_issued'] is not None: utc(r['last_issued'])
            if not set(payload['overrides']) <= set(payload['records']) or not all(isinstance(v, str) and v for v in payload['overrides'].values()):
                raise ValueError('invalid battery override journal')
            self.records, self.overrides = deepcopy(payload['records']), deepcopy(payload['overrides'])
        self.storage_fault = False
        self.loaded = True

    @staticmethod
    def valid_value(binding, role, value):
        cap = next(c for c in binding['capabilities'] if c['role'] == role)
        validate_value(cap, value)
        if role.endswith('_ceiling'):
            watts = finite_number(value) * (1000 if cap['unit'] == 'kW' else 1)
            if watts < 0 or watts == SENTINEL_W:
                raise ValueError('unset ESS sentinel cannot be owned or restored')
        if role == 'mode' and value not in set(PRESETS['sigenergy']['modes'].values()):
            raise ValueError('unknown or prohibited battery mode')

    def entity(self, binding, role):
        entity = resolve(binding['spec']['roles'][role], self.setup.inventory())
        cap = next(c for c in binding['capabilities'] if c['role'] == role)
        if {'role': role, **entity['capability']} != cap:
            raise ValueError('battery capability changed; handover requires the saved interface')
        state = self.hass.states.get(entity['entity_id'])
        if state is None or state.state in ('unknown', 'unavailable'):
            raise ValueError(role + ' is unavailable')
        age = (self.wall() - state.last_reported).total_seconds()
        if not -5 <= age <= 120:
            raise ValueError(role + ' is stale or future-dated')
        return entity['entity_id'], state, cap

    def value(self, binding, role):
        _entity, state, cap = self.entity(binding, role)
        value = finite_number(state.state) if cap['operation'] == 'set_number' else state.state
        if role in WRITE_ROLES: self.valid_value(binding, role, value)
        return value

    def measurements(self, binding, *, after=None):
        result = {}
        for role in FLOW_ROLES:
            _entity, state, cap = self.entity(binding, role)
            if after and state.last_reported < utc(after):
                raise ValueError('waiting for battery/household observations after settings acknowledgement')
            result[role] = finite_number(state.state) * (1000 if cap['unit'] == 'kW' else 1)
        return result

    def scope(self):
        options = self.options()
        return {key: options.get(key) for key in SCOPE_KEYS}

    def guard(self):
        self.legacy_ready()
        if self.storage_fault:
            raise ValueError('battery journal persistence is unconfirmed')
        if self.remote_guard:
            binding = self.records[self.remote_guard]['binding']
            if self.value(binding, 'authority') != 'on' or self.value(binding, 'authority_confirmation') != 'Remote EMS':
                raise ValueError('Remote EMS authority was lost')
        if self.context is None:
            return  # Journal-authorized handover only; no new dispatch authority.
        key, command, plan_id, scope = self.context
        if self.closed or self.scope() != scope:
            raise ValueError('battery operating configuration changed')
        current = self.agreement.guard(key, command['accepted'], plan_id)
        if current != command or key in self.overrides:
            raise ValueError('battery command changed or control is overridden')

    async def persist_record(self, key, record):
        records = deepcopy(self.records); records[key] = deepcopy(record)
        await self.save(records)
        self.guard()

    async def capture(self, key, binding):
        if key in self.records:
            return
        async with self.setup.lock:
            self.guard()
            if self.setup.records.get(key) != binding:
                raise ValueError('battery binding changed before ownership capture')
            normal_state(binding)  # Validate the complete handover before owning anything.
            expected = {role: self.value(binding, role) for role in WRITE_ROLES}
            await self.persist_record(key, {'binding': deepcopy(binding), 'expected': expected, 'pending': None,
                                            'phase': 'owned', 'last_issued': None})

    async def external_change(self, key):
        record = self.records[key]
        changed = []
        for role, expected in record['expected'].items():
            _entity, state, cap = self.entity(record['binding'], role)
            # Even a prohibited external mode or unset ceiling is evidence of
            # another writer. Latch it before applying writable-value validation.
            current = finite_number(state.state) if cap['operation'] == 'set_number' else state.state
            pending = record['pending']
            if current != expected and not (pending and pending['role'] == role and current == pending['value']):
                changed.append(role)
        if changed:
            overrides = {**self.overrides, key: 'External change to ' + ', '.join(changed) + '; reviewed handover required'}
            await self.save(overrides=overrides)
            self.agreement.active = None
            raise ValueError(overrides[key])
        if key in self.overrides:
            raise ValueError(self.overrides[key])

    async def settle_pending(self, key):
        """A timed-out or interrupted write must settle before any restoring write."""
        record = self.records[key]
        pending = record['pending']
        if not pending:
            return
        _entity, state, _cap = self.entity(record['binding'], pending['role'])
        if self.value(record['binding'], pending['role']) != pending['value'] or state.last_reported < utc(pending['issued_at']):
            raise ValueError('handover pending: last battery write has not been acknowledged')
        updated = deepcopy(record)
        updated['expected'][pending['role']] = pending['value']; updated['pending'] = None
        await self.persist_record(key, updated)

    async def confirm(self, predicate, message):
        deadline = self.monotonic() + self.confirm_seconds
        while True:
            self.guard()
            if predicate(): return
            if self.monotonic() >= deadline: raise ValueError(message)
            await self.sleep(.25)
            self.guard()

    async def write(self, key, role, value):
        self.guard()
        await self.external_change(key)
        self.guard()
        record = self.records[key]
        if record['pending']:
            raise ValueError('previous battery write remains unacknowledged')
        binding = record['binding']
        self.valid_value(binding, role, value)
        entity_id, state, _cap = self.entity(binding, role)
        domain, service, data, value, equal = build_command(entity_id, state.state, state.attributes, value)
        if equal: return
        issued = self.wall().isoformat().replace('+00:00', 'Z')
        updated = deepcopy(record)
        updated['pending'] = {'role': role, 'value': value, 'issued_at': issued}
        updated['last_issued'] = issued
        submitted = False
        try:
            await self.persist_record(key, updated)
            self.guard()
            # Resolve again after the durable write-ahead await.
            if self.entity(binding, role)[0] != entity_id:
                raise ValueError('entity renamed during battery write; retry after handover')
            await self.external_change(key)
            self.guard()
            if self.context and role.endswith('_ceiling'):
                control = next(c for c in self.agreement.saved['definition']['controls'] if c['control_id'] == key)
                remaining = (utc(self.agreement.plan['valid_until_utc']) - self.wall()).total_seconds()
                safe = desired_state(control, self.context[1]['instruction'], self.measurements(binding), binding, self.scope(), remaining)
                if value > safe[role] + 1e-9:
                    raise ValueError('live SOC or grid headroom changed before the battery write')
            submitted = True
            await asyncio.wait_for(self.hass.services.async_call(domain, service, {'entity_id': entity_id, **data}, blocking=True), self.confirm_seconds)
        except BaseException:
            if not submitted and not self.storage_fault and key in self.records:
                # This process knows it never submitted this write. An old
                # persisted pending record on restart has no such evidence.
                restored = deepcopy(self.records); restored[key] = deepcopy(record)
                await self.save(restored)
            raise
        self.guard()
        await self.confirm(lambda: self.value(binding, role) == value and self.entity(binding, role)[1].last_reported >= utc(issued),
                           role + ' did not acknowledge the setting')
        await self.settle_pending(key)
        self.guard()

    async def transition(self, key, settings):
        """Restrict both directions before claiming/changing mode; relax after readback."""
        record = self.records[key]; binding = record['binding']
        for role, value in settings.items(): self.valid_value(binding, role, value)
        for role in binding['spec']['roles']: self.entity(binding, role)
        claiming = self.value(binding, 'authority') != 'on'
        self.remote_guard = None if claiming else key
        for role in ('charge_ceiling', 'discharge_ceiling'):
            await self.write(key, role, 0 if claiming else min(self.value(binding, role), settings[role]))
            self.guard()
        await self.write(key, 'authority', 'on')
        self.guard()
        authority_after = self.records[key]['last_issued'] if claiming else None
        await self.confirm(lambda: self.value(binding, 'authority_confirmation') == 'Remote EMS' and
                           (not authority_after or self.entity(binding, 'authority_confirmation')[1].last_reported >= utc(authority_after)),
                           'Remote EMS authority was not confirmed')
        self.remote_guard = key
        self.guard()
        await self.write(key, 'mode', settings['mode'])
        self.guard()
        for role in ('charge_ceiling', 'discharge_ceiling'):
            if self.value(binding, 'authority_confirmation') != 'Remote EMS':
                raise ValueError('Remote EMS authority was lost')
            await self.write(key, role, settings[role])
            self.guard()

    async def handover(self, key):
        self.context = None
        self.remote_guard = None
        self.agreement.active = None
        if key not in self.records: return
        await self.external_change(key)
        await self.settle_pending(key)
        record = deepcopy(self.records[key]); record['phase'] = 'handover'
        await self.persist_record(key, record)
        settings = normal_state(record['binding'])
        # An externally lost authority is not permission to fight a new owner.
        if self.value(record['binding'], 'authority') == 'on' and self.value(record['binding'], 'authority_confirmation') != 'Remote EMS':
            raise ValueError('handover pending: Remote EMS authority lost; review the plant owner')
        await self.transition(key, settings)
        measured = self.measurements(record['binding'], after=self.records[key]['last_issued'])
        result = operation('self_consumption', settings, measured, record['binding'])
        records = deepcopy(self.records); del records[key]
        await self.save(records)
        self.remote_guard = None
        self.report('baseline', control_id=key, settings_acknowledged=True, **{k: v for k, v in result.items() if k != 'state'},
                    handover='Reviewed normal self-consumption restored; remote authority remains on')

    async def async_review_handover(self, key, expected_revision, reviewed):
        """A local administrator may acknowledge a physically completed handover.

        This never actuates the plant or resumes a pending dispatch. An uncertain
        submitted write must acknowledge before its ownership can be released.
        """
        async with self.lock:
            self.agreement.revoke()
            if not self.loaded or self.storage_fault:
                await self.load()
            record = self.records.get(key)
            if reviewed is not True or not record or record['binding']['binding_revision'] != expected_revision:
                raise ValueError('Review the owned binding revision before acknowledging handover')
            if record['pending']:
                raise ValueError('The pending battery write must acknowledge before reviewed handover')
            binding = record['binding']
            settings = normal_state(binding)
            if self.value(binding, 'authority') != 'on' or self.value(binding, 'authority_confirmation') != 'Remote EMS':
                raise ValueError('Reviewed handover requires confirmed Remote EMS authority')
            if any(self.value(binding, role) != value for role, value in settings.items()):
                raise ValueError('The plant has not reached its reviewed normal profile')
            result = operation('self_consumption', settings, self.measurements(binding, after=record['last_issued']), binding)
            records, overrides = deepcopy(self.records), deepcopy(self.overrides)
            del records[key]
            overrides.pop(key, None)
            await self.save(records, overrides)
            self.report('baseline', control_id=key, settings_acknowledged=True,
                        **{k: v for k, v in result.items() if k != 'state'}, handover='Local administrator reviewed normal handover')
            return deepcopy(self.status)

    async def async_start(self):
        self.legacy_ready()
        self.agreement.revoke()
        await self.load()
        # Ownership never resumes from a journal or cached plan.
        async with self.lock:
            for key in tuple(self.records):
                try: await self.handover(key)
                except Exception as err: self.report('handover_pending', control_id=key, reason=str(err))

    async def async_tick(self, _now=None):
        if self.closed or self.lock.locked(): return
        async with self.lock:
            try:
                if not self.loaded or self.storage_fault:
                    await self.load()
                    for key in tuple(self.records): await self.handover(key)
                definition = self.agreement.saved['definition']
                controls = [c for c in (definition or {}).get('controls', []) if c['contract']['name'] == 'battery_dispatch']
                if len(controls) > 1: raise ValueError('only one battery association is supported')
                control = controls[0] if controls else None
                key = control['control_id'] if control else None
                plan = self.agreement.plan
                command = next((c for c in (plan or {}).get('commands', []) if c['control_id'] == key), None)
                for owned in tuple(self.records):
                    if owned != key or self.records[owned]['phase'] == 'handover': await self.handover(owned)
                if not command:
                    for owned in tuple(self.records): await self.handover(owned)
                    if not self.records: self.report('disabled', reason='No accepted battery instruction; operation permission is local')
                    return
                if self.plan_scope and self.plan_scope[0] == plan['plan_id'] and self.plan_scope[1] != self.scope():
                    self.agreement.revoke()
                    raise ValueError('Battery physical settings changed; a new plan is required')
                self.plan_scope = (plan['plan_id'], deepcopy(self.scope()))
                self.context = (key, deepcopy(command), plan['plan_id'], self.scope())
                self.guard()
                validate(plan, definition)
                binding = deepcopy(self.setup.records[key])
                if binding['binding_revision'] != command['accepted']['binding']:
                    raise ValueError('battery binding revision changed')
                if key in self.records and self.records[key]['binding'] != binding:
                    raise ValueError('owned battery binding changed; old binding must hand over')
                measurements = self.measurements(binding)
                seconds = (utc(plan['valid_until_utc']) - self.wall()).total_seconds()
                settings = desired_state(control, command['instruction'], measurements, binding, self.scope(), seconds)
                await self.capture(key, binding)
                await self.external_change(key)
                self.guard()
                if self.records[key]['pending']: raise ValueError('battery write is still pending')
                if self.records[key]['expected']['authority'] == 'on' and self.value(binding, 'authority_confirmation') != 'Remote EMS':
                    raise ValueError('Remote EMS authority was lost')
                await self.transition(key, settings)
                self.guard()
                try:
                    measured = self.measurements(binding, after=self.records[key]['last_issued'])
                except ValueError as err:
                    self.report('awaiting_observation', control_id=key, settings_acknowledged=True, reason=str(err))
                    self.agreement.active = None
                    return
                result = operation(command['instruction']['intent'], settings, measured, binding)
                self.report(**result, control_id=key, settings_acknowledged=True, intent=command['instruction']['intent'],
                            settings=settings, plan_id=plan['plan_id'], accepted=revision(control))
                self.agreement.active = {'plan_id': plan['plan_id'], 'accepted': deepcopy(self.agreement.saved['accepted']),
                    'observed_at_utc': self.wall().isoformat().replace('+00:00', 'Z'), 'controls': {key: deepcopy(self.status)}}
            except Exception as err:
                self.context = None
                self.agreement.active = None
                reason = str(err)
                if not self.storage_fault:
                    for owned in tuple(self.records):
                        try: await self.handover(owned)
                        except Exception as restore_error: reason += '; ' + str(restore_error)
                self.report('handover_pending' if self.records or self.storage_fault else 'disabled', reason=reason)
            finally:
                self.context = None
                self.remote_guard = None

    async def async_stop(self, _event=None):
        self.closed = True
        self.agreement.active = None
        async with self.lock:
            self.context = None
            for key in tuple(self.records):
                try: await self.handover(key)
                except Exception as err: self.report('handover_pending', control_id=key, reason=str(err))
