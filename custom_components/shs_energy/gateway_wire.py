"""HA transport framing and source dependency discovery, without runtime policy."""
from hashlib import sha256
import json


def filter_sources(entities, read, platform):
    result, pending = set(entities), list(entities)
    while pending:
        entity = pending.pop()
        state = read(entity)
        source = state.attributes.get('entity_id') if state and platform(entity) == 'filter' else None
        if isinstance(source, str) and source and source not in result:
            result.add(source)
            pending.append(source)
    return result


class ProjectionAssembly:
    """One bounded, checksummed projection per authenticated socket."""
    def __init__(self):
        self.identity = None
        self.parts = []
        self.size = 0

    def receive(self, body):
        if type(body) is not dict or set(body) != {'transfer','index','last','data','sha256'}:
            raise ValueError('Invalid projection chunk')
        key, index, last, data, digest = (body[k] for k in ('transfer','index','last','data','sha256'))
        if (type(key) is not str or not 1 <= len(key) <= 64 or type(index) is not int or index < 0
                or type(last) is not bool or type(data) is not str or len(data.encode()) > 512*1024
                or type(digest) is not str or len(digest) != 64):
            raise ValueError('Invalid projection chunk framing')
        if index == 0:
            self.identity, self.parts, self.size = (key,digest), [], 0
        if self.identity != (key,digest) or index != len(self.parts):
            raise ValueError('Projection chunks are out of order')
        size = len(data.encode())
        if self.size + size > 32*1024*1024:
            raise ValueError('Projection exceeds the transport budget')
        self.size += size
        self.parts.append(data)
        if not last:
            return None
        content = ''.join(self.parts).encode()
        self.identity, self.parts, self.size = None, [], 0
        if sha256(content).hexdigest() != digest:
            raise ValueError('Projection checksum mismatch')
        value = json.loads(content)
        if type(value) is not dict:
            raise ValueError('Projection must be an object')
        return value
