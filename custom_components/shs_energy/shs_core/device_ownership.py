"""Durable captured settings and appliance obligations owned by the device gateway."""
from copy import deepcopy

from .minimum_run import MinimumRuns


def decode_ownership(value):
    """Decode the existing controller Store without acquiring hardware ownership.

    This decoder is also used by dormant migration checks. It neither consults
    live entities nor changes the saved run clocks.
    """
    if type(value) is not dict:
        raise ValueError('Invalid device ownership record')
    allowed = {'records', 'overrides', 'retired_pool_temperature_settings', 'runs'}
    if set(value) - allowed or any(type(item) is not dict for item in value.values()):
        raise ValueError('Unsupported device ownership record')
    records = deepcopy(value.get('records', {}))
    for device, record in records.items():
        if (type(device) is not str or not device or type(record) is not dict
                or type(record.get('options')) is not dict or type(record.get('originals')) is not dict
                or any(type(entity) is not str or not entity for entity in record['originals'])):
            raise ValueError('Invalid captured device settings')
    overrides = deepcopy(value.get('overrides', {}))
    if any(type(key) is not str or type(reason) is not str for key, reason in overrides.items()):
        raise ValueError('Invalid device override record')
    return (records, overrides, deepcopy(value.get('retired_pool_temperature_settings', {})),
            MinimumRuns(value.get('runs', {})))


class DeviceOwnership:
    """Single mutable owner of restoration records and minimum-run promises.

    Methods are called under the gateway's command lock. Save captures a private
    value before awaiting storage; later in-memory mutations cannot change what
    that save means or mark a newer run revision as persisted.
    """
    def __init__(self, store):
        self.store = store
        self.records = {}
        self.overrides = {}
        self.retired_pool_temperature_settings = {}
        self.runs = MinimumRuns()

    async def load(self):
        value = await self.store.async_load()
        decoded = decode_ownership(value if value is not None else {})
        self.records, self.overrides, self.retired_pool_temperature_settings, self.runs = decoded

    def snapshot(self):
        return deepcopy({'records': self.records, 'overrides': self.overrides,
                         'retired_pool_temperature_settings': self.retired_pool_temperature_settings,
                         'runs': self.runs.records})

    async def save(self):
        revision = self.runs.revision
        await self.store.async_save(self.snapshot())
        self.runs.saved_revision = revision

    async def capture(self, device, options, originals):
        if device in self.records:
            return self.records[device]
        self.records[device] = {'options': deepcopy(options), 'originals': deepcopy(originals)}
        await self.save()
        return self.records[device]
