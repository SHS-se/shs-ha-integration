"""The app's durable room map and its independently retried cloud replicas."""
from uuid import uuid4

from shs_core.configuration_schema import ConfigurationFieldErrors


def connections(value):
    """Canonical undirected pairs; retired doorway labels carry no meaning."""
    if not isinstance(value, list):
        raise ConfigurationFieldErrors({'connections': ['Choose a list of room connections.']})
    result = []
    seen = set()
    for edge in value:
        pair = edge.get('rooms') if isinstance(edge, dict) else None
        if (not isinstance(pair, list) or len(pair) != 2
                or any(not isinstance(key, str) or not key.strip() for key in pair) or pair[0] == pair[1]):
            raise ConfigurationFieldErrors({'connections': ['Choose two different rooms for each doorway.']})
        pair = tuple(sorted(pair))
        if pair in seen:
            raise ConfigurationFieldErrors({'connections': ['That doorway is already listed.']})
        seen.add(pair)
        result.append({'rooms': list(pair)})
    return sorted(result, key=lambda edge: edge['rooms'])


class RoomConnections:
    def __init__(self, engine):
        self.engine = engine
        self.confirmed = {}

    async def save(self, body, rooms):
        configuration = self.engine.configuration

        async def update(existing):
            previous = existing.get('room_connections')
            if previous is None:
                raise ConfigurationFieldErrors({'connections': ['Wait for the saved connections to be recovered.']})
            proposed = connections(body['connections'])
            for edge in proposed:
                if edge not in previous['connections'] and any(key not in rooms for key in edge['rooms']):
                    raise ConfigurationFieldErrors({'connections': ['Choose rooms that are still available in Home Assistant.']})
            return {**existing, 'room_connections': {
                'revision': configuration.revision + 1, 'connections': proposed}}

        await configuration.commit(body['expected_revision'], update, body['request_id'],
                                   intent={'connections': body['connections']})
        self.engine.wake_projection.set()

    async def sync(self, environment):
        configuration = self.engine.configuration
        client = self.engine.households[environment].client
        if 'room_connections' not in configuration.options():
            if environment != self.engine.environment:
                return
            recovered = connections((await client.read_room_connections())['connections'])

            async def adopt(existing):
                # A late download never replaces a completed adoption or edit.
                if 'room_connections' in existing:
                    return existing
                return {**existing, 'room_connections': {
                    'revision': configuration.revision + 1, 'connections': recovered}}

            await configuration.commit(configuration.revision, adopt, uuid4().hex,
                                       intent={'adopt_room_connections': recovered})
            self.engine.wake_projection.set()
        document = configuration.options()['room_connections']
        publication = {'owner': self.engine.identity['migration_id'], **document}
        if self.confirmed.get(environment) == publication:
            return
        receipt = await client.publish_room_connections(publication)
        if receipt != publication:
            raise ValueError('The website did not confirm the app room connections. SHS will retry.')
        self.confirmed[environment] = publication
        self.engine.wake_projection.set()
