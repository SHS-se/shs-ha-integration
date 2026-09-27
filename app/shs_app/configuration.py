"""The single app-owned settings writer and its acknowledged HA projection."""
import asyncio
from copy import deepcopy
from uuid import uuid4

from shs_core.gateway_journal import GatewayConflict, digest
from .records import RecordStore


class Configuration:
    def __init__(self, root, identity, install):
        self.record = RecordStore(root / 'configuration.json')
        self.identity, self.install = identity, install
        self.lock = asyncio.Lock()
        self.data = None

    @property
    def revision(self):
        return self.data['applied_revision']

    def options(self):
        return deepcopy(self.data['applied_options'])

    def credentials(self):
        return deepcopy(self.data['credentials'])

    def status(self):
        return dict(revision=self.revision, desired_revision=self.data['revision'],
                    state='applied' if self.revision == self.data['revision'] else 'pending')

    async def load(self, installed, credentials):
        async with self.lock:
            self.data = await self.record.async_load()
            if self.data is None:
                if installed['revision'] != 0:
                    raise GatewayConflict('App settings are missing after adoption; restore the app configuration record')
                self.data = dict(schema=1, identity=self.identity, revision=1, applied_revision=0,
                    options=deepcopy(installed['options']), applied_options=deepcopy(installed['options']),
                    credentials=await credentials(), adopted_digest=digest(installed['options']), requests={})
                await self.record.async_save(self.data)
            elif self.data.get('schema') != 1 or self.data.get('identity') != self.identity:
                raise GatewayConflict('Unsupported app configuration schema or installation identity')
            await self._apply()

    async def _apply(self):
        desired = self.data
        result = await self.install(dict(expected_revision=desired['applied_revision'],
            revision=desired['revision'], options=desired['options'], digest=digest(desired['options'])))
        if result != {'revision':desired['revision'], 'digest':digest(desired['options'])}:
            raise GatewayConflict('HA did not acknowledge the desired configuration revision')
        if desired['applied_revision'] != desired['revision']:
            updated = dict(desired, applied_revision=desired['revision'], applied_options=deepcopy(desired['options']))
            await self.record.async_save(updated)
            self.data = updated

    async def commit(self, expected_revision, options, request_id):
        if type(expected_revision) is not int or type(options) is not dict or type(request_id) is not str or not request_id:
            raise ValueError('A configuration revision, document and request identity are required')
        async with self.lock:
            request_digest = digest(dict(expected_revision=expected_revision, options=options))
            prior = self.data['requests'].get(request_id)
            if prior:
                if prior['digest'] != request_digest:
                    raise GatewayConflict('Configuration request identity reused with different values')
                await self._apply()
                return {'revision':prior['revision']}
            if self.data['revision'] != self.revision:
                raise GatewayConflict('A configuration change is still waiting for Home Assistant; reconnect before editing')
            if expected_revision != self.revision:
                raise GatewayConflict('Configuration changed in another window or Home Assistant; refresh before saving')
            revision = self.revision + 1
            requests = dict(list(self.data['requests'].items())[-127:])
            requests[request_id] = dict(digest=request_digest, revision=revision)
            updated = dict(self.data, revision=revision, options=deepcopy(options), requests=requests)
            await self.record.async_save(updated)
            self.data = updated
            await self._apply()
            return {'revision':revision}

    async def admit(self, expected, admitted):
        if expected != self.options():
            raise GatewayConflict('Configuration changed during planning admission')
        await self.commit(self.revision, admitted, uuid4().hex)
