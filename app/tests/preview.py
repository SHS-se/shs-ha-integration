"""Local browser test harness; fixtures never ship in the app image."""
import argparse
from copy import deepcopy
import json
from pathlib import Path
import sys

from aiohttp import web
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "app"), str(ROOT / "custom_components/shs_energy")]
from shs_app.server import create_app
from shs_app.profiling import AppProfiler
from shs_core.app_projection import schedule
from shs_core.configuration_fields import _configuration_sections, device_field_catalog, LABELS
from shs_core.const import DEVICE_CATEGORIES
from shs_core.device_controls import CONTROL_TYPES
from shs_core.device_setup import LOAD_TYPES
from shs_core.configuration_schema import resolve_configuration

plan = json.loads((ROOT / "tests/fixtures/schema-9-mixed-mode-plan.json").read_text())["plan"]
entry = {"id": "test-home", "title": "Test home", "state": "loaded", "operation": {"state": "ready", "label": "Ready", "reason": "A validated plan is available"},
         "schedule": schedule(plan, {"state": "ready"}), "controllers": {"battery": {"state": "controlling", "reason": "Following the current energy plan"}},
         "attention": [], "measurements": {"house": {"value": 3790, "unit": "W", "observed_at": plan["issued_at"]}, "solar": {"value": 1250, "unit": "W", "observed_at": plan["issued_at"]}, "grid": {"value": 2540, "unit": "W", "observed_at": plan["issued_at"]}, "battery_soc": {"value": 47, "unit": "%", "observed_at": plan["issued_at"]}}}
parser = argparse.ArgumentParser(description="Preview the SHS app with sample data")
parser.add_argument("--devices", action="store_true", help="Show the sample device workspace")
parser.add_argument("--port", type=int, default=8098)
preview = parser.parse_args()
app_version = json.loads((ROOT / "web/package.json").read_text())["version"] if preview.devices else "0.1.0-beta.1"

HEATER = "sensor.guest_heater_energy"
ROOM = "guest_room"
SAMPLE_ROOM_PROBE = "sensor.guest_room_temperature"
SAMPLE_POWER = "sensor.guest_heater_power"
SAMPLE_SWITCH = "switch.guest_heater"

history = [{"sampled_at": s["start"], "resources": {"cpu_percent": 2.5, "memory_usage": 48000000}, "telemetry": {"test-home": {"measured_w": s["load_w"] * .95, "planned_w": s["load_w"], "plan_id": plan["plan_id"]}}} for s in plan["plans"]["priority"]["slots"]]
class Editor:
    revision=1
    options={'planning_mode':'live','outdoor_temperature_entity':'sensor.outdoor'}
    if preview.devices:
        options['device_control_mappings'] = {HEATER: {
            "room_area_id": ROOM, "control_type": "setpoint",
            "actuator_entity_ids": [SAMPLE_SWITCH], "power": SAMPLE_POWER,
        }}

    def preview_devices(self):
        if not preview.devices:
            return []
        mapping = self.options['device_control_mappings'][HEATER]
        catalog = device_field_catalog()
        method = self.options.get("sample_control_type", "setpoint")
        category = self.options.get("sample_category", "heating")
        fields = catalog['rows'][f"{category}:{method}"]['fields']
        has_probe = bool(mapping.get("temperature_entity_id"))
        return [
            {"key": HEATER, "name": "Guest bedroom heater", "statistic_id": HEATER,
             "category": category, "control_type": method,
             "load_type": self.options.get("sample_load_type", "fixed_full_load"),
             "room_name": "Guest Room", "included": True, "planned": True,
             "mapping_status": "ready" if has_probe else "invalid",
             "mapping_error": None if has_probe else "Choose a room temperature probe for Guest Room.",
             "mapping": mapping, "fields": [catalog['fields'][index] for index in fields],
             "meter_room": {"meters": [HEATER, SAMPLE_POWER], "area_id": ROOM, "editable": False},
             "readings": [{"name": "Power", "value": "120 W"}]},
            {"key": "sensor.laundry_fan_energy", "name": "Laundry fan", "category": "other",
             "room_name": "Laundry", "included": True, "planned": False,
             "mapping_status": "not_configured", "fields": [],
             "meter_room": {"meters": ["sensor.laundry_fan_energy"], "area_id": "laundry", "editable": False}},
        ]

    def preview_entities(self):
        if not preview.devices:
            return []
        return [
            {"entity_id": SAMPLE_ROOM_PROBE, "name": "Guest Room temperature", "unit": "°C", "domain": "sensor"},
            {"entity_id": SAMPLE_POWER, "name": "Guest heater power", "unit": "W", "domain": "sensor"},
            {"entity_id": SAMPLE_SWITCH, "name": "Guest heater relay", "domain": "switch"},
        ]
    async def action(self,action,body):
        if preview.devices and action == 'save_device' and body.get('device_key') == HEATER:
            if body['expected_revision'] != self.revision:
                raise ValueError('Configuration changed in another window')
            self.options.setdefault('device_control_mappings', {})[HEATER] = deepcopy(body['mapping'])
            self.options.update({f"sample_{key}": value for key, value in body['settings'].items()})
            self.revision += 1
            return await self.action('get', {})

        if action=='save':
            if body['expected_revision']!=self.revision:raise ValueError('Configuration changed in another window')
            self.options.update(body['configuration']);self.revision+=1
            return dict(saved=True,refreshing=False,revision=self.revision)
        if action!='get':raise ValueError('Unsupported fixture action')
        devices = self.preview_devices()
        attention = []
        if devices and devices[0]["mapping_status"] == "invalid":
            detail = devices[0]["mapping_error"]
            attention.append({"key": "guest-room-probe", "severity": "warning", "title": detail,
                "detail": detail, "fix": {"kind": "fields", "fields": [
                    {"key": "temperature_entity_id", "scope": "mapping", "device_key": HEATER, "message": detail}
                ]}})
        return dict(revision=self.revision,state='applied',configuration=resolve_configuration(self.options,59,18),
            configured_keys=list(self.options),labels=LABELS,device_categories=DEVICE_CATEGORIES,
            device_control_types=CONTROL_TYPES,device_load_types=LOAD_TYPES,device_setups_ready=True,
            device_field_catalog=device_field_catalog(),
            sections=_configuration_sections(),devices=devices,entities=self.preview_entities(),
            rooms={ROOM: "Guest Room", "laundry": "Laundry"} if preview.devices else {},

            entry=dict(entry_id='test-home',title='Test home',state='loaded'),attention=attention,readiness={},thermal={},
            portal=dict(status='synchronised'),operation=entry['operation'],diagnostics={},meter_inventory=[],
            replan_recommendations=[],measurement_issues=[],locale=dict(language='en',timezone='Europe/Stockholm'),
            backends=dict(revision=1,selected='test',environments=[
                dict(environment=environment,paired=environment=='test',selected=environment=='test',
                    subscription_active=environment=='test',last_delivery=None,error=None,
                    website_url='https://'+('test.' if environment=='test' else '')+'smarthomesolutions.se/portal/account')
                for environment in ('production','test')]))
class Fixture:
    profiler=AppProfiler()
    editor=Editor()
    async def project(self):pass
    async def gateway_call(self, operation, body):
        if operation != 'heating' or body['edit'] is not None:
            raise ValueError('Unsupported preview request')
        return dict(zones={}, loads={}, order=[])
    @property
    def engine(self):
        return SimpleNamespace(closed=False,started=True,gateway=SimpleNamespace(connected=True,call=self.gateway_call),editor=self.editor,
            configuration=SimpleNamespace(status=lambda:dict(revision=self.editor.revision)),project=self.project)
    def payload(self):
        return {"cloud_connection":{"state":"connected","retry_at":None,"persistent":False}, "attention": [], "recovery": None, "app_version": app_version, "required_companion": "0.9.0-beta.50", "protocol": 1, "connection": {"state": "connected", "message": "Connected"}, "snapshot": {"sampled_at": plan["issued_at"], "integration_version": "0.9.0-beta.50", "entries": [entry]}, "system": {"sampled_at": plan["issued_at"], "resources": {"cpu_percent": 2.5, "memory_usage": 48000000, "memory_limit": 2000000000, "blk_read": 14000, "blk_write": 23000, "network_rx": 150000, "network_tx": 44000}, "filesystem_free_bytes": 40000000000, "error": None, "database": {"file_bytes": 40960, "schema_version": 1, "page_size": 4096, "free_pages": 0, "journal_mode": "delete", "tables": [{"name": "resource_samples", "bytes": 8192, "pages": 2, "rows": 16}], "history": history, "query_plan": ["SEARCH resource_samples USING INDEX sqlite_autoindex_resource_samples_1"], "operations": [{"name": "record_resources", "count": 16, "errors": 0, "last_ms": .7, "max_recent_ms": 1.2, "queue_ms": .01}]}}, "companion": {"state": "not_requested", "message": "Companion installation is controlled in Home Assistant."}, "app_slug": "test_shs_energy", "sidebar_enabled": True, "control_owner": "SHS app"}

app = web.Application()
app.add_subapp("/ingress/test", create_app(Fixture(), ROOT / "web/dist", trusted_peer="127.0.0.1"))
web.run_app(app, host="127.0.0.1", port=preview.port, print=None)
