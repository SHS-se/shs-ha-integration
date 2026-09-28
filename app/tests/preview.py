"""Local browser test harness; fixtures never ship in the app image."""
import json
from pathlib import Path
import sys

from aiohttp import web
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "app"), str(ROOT / "custom_components/shs_energy")]
from shs_app.server import create_app
from shs_core.app_projection import schedule
from shs_core.configuration_fields import _configuration_sections, LABELS
from shs_core.configuration_schema import resolve_configuration

plan = json.loads((ROOT / "tests/fixtures/schema-9-mixed-mode-plan.json").read_text())["plan"]
entry = {"id": "test-home", "title": "Test home", "state": "loaded", "operation": {"state": "ready", "label": "Ready", "reason": "A validated plan is available"},
         "schedule": schedule(plan, {"state": "ready"}), "controllers": {"battery": {"state": "controlling", "reason": "Following the current energy plan"}},
         "attention": [], "measurements": {"house": {"value": 3790, "unit": "W", "observed_at": plan["issued_at"]}, "solar": {"value": 1250, "unit": "W", "observed_at": plan["issued_at"]}, "grid": {"value": 2540, "unit": "W", "observed_at": plan["issued_at"]}, "battery_soc": {"value": 47, "unit": "%", "observed_at": plan["issued_at"]}}}
history = [{"sampled_at": s["start"], "resources": {"cpu_percent": 2.5, "memory_usage": 48000000}, "telemetry": {"test-home": {"measured_w": s["load_w"] * .95, "planned_w": s["load_w"], "plan_id": plan["plan_id"]}}} for s in plan["plans"]["priority"]["slots"]]
class Editor:
    revision=1
    options={'planning_mode':'live','pv_forecast_latitude':59.0}
    async def action(self,action,body):
        if action=='save':
            if body['expected_revision']!=self.revision:raise ValueError('Configuration changed in another window')
            self.options.update(body['configuration']);self.revision+=1
            return dict(saved=True,refreshing=False,revision=self.revision)
        if action!='get':raise ValueError('Unsupported fixture action')
        return dict(revision=self.revision,state='applied',configuration=resolve_configuration(self.options,59,18),
            configured_keys=list(self.options),labels=LABELS,sections=_configuration_sections(),devices=[],entities=[],
            entry=dict(entry_id='test-home',title='Test home',state='loaded'),attention=[],readiness={},thermal={},
            portal=dict(status='synchronised'),operation=entry['operation'],diagnostics={},meter_inventory=[],
            replan_recommendations=[],measurement_issues=[],locale=dict(language='en',timezone='Europe/Stockholm'))
class Fixture:
    editor=Editor()
    async def project(self):pass
    @property
    def engine(self):
        return SimpleNamespace(closed=False,started=True,gateway=SimpleNamespace(connected=True),editor=self.editor,
            configuration=SimpleNamespace(status=lambda:dict(revision=self.editor.revision)),project=self.project)
    def payload(self):
        return {"recovery": None, "app_version": "0.1.0-beta.1", "required_companion": "0.9.0-beta.50", "protocol": 1, "connection": {"state": "connected", "message": "Connected"}, "snapshot": {"sampled_at": plan["issued_at"], "integration_version": "0.9.0-beta.50", "entries": [entry]}, "system": {"sampled_at": plan["issued_at"], "resources": {"cpu_percent": 2.5, "memory_usage": 48000000, "memory_limit": 2000000000, "blk_read": 14000, "blk_write": 23000, "network_rx": 150000, "network_tx": 44000}, "filesystem_free_bytes": 40000000000, "error": None, "database": {"file_bytes": 40960, "schema_version": 1, "page_size": 4096, "free_pages": 0, "journal_mode": "delete", "tables": [{"name": "resource_samples", "bytes": 8192, "pages": 2, "rows": 16}], "history": history, "query_plan": ["SEARCH resource_samples USING INDEX sqlite_autoindex_resource_samples_1"], "operations": [{"name": "record_resources", "count": 16, "errors": 0, "last_ms": .7, "max_recent_ms": 1.2, "queue_ms": .01}]}}, "companion": {"state": "not_requested", "message": "Companion installation is controlled in Home Assistant."}, "app_slug": "test_shs_energy", "sidebar_enabled": True, "control_owner": "SHS app"}

app = web.Application()
app.add_subapp("/ingress/test", create_app(Fixture(), ROOT / "web/dist", trusted_peer="127.0.0.1"))
web.run_app(app, host="127.0.0.1", port=8098, print=None)
