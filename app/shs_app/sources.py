"""Ordered observations and read-only recorder ports for the app household."""
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from shs_core.household_ports import HomeFacts


def timestamp(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00'))


@dataclass(frozen=True)
class Observation:
    state: str
    attributes: dict
    last_updated: datetime
    last_reported: datetime
    last_changed: datetime


class ObservationMirror:
    def __init__(self):
        self.context = None
        self.rows = {}
        self.listeners = {}
        self.revision = 0
        self.changes = {}
        self.durable_rows = self.rows
        self.row_receipts = {}

    def install_context(self, context):
        self.context = deepcopy(context)

    def home(self):
        h = self.context['home']
        return HomeFacts(h['latitude'], h['longitude'], h['language'], ZoneInfo(h['timezone']))

    def options(self):
        return deepcopy(self.context['options'])

    def report(self, entity):
        return self.rows.get(entity)

    def read(self, entity):
        row = self.rows.get(entity)
        if row is None or row['state'] is None:
            return None
        return Observation(row['state'], row['attributes'], timestamp(row['last_updated']),
                           timestamp(row['last_reported']), timestamp(row['last_changed']))

    def subscribe(self, entity, notify):
        self.listeners.setdefault(entity, set()).add(notify)
        return lambda:self.listeners.get(entity, set()).discard(notify)

    def apply(self, receipt, *, notify=True):
        # Receipt ordinals establish arrival order; timestamps remain provenance.
        self.revision = receipt['ordinal']
        self.changes[self.revision] = deepcopy(receipt)
        if receipt['kind'] == 'observation':
            row = deepcopy(receipt['payload'])
            entity = row['entity_id']
            self.row_receipts[entity] = self.revision
            if row['state'] is None:
                self.rows.pop(entity, None)
                self.durable_rows.pop(entity, None)
            else:
                self.rows[entity] = row
                self.durable_rows[entity] = row
            if notify:
                for callback in tuple(self.listeners.get(entity, ())):
                    callback(entity, self.read(entity), row['kind'])
        elif receipt['kind'] == 'configuration':
            self.context = deepcopy(receipt['payload'])

    def install_snapshot(self, snapshot):
        self.context = deepcopy(snapshot['configuration'])
        self.rows = {key:deepcopy(value['value']) for key,value in snapshot['observations'].items()
                     if value['value']['state'] is not None}
        self.revision = snapshot['through']
        self.changes.clear()
        self.durable_rows = self.rows
        self.row_receipts = {entity:snapshot['through'] for entity in snapshot['observations']}

    def apply_live(self, rows, *, through):
        # Replaceable values have no authority to advance the execution prefix.
        if self.rows is self.durable_rows:
            self.rows = dict(self.rows)
        for entity, row in rows.items():
            if self.row_receipts.get(entity, 0) > through:
                continue
            if row['state'] is None:
                self.rows.pop(entity, None)
            else:
                self.rows[entity] = deepcopy(row)

    def checkpoint(self, through, previous):
        """Freeze only changed sources at the execution prefix, never ahead of it."""
        if previous is None:
            if through != self.revision:
                raise ValueError('Initial source checkpoint must match the current mirror')
            return dict(receipt=through, context=deepcopy(self.context), rows=deepcopy(self.durable_rows))
        rows = dict(previous['rows'])
        context = previous['context']
        for ordinal, receipt in self.changes.items():
            if not previous['receipt'] < ordinal <= through:
                continue
            if receipt['kind'] == 'configuration':
                context = deepcopy(receipt['payload'])
            elif receipt['kind'] == 'observation':
                row = receipt['payload']
                if row['state'] is None:
                    rows.pop(row['entity_id'], None)
                else:
                    rows[row['entity_id']] = row
        if through == self.revision:
            # Also supports a freshly restored mirror without pending deltas.
            for entity in rows.keys()-self.durable_rows.keys():
                rows.pop(entity)
            for entity, row in self.durable_rows.items():
                if row != rows.get(entity):
                    rows[entity] = deepcopy(row)
            if self.context != context:
                context = deepcopy(self.context)
        return dict(receipt=through, context=context, rows=rows)

    def committed(self, through):
        self.changes = {ordinal: row for ordinal, row in self.changes.items() if ordinal > through}


class RemoteHistory:
    def __init__(self, gateway):
        self.gateway = gateway
        self.profiler = gateway.profiler

    async def source(self, operation, body):
        name = {'statistics':'ha_statistics','states':'ha_states','forecast':'ha_forecast'}.get(operation,'ha_metadata')
        with self.profiler.measure(name):
            return await self.gateway.call('source', {'operation':operation, 'body':body})

    async def statistics(self, start, end, entities, period, units, kinds):
        return await self.source('statistics', dict(start=start.isoformat(), end=end.isoformat(),
            entities=sorted(entities), period=period, units=units, kinds=sorted(kinds)))

    async def states(self, start, end, entities, *, with_attributes):
        rows = await self.source('states', dict(start=start.isoformat(), end=end.isoformat(),
            entities=entities, with_attributes=with_attributes))
        return {entity:[(timestamp(at), value, attributes) for at,value,attributes in values] for entity,values in rows.items()}

    async def hourly_forecast(self, entity):
        return await self.source('forecast', {'entity':entity})
