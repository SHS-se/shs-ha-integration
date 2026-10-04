"""Durable captured settings owned by the device gateway."""
from copy import deepcopy


def decode_ownership(value):
    """Decode the existing controller Store without acquiring hardware ownership.

    This decoder is also used by dormant migration checks. It does not consult
    live entities. Retired run clocks are discarded on decode.
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
    return records, overrides, deepcopy(value.get('retired_pool_temperature_settings', {}))


class DeviceOwnership:
    """Single mutable owner of restoration records.

    Methods are called under the gateway's command lock. Save captures a private
    value before awaiting storage; later in-memory mutations cannot change what
    that save means.
    """
    def __init__(self, store):
        self.store = store
        self.records = {}
        self.overrides = {}
        self.retired_pool_temperature_settings = {}

    async def load(self):
        value = await self.store.async_load()
        decoded = decode_ownership(value if value is not None else {})
        self.records, self.overrides, self.retired_pool_temperature_settings = decoded

    def snapshot(self):
        return deepcopy({'records': self.records, 'overrides': self.overrides,
                         'retired_pool_temperature_settings': self.retired_pool_temperature_settings})

    async def save(self):
        await self.store.async_save(self.snapshot())

    async def capture(self, device, options, originals):
        if device in self.records:
            return self.records[device]
        self.records[device] = {'options': deepcopy(options), 'originals': deepcopy(originals)}
        await self.save()
        return self.records[device]
