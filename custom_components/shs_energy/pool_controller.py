"""Journalled scheduling requests to a customer-owned HA script interface.

The customer owns Nibe, circulation, interlocks, expiry and physical handover.
This module has no scalar actuator command or dependency discovery path.
"""
import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import time

if __package__:
    from . import pool_policy
    from .contract_v1.validate import validate, revision, utc
    from .control_capabilities import finite_number
    from .control_setup import resolve, identity, opaque_id, validate_binding_record
else:
    import pool_policy
    from contract_v1.validate import validate, revision, utc
    from control_capabilities import finite_number
    from control_setup import resolve, identity, opaque_id, validate_binding_record

RETRY_SECONDS = 5
ACK_SECONDS = 15
RENEW_SECONDS = 30


class PoolController:
    def __init__(self, hass, setup, agreement, store, *, report=None, wall=None, monotonic=None,
                 legacy_ready=None, service_timeout=15):
        self.hass, self.setup, self.agreement, self.store = hass, setup, agreement, store
        self.report_callback = report or (lambda **_status: None)
        self.wall = wall or (lambda: datetime.now(timezone.utc))
        self.monotonic = monotonic or time.monotonic
        self.legacy_ready = legacy_ready or (lambda: None)
        self.service_timeout = service_timeout
        self.records, self.sequences, self.overrides = {}, {}, {}
        self.status = {'state': 'disabled'}
        self.lock = asyncio.Lock()
        self.loaded = self.storage_fault = self.closed = False
        self.context = None

    def report(self, state, **details):
        key = details.get('control_id') or (next(iter(self.records)) if len(self.records) == 1 else None)
        if key in self.records:
            record = self.records[key]
            details.update(control_id=key, owned_binding_revision=record['binding']['binding_revision'],
                           request_id=record['request']['request_id'], request_sequence=record['request']['request_sequence'])
        self.status = {'state': state, **details}
        self.report_callback(**self.status)

    async def save(self, records=None, sequences=None, overrides=None):
        values = deepcopy(dict(records=self.records if records is None else records,
                               sequences=self.sequences if sequences is None else sequences,
                               overrides=self.overrides if overrides is None else overrides))
        try:
            await self.store.async_save(dict(schema_version=1, home_id=self.setup.home_id, **values))
        except BaseException:
            self.storage_fault = True
            self.agreement.revoke()
            raise
        self.records, self.sequences, self.overrides = values['records'], values['sequences'], values['overrides']

    async def load(self):
        self.storage_fault = True
        payload = await self.store.async_load()
        if payload is None:
            self.records, self.sequences, self.overrides = {}, {}, {}
        else:
            if (not isinstance(payload, dict) or set(payload) != {'schema_version', 'home_id', 'records', 'sequences', 'overrides'}
                or payload['schema_version'] != 1 or payload['home_id'] != self.setup.home_id
                or any(not isinstance(payload[k], dict) for k in ('records', 'sequences', 'overrides'))):
                raise ValueError('invalid pool request journal')
            for key, seq in payload['sequences'].items():
                opaque_id(key)
                if type(seq) is not int or seq < 1: raise ValueError('invalid pool request sequence')
            for key, r in payload['records'].items():
                if not isinstance(r, dict) or set(r) != {'binding', 'definition', 'request', 'phase', 'last_sent_at', 'feedback'}:
                    raise ValueError('invalid pool ownership record')
                validate_binding_record(key, r['binding'])
                spec = r['binding']['spec']
                if spec['contract'] != {'name': 'pool_service', 'version': 1} or set(spec['roles']) != {'request', 'feedback', 'water_temperature'}:
                    raise ValueError('pool journal contains an internal actuator')
                if r['binding']['claims'] != [list(identity(spec['roles']['request']))] or r['binding']['members']:
                    raise ValueError('pool journal may reserve only its customer request interface')
                validate(r['definition']); validate(r['request'], r['definition'])
                if r['definition']['home_id'] != self.setup.home_id or r['request']['control_id'] != key:
                    raise ValueError('foreign pool ownership')
                if r['request']['request_sequence'] != payload['sequences'].get(key):
                    raise ValueError('pool request sequence journal mismatch')
                c = next(c for c in r['definition']['controls'] if c['control_id'] == key)
                if c['local']['binding_revision'] != r['binding']['binding_revision'] or c['local']['capabilities'] != r['binding']['capabilities'] or c['local']['limits'] != spec['limits']:
                    raise ValueError('pool journal binding and accepted definition differ')
                if r['phase'] not in ('owned', 'release', 'released') or (r['phase'] == 'owned') == (r['request']['action'] == 'release'):
                    raise ValueError('invalid pool release phase')
                if r['last_sent_at'] is not None: utc(r['last_sent_at'])
                if r['phase'] == 'released':
                    pool_policy.feedback(r['feedback'], r['request'], r['definition'], utc(r['feedback']['reported_at_utc']))
                    if r['feedback']['acknowledgement'] != 'released': raise ValueError('release not acknowledged')
                elif r['feedback'] is not None: raise ValueError('unexpected persisted pool feedback')
            if not set(payload['overrides']) <= set(payload['records']) or not all(isinstance(v, str) and v for v in payload['overrides'].values()):
                raise ValueError('invalid pool override journal')
            self.records, self.sequences, self.overrides = (deepcopy(payload[k]) for k in ('records', 'sequences', 'overrides'))
        self.loaded, self.storage_fault = True, False

    def entity(self, binding, role, *, fresh=False):
        entity = resolve(binding['spec']['roles'][role], self.setup.inventory())
        cap = next(c for c in binding['capabilities'] if c['role'] == role)
        if {'role': role, **entity['capability']} != cap:
            raise ValueError('pool interface capability changed; review the binding')
        if role == 'request' and (entity['registry']['domain'] != 'script' or not entity.get('accepts_request')):
            raise ValueError('customer script must accept a complete request object')
        state = self.hass.states.get(entity['entity_id'])
        if state is None or state.state in ('unknown', 'unavailable'):
            raise ValueError(role + ' is unavailable')
        if fresh and not -5 <= (self.wall()-state.last_reported).total_seconds() <= 120:
            raise ValueError(role + ' is stale or future-dated')
        return entity['entity_id'], state

    def water(self, binding):
        _entity, state = self.entity(binding, 'water_temperature', fresh=True)
        return finite_number(state.state)

    def guard(self):
        self.legacy_ready()
        if self.storage_fault: raise ValueError('pool journal persistence is unconfirmed')
        if self.context is None: return  # Persisted release authority only.
        key, command, plan_id = self.context
        self.setup.assert_adapter_ownership(key, {'name': 'pool_service', 'version': 1})
        if self.closed or key in self.overrides or self.agreement.guard(key, command['accepted'], plan_id) != command:
            raise ValueError('pool authority changed or customer override is latched')
        binding = self.setup.records[key]
        self.water(binding)
        if key in self.records and self.records[key]['binding'] != binding:
            raise ValueError('pool binding changed; release the owned interface first')

    def read_feedback(self, key):
        r = self.records[key]
        _entity, state = self.entity(r['binding'], 'feedback', fresh=True)
        return pool_policy.feedback(state.attributes.get('feedback'), r['request'], r['definition'], self.wall())

    def authorization_expiry(self):
        return min(utc(self.agreement.lease['expires_at_utc']), utc(self.agreement.plan['valid_until_utc']),
                   self.wall() + timedelta(seconds=self.agreement.deadline-self.monotonic()))

    async def reject(self, key, value):
        await self.save(overrides={**self.overrides, key: value['reason']})
        raise ValueError('Customer rejected pool scheduling: ' + value['reason'])

    async def prepare(self, key, *, binding=None, command=None, plan_id=None):
        self.guard()
        prior = self.records.get(key)
        definition = deepcopy(self.agreement.saved['definition'] if command else prior['definition'])
        binding = deepcopy(binding if command else prior['binding'])
        expires = None
        if command:
            expires = self.authorization_expiry()
        seq = self.sequences.get(key, 0) + 1
        sent = pool_policy.request(definition, key, seq, self.wall(), command=command, plan_id=plan_id, expires=expires)
        records = {**self.records, key: dict(binding=binding, definition=definition, request=sent,
                   phase='owned' if command else 'release', last_sent_at=None, feedback=None)}
        # Binding edits serialize with the first ownership claim; every later
        # awaited boundary rechecks the exact accepted binding before dispatch.
        async with self.setup.lock:
            self.guard()
            await self.save(records, {**self.sequences, key: seq})
        self.guard()

    def request_guard(self, key):
        self.guard()
        sent = self.records[key]['request']
        if not utc(sent['issued_at_utc']) <= self.wall() < utc(sent['expires_at_utc']):
            raise ValueError('pool request expired or clock moved before issue')
        if self.context and (utc(sent['expires_at_utc']) > utc(self.agreement.lease['expires_at_utc']) or
                (utc(sent['expires_at_utc'])-self.wall()).total_seconds() > self.agreement.deadline-self.monotonic()+.001):
            raise ValueError('pool request exceeds current authority')

    async def send(self, key):
        self.request_guard(key)
        r = self.records[key]; sent = r['request']
        entity_id, _state = self.entity(r['binding'], 'request')
        records = deepcopy(self.records); records[key]['last_sent_at'] = pool_policy.stamp(self.wall())
        await self.save(records)
        self.guard()
        if self.entity(r['binding'], 'request')[0] != entity_id:
            raise ValueError('customer script renamed during request preparation')
        self.request_guard(key)
        # Direct customer service, one request argument, no entity_id/variables
        # wrapper and no inferred private actuator sequence.
        await asyncio.wait_for(self.hass.services.async_call('script', entity_id.split('.', 1)[1],
                               {'request': deepcopy(sent)}, blocking=True), self.service_timeout)
        self.guard()

    def retry_due(self, record):
        return record['last_sent_at'] is None or (self.wall()-utc(record['last_sent_at'])).total_seconds() >= RETRY_SECONDS

    async def release(self, key, *, force=False):
        self.context = None
        self.agreement.clear_operation(key)
        r = self.records[key]
        if r['phase'] == 'released':
            self.report('overridden', control_id=key, reason=self.overrides[key], request_acknowledged=True,
                        operation='released', release_acknowledged=True)
            return
        if r['phase'] != 'release' or self.wall() >= utc(r['request']['expires_at_utc']):
            await self.prepare(key)
        try: value = self.read_feedback(key)
        except (ValueError, TypeError, KeyError, StopIteration): value = None
        if value is None or value['acknowledgement'] != 'released':
            if force or self.retry_due(self.records[key]): await self.send(key)
            try: value = self.read_feedback(key)
            except (ValueError, TypeError, KeyError, StopIteration): value = None
        if value and value['acknowledgement'] == 'released':
            records = deepcopy(self.records)
            if key in self.overrides:
                records[key].update(phase='released', feedback=value)
            else:
                del records[key]
            await self.save(records)
            self.report('overridden' if key in self.overrides else 'released', control_id=key,
                        request_acknowledged=True, release_acknowledged=True, operation='released',
                        reason=self.overrides.get(key, value['reason']))
        else:
            self.report('release_pending', control_id=key, request_acknowledged=False,
                        release_acknowledged=False, operation='unknown',
                        reason=value['reason'] if value else 'Awaiting correlated customer release feedback')

    async def observe(self, key):
        self.request_guard(key)
        r = self.records[key]
        try:
            value = self.read_feedback(key)
        except (ValueError, TypeError, KeyError, StopIteration) as err:
            self.agreement.clear_operation(key)
            if (self.wall()-utc(r['request']['issued_at_utc'])).total_seconds() >= ACK_SECONDS:
                raise ValueError('Customer acknowledgement timed out: ' + str(err))
            self.report('awaiting_acknowledgement', control_id=key, request_acknowledged=False,
                        settings_acknowledged=False, operation='unknown', reason=str(err))
            return False
        if value['acknowledgement'] == 'rejected':
            await self.reject(key, value)
        self.report(value['operation'], control_id=key, request_acknowledged=True, settings_acknowledged=True,
                    operation=value['operation'], reason=value['reason'], feedback_reported_at_utc=value['reported_at_utc'],
                    plan_id=r['request']['plan_id'], accepted=r['request']['accepted'],
                    authorization_expires_at_utc=r['request']['expires_at_utc'], water_temperature_c=self.water(r['binding']))
        self.agreement.report_operation(key, self.status)
        return True

    async def async_start(self):
        self.legacy_ready()
        self.agreement.revoke()
        await self.load()
        async with self.lock:
            for key in tuple(self.records):
                try: await self.release(key, force=True)
                except Exception as err: self.report('release_pending', control_id=key, reason=str(err))

    async def async_tick(self, _now=None):
        if self.closed or self.lock.locked(): return
        async with self.lock:
            try:
                if not self.loaded or self.storage_fault:
                    await self.load()
                    for key in tuple(self.records): await self.release(key)
                definition = self.agreement.saved['definition']
                controls = [c for c in (definition or {}).get('controls', []) if c['contract']['name'] == 'pool_service']
                if len(controls) > 1: raise ValueError('only one pool service association is supported')
                key = controls[0]['control_id'] if controls else None
                plan = self.agreement.plan
                command = next((c for c in (plan or {}).get('commands', []) if c['control_id'] == key), None)
                for owned in tuple(self.records):
                    if owned != key or self.records[owned]['phase'] != 'owned': await self.release(owned)
                if self.records and any(r['phase'] != 'owned' for r in self.records.values()): return
                if not command:
                    for owned in tuple(self.records): await self.release(owned)
                    if not self.records: self.report('disabled', reason='No accepted pool instruction; permission is local')
                    return
                self.context = (key, deepcopy(command), plan['plan_id'])
                self.guard(); validate(plan, definition)
                r = self.records.get(key)
                acknowledged = None
                if r:
                    try: acknowledged = self.read_feedback(key)
                    except (ValueError, TypeError, KeyError, StopIteration): pass
                    if acknowledged and acknowledged['acknowledgement'] == 'rejected':
                        await self.reject(key, acknowledged)
                # Requests have their own immutable expiry. A lease renewal does
                # not silently extend a request already held by the customer.
                changed = r and (r['request']['accepted'] != command['accepted'] or r['request']['plan_id'] != plan['plan_id']
                    or r['request']['action'] != command['instruction']['request'] or r['request']['objective'] != controls[0]['desired']['objective'])
                renew = r and acknowledged and (utc(r['request']['expires_at_utc'])-self.wall()).total_seconds() <= RENEW_SECONDS and self.authorization_expiry() > utc(r['request']['expires_at_utc'])
                if not r or changed or renew:
                    await self.prepare(key, binding=self.setup.records[key], command=command, plan_id=plan['plan_id'])
                    await self.send(key)
                elif self.retry_due(r):
                    try: self.read_feedback(key)
                    except (ValueError, TypeError, KeyError, StopIteration):
                        if (self.wall()-utc(r['request']['issued_at_utc'])).total_seconds() < ACK_SECONDS:
                            await self.send(key)  # Exact idempotent retry, same sequence and expiry.
                await self.observe(key)
            except Exception as err:
                self.agreement.revoke()
                self.context = None
                reason = str(err)
                if not self.storage_fault:
                    for owned in tuple(self.records):
                        try: await self.release(owned)
                        except Exception as release_error: reason += '; ' + str(release_error)
                state = 'overridden' if self.overrides and all(r['phase'] == 'released' for r in self.records.values()) else 'release_pending' if self.records or self.storage_fault else 'disabled'
                self.report(state, reason=reason)
            finally:
                self.context = None

    async def async_review_release(self, key, expected_revision, reviewed):
        async with self.lock:
            if not self.loaded or self.storage_fault: await self.load()
            r = self.records.get(key)
            if reviewed is not True or type(expected_revision) is not int or not r or r['phase'] != 'released' or r['binding']['binding_revision'] != expected_revision:
                raise ValueError('Review the owned revision after acknowledged customer release')
            self.agreement.revoke()
            records, overrides = deepcopy(self.records), deepcopy(self.overrides)
            del records[key]; overrides.pop(key, None)
            await self.save(records, overrides=overrides)
            self.report('released', control_id=key, release_acknowledged=True, reason='Customer release reviewed; operation permission remains local')
            return deepcopy(self.status)

    async def async_stop(self, _event=None):
        self.closed = True
        async with self.lock:
            self.context = None
            for key in tuple(self.records):
                try: await self.release(key, force=True)
                except Exception as err: self.report('release_pending', control_id=key, reason=str(err))
