"""Explicit, inert household ports for domain scenarios."""
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from unittest.mock import AsyncMock, Mock
from zoneinfo import ZoneInfo

sys.path.append(str(Path(__file__).parents[1]/'custom_components/shs_energy'))
from shs_core.durable_record import DurableRecord
from shs_core.household import Household
from shs_core.household_ports import HomeFacts, HouseholdPorts, ConfigurationChanged
from shs_core.api_contract import API_VERSION, SUPPORTED_PLAN_SCHEMA_VERSIONS, SNAPSHOT_SCHEMA_VERSION


class Store:
    def __init__(self, value=None):
        self.saved = deepcopy(value or {})

    async def async_load(self):
        return deepcopy(self.saved)

    async def async_save(self, value):
        self.saved = deepcopy(value)


def cloud_status(**values):
    return {'api_version': API_VERSION,
            'supported_snapshot_schema_versions': [SNAPSHOT_SCHEMA_VERSION],
            'supported_plan_schema_versions': sorted(SUPPORTED_PLAN_SCHEMA_VERSIONS),
            'minimum_snapshot_schema_version': SNAPSHOT_SCHEMA_VERSION,
            'minimum_plan_schema_version': min(SUPPORTED_PLAN_SCHEMA_VERSIONS),
            'latest_plan_request_id': None, **values}


class Rig:
    def __init__(self, *, now=None, options=None, stored=None):
        self.now = now or datetime(2026, 9, 27, 10, tzinfo=timezone.utc)
        self.options = deepcopy(options or {'planning_mode': 'live'})
        self.home = HomeFacts(59.3, 18.0, 'en', ZoneInfo('Europe/Stockholm'))
        self.states = {}
        self.records = Store(stored)
        self.battery_store = Store()
        self.client = Mock(status=AsyncMock(return_value=cloud_status(subscription_active=False)))
        self.history = Mock(statistics=AsyncMock(return_value={}), states=AsyncMock(return_value={}), hourly_forecast=AsyncMock(return_value=[]))
        self.repairs = []
        self.published = []
        self.spawned = []
        async def admit(expected, updated):
            if self.options != expected:
                raise ConfigurationChanged('settings changed')
            self.options = deepcopy(updated)
        def spawn(coroutine, name):
            self.spawned.append(name)
            coroutine.close()  # Tests explicitly invoke work; no detached tasks.
        ports = HouseholdPorts(
            home=lambda: self.home, options=lambda: deepcopy(self.options), admit=admit,
            read_state=self.states.get, entity_ids=lambda: set(self.states),
            entity_names=lambda: {}, area_names=lambda: {}, entity_areas=lambda: {},
            inventory=AsyncMock(return_value=[]), battery_report=lambda entity: None,
            history=self.history, utcnow=lambda: self.now, recovering=lambda: False,
            repair=lambda *args: self.repairs.append(args), publish=lambda: self.published.append(True), spawn=spawn,
        )
        self.household = Household(ports, self.client,
            store=DurableRecord(self.records, json.dumps, json.loads), battery_inputs_store=self.battery_store, control_authority=True)
