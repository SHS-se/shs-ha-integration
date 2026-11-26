"""Room-map ownership, migration and publication use durable app settings."""
import asyncio
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import AsyncMock

from shs_app.configuration import Configuration
from shs_app.room_connections import RoomConnections
from shs_core.gateway_journal import GatewayConflict, digest
from shs_core.configuration_schema import ConfigurationFieldErrors
from shs_core.native_configuration import native_options


PAIR = {'rooms': ['hall', 'office']}


class RoomConnectionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.identity = {'migration_id': 'synthetic-installation'}
        self.installed = {'device_modes': {'heater': 'controlling'}}

        async def install(body):
            self.installed = deepcopy(body['options'])
            return {'revision': body['revision'], 'digest': digest(body['options'])}

        self.install = install
        self.configuration = Configuration(self.root, self.identity, install, project=native_options)
        await self.configuration.load({'revision': 0, 'options': self.installed}, AsyncMock(return_value={}))
        self.production = SimpleNamespace(client=SimpleNamespace(
            read_room_connections=AsyncMock(return_value={'connections': [PAIR]}),
            publish_room_connections=AsyncMock(side_effect=lambda value: value)))
        self.observer = SimpleNamespace(client=SimpleNamespace(
            read_room_connections=AsyncMock(return_value={'connections': [{'rooms': ['hall', 'garage']}]}),
            publish_room_connections=AsyncMock(side_effect=lambda value: value)))
        self.engine = SimpleNamespace(configuration=self.configuration, identity=self.identity,
            environment='production', households={'production': self.production, 'test': self.observer},
            wake_projection=asyncio.Event())
        self.maps = RoomConnections(self.engine)

    async def save(self, connections, request='edit', revision=None):
        await self.maps.save({'expected_revision': revision or self.configuration.revision,
                             'request_id': request, 'connections': connections},
                            {'hall': 'Hall', 'office': 'Office', 'garage': 'Garage'})

    async def test_import_once_from_selected_backend_and_persist_across_restart(self):
        await self.maps.sync('test')
        self.observer.client.read_room_connections.assert_not_awaited()
        self.assertNotIn('room_connections', self.configuration.options())
        await self.maps.sync('production')
        await self.maps.sync('production')
        self.production.client.read_room_connections.assert_awaited_once()
        self.assertEqual(self.configuration.options()['room_connections']['connections'], [PAIR])
        reopened = Configuration(self.root, self.identity, self.install, project=native_options)
        await reopened.load({'revision': self.configuration.revision, 'digest': digest(self.installed)}, AsyncMock())
        self.assertEqual(reopened.options(), self.configuration.options())
        self.assertEqual(self.installed, {'device_modes': {'heater': 'controlling'}})

    async def test_add_delete_and_retry_are_local_and_both_backends_receive_the_map(self):
        await self.maps.sync('production')
        revision = self.configuration.revision
        await self.save([], revision=revision)
        saved = self.configuration.options()['room_connections']
        await self.save([], revision=revision)
        self.assertEqual(self.configuration.options()['room_connections'], saved)
        self.production.client.publish_room_connections.side_effect = OSError('offline')
        with self.assertRaises(OSError):
            await self.maps.sync('production')
        await self.maps.sync('test')
        self.assertEqual(self.observer.client.publish_room_connections.await_args.args[0]['connections'], [])
        await self.save([{'rooms': ['garage', 'hall']}], request='add')
        self.assertEqual(self.configuration.options()['room_connections']['connections'], [{'rooms': ['garage', 'hall']}])
        self.assertEqual(self.installed, {'device_modes': {'heater': 'controlling'}})

    async def test_stale_editor_conflicts_without_losing_saved_deletion(self):
        await self.maps.sync('production')
        revision = self.configuration.revision
        await self.save([])
        with self.assertRaises(GatewayConflict):
            await self.save([PAIR], request='stale', revision=revision)
        self.assertEqual(self.configuration.options()['room_connections']['connections'], [])

    async def test_unavailable_import_does_not_create_an_empty_map(self):
        self.production.client.read_room_connections.side_effect = OSError('offline')
        with self.assertRaises(OSError):
            await self.maps.sync('production')
        self.assertNotIn('room_connections', self.configuration.options())
        with self.assertRaises(ConfigurationFieldErrors):
            await self.save([])

    async def test_late_import_cannot_replace_a_map_already_adopted(self):
        async def late_reply():
            await self.configuration.commit(self.configuration.revision,
                {**self.configuration.options(), 'room_connections': {'revision': 7, 'connections': []}}, 'other-import')
            return {'connections': [PAIR]}
        self.production.client.read_room_connections.side_effect = late_reply
        await self.maps.sync('production')
        self.assertEqual(self.configuration.options()['room_connections']['connections'], [])

    async def test_missing_room_edges_survive_import_and_are_explicitly_removable(self):
        pair = {'rooms': ['hall', 'removed_room']}
        self.production.client.read_room_connections.return_value = {'connections': [pair]}
        await self.maps.sync('production')
        await self.save([pair, PAIR])
        self.assertIn(pair, self.configuration.options()['room_connections']['connections'])
        await self.save([PAIR], request='remove-missing')
        self.assertEqual(self.configuration.options()['room_connections']['connections'], [PAIR])

    async def test_rejects_invalid_new_connections_with_structured_correction(self):
        await self.maps.sync('production')
        for value in ([{'rooms': ['hall', 'missing']}], [PAIR, {'rooms': ['office', 'hall']}],
                      [{'rooms': ['hall', 'hall']}], [{'rooms': ['hall']}], 'invalid'):
            with self.assertRaises(ConfigurationFieldErrors) as error:
                await self.save(value)
            self.assertIn('connections', error.exception.field_errors)
        self.assertEqual(self.configuration.options()['room_connections']['connections'], [PAIR])

    async def test_unconfirmed_replica_is_retried_without_replacing_local_settings(self):
        await self.maps.sync('production')
        await self.save([])
        self.production.client.publish_room_connections.side_effect = None
        self.production.client.publish_room_connections.return_value = {'owner': 'different', 'revision': 99, 'connections': [PAIR]}
        with self.assertRaises(ValueError):
            await self.maps.sync('production')
        self.assertEqual(self.configuration.options()['room_connections']['connections'], [])
