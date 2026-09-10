"""Exercise capability, composed setup, ownership and durable save boundaries."""
import asyncio
from copy import deepcopy
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / 'custom_components/shs_energy'))
from control_capabilities import build_command, primitive
from control_setup import ControlSetup, PRESETS, describe_inventory, discover, identity, setup_report, reserved_claims, validate_legacy_targets, VerifiedBindingStore

HOME = '00000000-0000-4000-8000-000000000001'
CONTROL = '00000000-0000-4000-8000-000000000002'
OTHER = '00000000-0000-4000-8000-000000000003'


def entity(entity_name, *, platform='unfamiliar', unique=None, state='on', device='plant', **attributes):
    return {'registry': {'domain': entity_name.split('.')[0], 'platform': platform, 'unique_id': unique or entity_name},
            'name': entity_name, 'device_id': device, 'state': state, 'attributes': attributes,
            'manufacturer': '  Sigenergy  ', 'model': '  SigenStor   EC 12 ', 'accepts_request': False}


def relay(inventory, target='switch.relay', control_id=CONTROL):
    return {'control_id': control_id, 'contract': {'name': 'relay_schedule', 'version': 1},
            'roles': {'output': inventory[target]['registry']},
            'limits': {'minimum_on_seconds': 30, 'minimum_off_seconds': 60},
            'normal_profile': {'output': 'off'}, 'handover': 'restore_reviewed_profile'}


def pool():
    raw = {'script.request': entity('script.request', platform='script', state='off'),
           'sensor.feedback': entity('sensor.feedback', platform='template', state='idle'),
           'sensor.water': entity('sensor.water', state='25', unit_of_measurement='°C')}
    raw['script.request']['accepts_request'] = True
    inv = describe_inventory(raw)
    spec = {'control_id': CONTROL, 'contract': {'name': 'pool_service', 'version': 1},
            'roles': {role: inv[key]['registry'] for role, key in zip(('request','feedback','water_temperature'), raw)},
            'limits': {'minimum_c': 20, 'maximum_c': 32, 'step_c': .1},
            'handover': 'release_customer_automation', 'normal_profile': {'policy': 'customer_automation'},
            'interface_review': {'request_contract_version': 1, 'expiry_handling_reviewed': True,
                                 'feedback_correlation_reviewed': True, 'release_policy_reviewed': True}}
    return inv, spec


def battery():
    raw, roles = {}, {}
    for role, requirement in PRESETS['contracts']['battery_dispatch']['roles'].items():
        operation, unit = requirement['operation'], requirement['units'][0]
        domain = {'select':'select','switch':'switch','observe':'sensor','set_number':'number'}[operation]
        key = domain + '.' + role
        attrs = {'unit_of_measurement': unit}
        if operation == 'set_number':
            attrs.update(min=0, max=12000, step=1)
        if operation == 'select':
            attrs['options'] = list(set(PRESETS['sigenergy']['modes'].values()))
        raw[key] = entity(key, **attrs)
        raw[key]['name'] = ' '.join(requirement['tokens'])
        roles[role] = raw[key]['registry']
    inv = describe_inventory(raw)
    spec = {'control_id': CONTROL, 'contract': {'name': 'battery_dispatch', 'version': 1}, 'roles': roles,
            'limits': {'charge_max_w': 8800, 'discharge_max_w': 9600, 'normal_charge_w': 8000,
                       'normal_discharge_w': 9000, 'minimum_soc_pct': 5, 'maximum_soc_pct': 95},
            'handover': 'reviewed_self_consumption', 'normal_profile': {'mode': 'Maximum Self Consumption'},
            'semantics': {'preset':'sigenergy','measurement_charge_positive':True}}
    return inv, spec


class CapabilityTests(unittest.TestCase):
    def test_generic_switch_and_normalized_metadata_need_no_manufacturer_preset(self):
        unknown = entity('switch.relay'); unknown.update(manufacturer=None, model=None)
        inv = describe_inventory({'switch.relay': unknown})
        report = setup_report(relay(inv), inv)
        self.assertEqual(report['status'], 'ready')
        self.assertFalse(report['control_enabled'])
        self.assertEqual(report['agreement'], 'not_evaluated')
        self.assertEqual(inv['switch.relay']['manufacturer'], '')
        self.assertEqual(inv['switch.relay']['model'], '')
        self.assertNotIn('state', report['capabilities'][0])
        self.assertEqual(build_command('switch.relay', 'on', {}, 'off')[1], 'turn_off')

    def test_number_unit_meaning_bounds_and_steps_are_independent(self):
        inv = describe_inventory({'number.output': entity('number.output', min=0, max=20, step=.5, unit_of_measurement='A')})
        spec = relay(inv, 'number.output')
        spec.update(contract={'name':'adjustable_output','version':1}, limits={'minimum':1,'maximum':16}, normal_profile={'output':5})
        self.assertEqual(setup_report(spec, inv)['status'], 'invalid')
        spec['semantics'] = {'meaning':'current_limit','phase_count':3,'voltage':230}
        self.assertEqual(setup_report(spec, inv)['status'], 'ready')
        for value in (16.2, 21, float('nan'), True):
            with self.subTest(value=value), self.assertRaises(ValueError):
                build_command('number.output', '5', inv['number.output']['attributes'], value)
        spec['limits']['maximum'] = 21
        self.assertEqual(setup_report(spec, inv)['status'], 'invalid')

    def test_thermostat_requires_supported_feature_and_celsius(self):
        attrs = {'min_temp':5,'max_temp':35,'target_temp_step':.5,'temperature':20}
        with self.assertRaisesRegex(ValueError, 'single target'):
            primitive('climate', attrs)
        attrs['supported_features'] = 1
        self.assertEqual(build_command('climate.test', 'heat', attrs, 21)[1], 'set_temperature')
        for state, unit, target in [('off','°C',21), ('heat','°F',21), ('heat','°C',21.1)]:
            with self.subTest(state=state, unit=unit, target=target), self.assertRaises(ValueError):
                build_command('climate.test', state, attrs, target, temperature_unit=unit)

    def test_pool_requires_customer_interface_not_hardware_dependencies(self):
        inv, spec = pool()
        self.assertEqual(setup_report(spec, inv)['status'], 'ready')
        self.assertEqual(setup_report(spec, inv)['claims'], [list(identity(inv['script.request']['registry']))])
        for role in ('pump', 'nibe_band'):
            proposal = deepcopy(spec); proposal['roles'][role] = inv['script.request']['registry']
            self.assertEqual(setup_report(proposal, inv)['status'], 'invalid')
        spec.pop('interface_review')
        self.assertIn('customer_interface_review_required', [e['code'] for e in setup_report(spec, inv)['errors']])
        inv, spec = pool(); inv['script.request']['accepts_request'] = False
        self.assertEqual(setup_report(spec, inv)['status'], 'invalid')
        inv, spec = pool(); inv['sensor.water']['capability']['unit'] = 'W'
        self.assertIn('requires observe in °C', str(setup_report(spec, inv)['errors']))

    def test_battery_full_state_normal_profile_and_mode_policy(self):
        inv, spec = battery()
        self.assertEqual(setup_report(spec, inv)['status'], 'ready')
        for change in ('sentinel','negative','soc','mode','plant','role_overlap','unit','inverter'):
            values, proposal = deepcopy(inv), deepcopy(spec)
            if change == 'sentinel': proposal['limits']['normal_charge_w'] = 4294967295
            if change == 'negative': proposal['limits']['discharge_max_w'] = -1
            if change == 'soc': proposal['limits']['minimum_soc_pct'] = 98
            if change == 'mode': values['select.mode']['capability']['options'].remove('Standby')
            if change == 'plant': values['sensor.soc']['device_id'] = 'another-plant'
            if change == 'role_overlap': proposal['roles']['discharge_ceiling'] = proposal['roles']['charge_ceiling']
            if change == 'unit': values['number.charge_ceiling']['capability']['unit'] = 'A'
            if change == 'inverter': values['number.charge_ceiling']['capability']['minimum'] = -10000
            with self.subTest(change=change):
                self.assertEqual(setup_report(proposal, values)['status'], 'invalid')

    def test_discovery_preserves_explicit_values_and_offers_ambiguous_candidates(self):
        inv, spec = battery(); before = deepcopy(spec)
        self.assertEqual(inv['select.mode']['model'], 'SigenStor EC 12')
        other = deepcopy(inv['number.charge_ceiling']); other['registry']['unique_id'] = 'another'
        other['entity_id'] = 'number.other'; inv['number.other'] = other
        report = discover(inv)
        self.assertEqual(len(report['candidates']['battery_dispatch']['charge_ceiling']), 2)
        self.assertEqual(spec, before)
        self.assertFalse(report['control_enabled'])
        self.assertNotIn('bindings', report)

    def test_unknown_and_ambiguous_registry_id_never_uses_a_similar_name(self):
        inv = describe_inventory({'switch.relay': entity('switch.relay')}); spec = relay(inv)
        unknown = deepcopy(spec); unknown['roles']['output']['unique_id'] = 'replaced'
        self.assertIn('explicit replacement', str(setup_report(unknown, inv)['errors']))
        inv['switch.duplicate'] = deepcopy(inv['switch.relay'])
        self.assertIn('ambiguously', str(setup_report(spec, inv)['errors']))


class Store:
    def __init__(self): self.saved = None; self.calls = 0; self.fail = False; self.during_save = None
    async def async_load(self): return deepcopy(self.saved)
    async def async_save(self, payload):
        self.calls += 1
        if self.fail: raise OSError('disk failure')
        if self.during_save: await self.during_save()
        self.saved = deepcopy(payload)


class DurableSetupTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.raw = {'switch.relay': entity('switch.relay'), 'switch.second': entity('switch.second')}
        self.store = Store()
        self.setup = ControlSetup(self.store, HOME, lambda: describe_inventory(self.raw), lambda _: {})
        self.spec = relay(describe_inventory(self.raw))

    async def test_save_is_durable_before_ack_and_no_permission_is_granted(self):
        saved = await self.setup.async_save(self.spec, 0)
        self.assertEqual(self.store.saved['records'][CONTROL]['binding_revision'], 1)
        self.assertEqual(saved['binding_revision'], 1)
        self.assertFalse(saved['control_enabled'])
        self.assertEqual(saved['agreement'], 'not_evaluated')
        replay = await self.setup.async_save(self.spec, 1)
        self.assertEqual(replay['binding_revision'], 1)
        self.assertEqual(self.store.calls, 1)
        with self.assertRaisesRegex(ValueError, 'revision conflict'):
            await self.setup.async_save(self.spec, 0)

    async def test_disk_failure_does_not_change_memory_or_acknowledge(self):
        self.store.fail = True
        with self.assertRaises(OSError): await self.setup.async_save(self.spec, 0)
        self.assertEqual(self.setup.records, {})
        self.assertIsNone(self.store.saved)

    async def test_interrupted_response_reloads_the_same_persisted_revision(self):
        await self.setup.async_save(self.spec, 0)
        restarted = ControlSetup(self.store, HOME, self.setup.inventory, lambda _: {})
        await restarted.async_load()
        result = await restarted.async_save(self.spec, 1)
        self.assertEqual(result['binding_revision'], 1)
        self.assertFalse(result['control_enabled'])
        self.assertEqual(self.store.calls, 1)

    async def test_rename_preserves_identity_revision_and_reviewed_settings(self):
        await self.setup.async_save(self.spec, 0)
        self.raw['switch.renamed'] = self.raw.pop('switch.relay')
        report = self.setup.report(self.spec)
        self.assertEqual(report['resolved']['output'], 'switch.renamed')
        self.assertEqual((await self.setup.async_save(self.spec, 1))['binding_revision'], 1)
        self.assertEqual(self.setup.records[CONTROL]['spec'], self.spec)

    async def test_group_members_reserved_even_when_other_control_is_disabled(self):
        self.raw['switch.group'] = entity('switch.group', platform='group', entity_id=['switch.relay'])
        group = relay(describe_inventory(self.raw), 'switch.group')
        await self.setup.async_save(group, 0)
        direct = relay(describe_inventory(self.raw), control_id=OTHER)
        with self.assertRaisesRegex(ValueError, 'reserved'):
            await self.setup.async_save(direct, 0)
        self.raw['switch.group']['attributes']['entity_id'].append('switch.second')
        self.assertIn('group_members_changed', str(self.setup.report(group)['errors']))
        self.assertEqual(len(self.setup.local_binding(CONTROL)['roles'][0]['members']), 1)
        saved = await self.setup.async_save(group, 1)
        self.assertEqual(saved['binding_revision'], 2)
        self.assertEqual(len(self.setup.local_binding(CONTROL)['roles'][0]['members']), 2)

    async def test_partial_overlap_cycle_and_external_owner_fail_before_saving(self):
        self.raw['switch.group'] = entity('switch.group', platform='group', entity_id=['switch.group'])
        with self.assertRaisesRegex(ValueError, 'cyclic'):
            await self.setup.async_save(relay(describe_inventory(self.raw), 'switch.group'), 0)
        self.setup.external_claims = lambda _: {'old-journal': {identity(self.spec['roles']['output'])}}
        with self.assertRaisesRegex(ValueError, 'old-journal'): await self.setup.async_save(self.spec, 0)
        self.assertEqual(self.store.calls, 0)

    async def test_changed_capability_needs_new_binding_and_does_not_overwrite_limits(self):
        inv, spec = battery(); source = deepcopy(inv)
        setup = ControlSetup(self.store, HOME, lambda: deepcopy(source), lambda _: {})
        await setup.async_save(spec, 0)
        source['number.charge_ceiling']['capability']['maximum'] = 11000
        self.assertIn('capabilities_changed', str(setup.report(spec)['errors']))
        saved = await setup.async_save(spec, 1)
        self.assertEqual(saved['binding_revision'], 2)
        self.assertEqual(setup.records[CONTROL]['spec']['limits'], spec['limits'])

    async def test_hardware_change_during_save_cannot_return_ready(self):
        async def remove(): self.raw.pop('switch.relay')
        self.store.during_save = remove
        report = await self.setup.async_save(self.spec, 0)
        self.assertTrue(report['saved'])
        self.assertEqual(report['status'], 'invalid')

    async def test_simultaneous_saves_serialize_and_reserve_once(self):
        other = deepcopy(self.spec); other['control_id'] = OTHER
        results = await asyncio.gather(self.setup.async_save(self.spec, 0), self.setup.async_save(other, 0), return_exceptions=True)
        self.assertEqual(sum(isinstance(r, ValueError) for r in results), 1)
        self.assertEqual(len(self.setup.records), 1)

    async def test_target_reports_work_without_mapping_or_live_writes(self):
        report = self.setup.summary()
        self.assertEqual(len(report['targets']), 2)
        self.assertTrue(all(r['errors'] for r in report['targets']))
        self.assertEqual(self.store.calls, 0)
        self.assertFalse(report['control_enabled'])

    async def test_old_editor_cannot_claim_a_new_control_group_member(self):
        await self.setup.async_save(self.spec, 0)
        self.raw['switch.group'] = entity('switch.group', platform='group', entity_id=['switch.relay'])
        observed = describe_inventory(self.raw)
        reservations = {CONTROL: reserved_claims(self.setup.records[CONTROL], observed)}
        options = {'device_control_mappings': {'old': {'actuator_entity_ids': ['switch.group']}}}
        with self.assertRaisesRegex(ValueError, 'reserved'):
            validate_legacy_targets(options, observed, reservations)

    async def test_separate_entries_share_lock_and_cannot_reserve_twice(self):
        other_store = Store()
        other = ControlSetup(other_store, OTHER, self.setup.inventory,
            lambda inv: {key: reserved_claims(r, inv) for key, r in self.setup.records.items()},
            lock=self.setup.lock)
        self.setup.external_claims = lambda inv: {key: reserved_claims(r, inv) for key, r in other.records.items()}
        spec = deepcopy(self.spec); spec['control_id'] = OTHER
        results = await asyncio.gather(self.setup.async_save(self.spec, 0), other.async_save(spec, 0), return_exceptions=True)
        self.assertEqual(sum(isinstance(r, ValueError) for r in results), 1)
        self.assertEqual(len(self.setup.records) + len(other.records), 1)

    async def test_foreign_home_store_rejected(self):
        await self.setup.async_save(self.spec, 0)
        setup = ControlSetup(self.store, OTHER, self.setup.inventory, lambda _: {})
        with self.assertRaisesRegex(ValueError, 'foreign-home'): await setup.async_load()

    async def test_local_binding_matches_step_one_schema(self):
        inv, spec = pool()
        setup = ControlSetup(self.store, HOME, lambda: inv, lambda _: {})
        await setup.async_save(spec, 0)
        schema = json.loads((ROOT / 'contracts/control/v1/schema.json').read_text())
        from jsonschema import Draft7Validator
        Draft7Validator(schema).validate(setup.local_binding(CONTROL))


class VerifiedStoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_logged_write_failure_is_not_a_successful_save(self):
        store = Store()
        async def disk(): return None
        verified = VerifiedBindingStore(store, disk, 'test', lambda: False)
        with self.assertRaisesRegex(OSError, 'did not persist'):
            await verified.async_save({'new': 'settings'})

    async def test_stopping_cannot_queue_a_write_then_claim_success(self):
        store = Store()
        async def disk(): return None
        verified = VerifiedBindingStore(store, disk, 'test', lambda: True)
        with self.assertRaisesRegex(OSError, 'stopping'):
            await verified.async_save({'new': 'settings'})
        self.assertEqual(store.calls, 0)

    async def test_disk_envelope_round_trip_and_wrong_version_rejection(self):
        store = Store()
        async def disk():
            return {'version': 1, 'minor_version': 1, 'key': 'test', 'data': store.saved}
        verified = VerifiedBindingStore(store, disk, 'test', lambda: False)
        await verified.async_save({'new': 'settings'})
        self.assertEqual(await verified.async_load(), {'new': 'settings'})
        async def wrong(): return {'version': 2, 'minor_version': 1, 'key': 'test', 'data': {}}
        verified.read_disk = wrong
        with self.assertRaisesRegex(ValueError, 'envelope'):
            await verified.async_load()
