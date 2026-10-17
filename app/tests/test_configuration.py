"""Configuration acknowledgement, conflicts and recovery across lost replies."""
import asyncio
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from shs_app.configuration import Configuration
from shs_app.records import RecordStore
from shs_core.execution_configuration import ExecutionConfiguration
from shs_core.gateway_journal import digest
from shs_core.native_configuration import native_options


class ConfigurationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.invalidations, self.captures = 0, []
        def invalidate(): self.invalidations += 1
        async def capture(): self.captures.append(deepcopy(self.gateway.value))
        self.gateway = ExecutionConfiguration(RecordStore(self.root/'gateway.json'),invalidate,capture)
        await self.gateway.load({'device_modes':{'battery':'control_verification'}})
        self.credentials_calls = 0
        async def credentials():
            self.credentials_calls += 1
            return {'device_token':'private-app-credential'}
        self.credentials = credentials
        self.app = Configuration(self.root,{'installation':'original'},self.gateway.install,project=deepcopy)
        await self.app.load(self.gateway.value,credentials)

    async def test_adoption_is_once_and_mode_success_means_native_revision_applied(self):
        result = await self.app.commit(1,{'device_modes':{'battery':'controlling'}},'ha-mode')
        self.assertEqual(result,{'revision':2})
        self.assertEqual(self.app.options(),self.gateway.options())
        self.assertEqual(self.captures[-1]['revision'],2)
        reopened = Configuration(self.root,{'installation':'original'},self.gateway.install,project=deepcopy)
        await reopened.load(self.gateway.value,self.credentials)
        self.assertEqual(reopened.options(),self.app.options())
        self.assertEqual(self.credentials_calls,1)

    async def test_reload_removes_obsolete_local_import_limit_from_both_records(self):
        options = {**self.app.options(), 'grid_import_limit_w': 13200, 'grid_export_limit_w': 13200}
        await self.app.commit(self.app.revision, options, 'old-local-limit')
        reopened = Configuration(self.root, {'installation':'original'}, self.gateway.install, project=deepcopy)
        await reopened.load(self.gateway.value, self.credentials)
        self.assertNotIn('grid_import_limit_w', reopened.options())
        self.assertNotIn('grid_import_limit_w', self.gateway.options())
        self.assertEqual(reopened.options()['grid_export_limit_w'], 13200)
        self.assertEqual(reopened.options()['device_modes'], options['device_modes'])

    async def test_upgrade_retires_manual_maximum_without_changing_modes_or_mapping(self):
        mapping=dict(control_type='switch_schedule',actuator_entity_ids=['switch.pool'],
            power='sensor.pool',maximum_power_w=1234)
        options={**self.app.options(),'device_modes':{'pool':'controlling'},
            'device_control_mappings':{'pool':mapping}}
        await self.app.commit(self.app.revision,options,'retire-obsolete-planning-fields-v3')
        reopened=Configuration(self.root,{'installation':'original'},self.gateway.install,project=deepcopy)
        await reopened.load(self.gateway.value,self.credentials)
        self.assertNotIn('maximum_power_w',reopened.options()['device_control_mappings']['pool'])
        self.assertEqual(reopened.options()['device_modes'],{'pool':'controlling'})
        self.assertEqual(reopened.options()['device_control_mappings']['pool'],
            {key:value for key,value in mapping.items() if key!='maximum_power_w'})
        self.assertEqual(reopened.options(),self.gateway.options())

    async def test_lost_reply_retains_pending_then_reconciles_without_duplicate_revision(self):
        async def lose_reply(body):
            await self.gateway.install(body)
            raise OSError('reply lost')
        previous = self.app.options()
        self.app.install = lose_reply
        with self.assertRaises(OSError):
            await self.app.commit(1,{'device_modes':{}},'edit')
        self.assertEqual(self.app.status()['state'],'pending')
        self.assertEqual(self.app.options(),previous)
        with self.assertRaisesRegex(ValueError,'still waiting'):
            await self.app.commit(1,{},'other')
        reopened = Configuration(self.root,{'installation':'original'},self.gateway.install,project=deepcopy)
        await reopened.load(self.gateway.value,self.credentials)
        count = self.invalidations
        await reopened.commit(1,{'device_modes':{}},'edit')
        self.assertEqual(reopened.revision,2)
        self.assertEqual(self.invalidations,count)
        with self.assertRaisesRegex(ValueError,'reused'):
            await reopened.commit(1,{},'edit')

    async def test_concurrent_ha_and_browser_edits_cannot_overwrite_each_other(self):
        results = await asyncio.gather(self.app.commit(1,{'mode':'ha'},'ha'),
            self.app.commit(1,{'mode':'browser'},'browser'),return_exceptions=True)
        self.assertEqual(sum(isinstance(r,ValueError) for r in results),1)
        self.assertEqual(self.app.revision,2)
        self.assertEqual(self.gateway.options(),self.app.options())

    async def test_gateway_rejects_stale_conflicting_or_tampered_configuration(self):
        for body in (dict(expected_revision=0,revision=2,options={},digest=digest({})),
                     dict(expected_revision=1,revision=1,options={},digest=digest({})),
                     dict(expected_revision=1,revision=2,options={},digest='wrong')):
            with self.assertRaises(ValueError): await self.gateway.install(body)
        self.assertEqual(self.gateway.value['revision'],1)

    async def test_missing_app_record_is_not_readopted_after_ownership_transfer(self):
        other = self.root/'empty';other.mkdir()
        app = Configuration(other,{'installation':'original'},self.gateway.install,project=deepcopy)
        with self.assertRaisesRegex(ValueError,'missing after adoption'):
            await app.load(self.gateway.value,self.credentials)

    async def test_changed_native_projection_rolls_forward_once_preserving_saved_settings(self):
        options = {'device_modes': {'water': 'controlling'},
                   'house_consumption_power_entity': 'sensor.house_power',
                   'device_control_mappings': {'water': {'control_type': 'permit_inhibit',
                       'actuator_entity_ids': ['switch.water'], 'power': 3000}},
                   'private_app_setting': 'retained'}
        def previous_projection(value):
            return {key: item for key, item in native_options(value).items()
                    if key != 'house_consumption_power_entity'}
        self.app.project = previous_projection
        await self.app.commit(1, options, 'old-version-settings')
        before = deepcopy(self.app.data)
        installed = deepcopy(self.gateway.value)
        reopened = Configuration(self.root, {'installation': 'original'},
                                 self.gateway.install, project=native_options)
        await reopened.load(installed, self.credentials)
        self.assertEqual(reopened.revision, 3)
        self.assertEqual(reopened.data, dict(before, revision=3, applied_revision=3))
        self.assertEqual(reopened.options(), options)
        self.assertEqual(self.gateway.options(), native_options(options))
        self.assertEqual(self.gateway.options()['device_modes'], options['device_modes'])
        self.assertEqual(self.credentials_calls, 1)
        await reopened.load(self.gateway.value, self.credentials)
        self.assertEqual(reopened.revision, 3)

    async def test_projection_upgrade_recovers_lost_acknowledgement_without_another_revision(self):
        async def lose_reply(body):
            saved = await self.app.record.async_load()
            self.assertEqual(saved['revision'], body['revision'])
            self.assertEqual(saved['applied_revision'], 2)
            await self.gateway.install(body)
            raise OSError('upgrade reply lost')
        # The previous release installed the full document; this release sends
        # only the native execution subset, just as adding a field changes it.
        reopened = Configuration(self.root, {'installation': 'original'},
                                 lose_reply, project=native_options)
        # Include an app-only value so the projection actually changes.
        await self.app.commit(1, dict(self.app.options(), private_setting='retained'), 'app-only')
        before = deepcopy(self.app.data)
        with self.assertRaisesRegex(OSError, 'upgrade reply lost'):
            await reopened.load(self.gateway.value, self.credentials)
        self.assertEqual(reopened.status(), dict(revision=2, desired_revision=3, state='pending'))
        self.assertEqual(reopened.options(), before['applied_options'])
        replacement = Configuration(self.root, {'installation': 'original'},
                                    self.gateway.install, project=native_options)
        await replacement.load(self.gateway.value, self.credentials)
        self.assertEqual(replacement.data, dict(before, revision=3, applied_revision=3))
        self.assertEqual(self.gateway.value['revision'], 3)

    async def test_startup_deletes_obsolete_hot_water_fields_and_keeps_the_power_sensor(self):
        options = {'device_modes': {'water': 'controlling'}, 'device_control_mappings': {
            'water': {'control_type': 'permit_inhibit', 'actuator_entity_ids': ['switch.water'],
                      'power': 'sensor.water_power', 'max_inhibit_slots': 4, 'running_power_w': 3000}}}
        await self.app.commit(1, options, 'old-water-settings')
        reopened = Configuration(self.root, {'installation': 'original'}, self.gateway.install, project=deepcopy)
        await reopened.load(self.gateway.value, self.credentials)
        self.assertEqual(reopened.options()['device_modes'], options['device_modes'])
        expected = deepcopy(options)
        expected['device_control_mappings']['water'].pop('max_inhibit_slots')
        expected['device_control_mappings']['water'].pop('running_power_w')
        self.assertEqual(reopened.options(), expected)
        self.assertEqual(reopened.options(), self.gateway.options())
        revision = reopened.revision
        await reopened.load(self.gateway.value, self.credentials)
        self.assertEqual(reopened.revision, revision)

    async def test_pause_setting_deletion_recovers_lost_reply_without_changing_other_settings(self):
        options = {'device_modes': {'water': 'controlling'}, 'device_control_mappings': {
            'water': {'control_type': 'permit_inhibit', 'actuator_entity_ids': ['switch.water'],
                      'power': 'sensor.water_power', 'max_inhibit_slots': 4, 'running_power_w': 3000}}, 'private_setting': 'retained'}
        await self.app.commit(1, options, 'old-water-settings')
        async def lose_reply(body):
            result = await self.gateway.install(body)
            if body['revision'] == 3:
                raise OSError('deletion reply lost')
            return result
        reopened = Configuration(self.root, {'installation': 'original'}, lose_reply, project=deepcopy)
        with self.assertRaisesRegex(OSError, 'deletion reply lost'):
            await reopened.load(self.gateway.value, self.credentials)
        self.assertEqual(reopened.options(), options)
        replacement = Configuration(self.root, {'installation': 'original'}, self.gateway.install, project=deepcopy)
        await replacement.load(self.gateway.value, self.credentials)
        expected = deepcopy(options)
        expected['device_control_mappings']['water'].pop('max_inhibit_slots')
        expected['device_control_mappings']['water'].pop('running_power_w')
        self.assertEqual(replacement.options(), expected)
        self.assertEqual(replacement.revision, 3)
        await replacement.load(self.gateway.value, self.credentials)
        self.assertEqual(replacement.revision, 3)

    async def test_startup_preserves_saved_appliance_settings_without_creating_a_revision(self):
        options = {'device_modes': {'pool': 'controlling'}, 'device_control_mappings': {
            'pool': {'control_type': 'switch_schedule', 'actuator_entity_ids': ['switch.pool'],
                     'power': 12000, 'minimum_on_seconds': 14400, 'minimum_off_seconds': 60}}}
        await self.app.commit(1, options, 'old-settings')
        before = deepcopy(self.app.data)
        reopened = Configuration(self.root, {'installation': 'original'}, self.gateway.install, project=deepcopy)
        await reopened.load(self.gateway.value, self.credentials)
        self.assertEqual(reopened.data, before)
        self.assertEqual(reopened.options(), options)
        self.assertEqual(reopened.options(), self.gateway.options())
        await reopened.load(self.gateway.value, self.credentials)
        self.assertEqual(reopened.data, before)
