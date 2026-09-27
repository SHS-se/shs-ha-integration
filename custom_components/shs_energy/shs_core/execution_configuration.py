"""HA's durable, app-authored configuration replica; never a settings editor."""
import asyncio
from copy import deepcopy

from .gateway_journal import GatewayConflict, digest


class ExecutionConfiguration:
    def __init__(self, record, invalidate, capture):
        self.record, self.invalidate, self.capture = record, invalidate, capture
        self.lock = asyncio.Lock()
        self.value = None

    async def load(self, initial):
        self.value = await self.record.async_load()
        if self.value is None:
            if initial.get('settings_owner') == 'app':
                raise GatewayConflict('Installed execution configuration is missing after app adoption')
            self.value = dict(revision=0, options=deepcopy(initial), digest=digest(initial))
            await self.record.async_save(self.value)
        if (set(self.value) != {'revision','options','digest'} or type(self.value['revision']) is not int
                or self.value['revision'] < 0 or type(self.value['options']) is not dict
                or self.value['digest'] != digest(self.value['options'])):
            raise GatewayConflict('Invalid installed SHS execution configuration')

    def options(self):
        return deepcopy(self.value['options'])

    async def install(self, change):
        if (type(change) is not dict or set(change) != {'expected_revision','revision','options','digest'}
                or any(type(change[key]) is not int or change[key] < 0 for key in ('expected_revision','revision'))
                or change['revision'] < 1 or type(change['options']) is not dict
                or change['digest'] != digest(change['options'])):
            raise ValueError('Invalid execution configuration revision')
        async with self.lock:
            revision = self.value['revision']
            if change['revision'] == revision:
                if change['digest'] != self.value['digest']:
                    raise GatewayConflict('Execution configuration revision reused with different content')
                # Resume capture after a lost reply or interrupted capture.
                await self.capture()
            else:
                if change['expected_revision'] != revision or change['revision'] != revision + 1:
                    raise GatewayConflict('Execution configuration changed; reconcile the app settings revision')
                self.invalidate()
                updated = {key:deepcopy(change[key]) for key in ('revision','options','digest')}
                await self.record.async_save(updated)
                self.value = updated
                await self.capture()
            return {key:self.value[key] for key in ('revision','digest')}
