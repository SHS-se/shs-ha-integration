import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from aiohttp.test_utils import AioHTTPTestCase
from shs_app.companion import hashes, install
from shs_app.server import create_app, Dashboard
from shs_app.storage import Diagnostics


class CompanionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bundle, self.ha, self.data = [self.root / name for name in ("bundle", "ha", "data")]
        for path in (self.bundle / "shs_energy", self.ha / "custom_components/shs_energy", self.data):
            path.mkdir(parents=True)
        self.target = self.ha / "custom_components/shs_energy"
        (self.target / "old.py").write_text("previous")
        (self.target / "manifest.json").write_text('{"domain":"shs_energy","version":"v1"}')
        (self.bundle / "shs_energy/new.py").write_text("new")
        (self.bundle / "shs_energy/manifest.json").write_text('{"domain":"shs_energy","version":"v2"}')
        (self.bundle / "bundle.json").write_text(json.dumps({"integration_version": "v2", "files": hashes(self.bundle / "shs_energy"), "replaceable": [hashes(self.target)]}))
        (self.ha / ".storage").mkdir()
        (self.ha / ".storage/history").write_text("must survive")

    def test_explicit_install_preserves_history_and_is_idempotent(self):
        self.assertEqual(install(self.bundle, self.ha, self.data)["state"], "installed")
        self.assertEqual((self.ha / ".storage/history").read_text(), "must survive")
        self.assertEqual((self.ha / ".shs-companion-install/backup/old.py").read_text(), "previous")
        self.assertEqual(self.discovered_versions(), ["v2"])
        self.assertEqual(install(self.bundle, self.ha, self.data)["state"], "installed")

    def discovered_versions(self):
        # Match HA's directory discovery: hidden directories are not excluded.
        return [json.loads((directory / "manifest.json").read_text())["version"]
                for directory in (self.ha / "custom_components").iterdir()
                if directory.is_dir() and (directory / "manifest.json").exists()]

    def test_staging_never_adds_another_discoverable_integration(self):
        with patch.object(Path, "rename", side_effect=OSError("interrupted before swap")):
            with self.assertRaises(OSError):
                install(self.bundle, self.ha, self.data)
        self.assertEqual(self.discovered_versions(), ["v1"])
        self.assertTrue((self.ha / ".shs-companion-install/stage/manifest.json").exists())
        install(self.bundle, self.ha, self.data)
        self.assertEqual(self.discovered_versions(), ["v2"])

    def test_refuse_local_modification_and_symlinks(self):
        (self.target / "old.py").write_text("local edits")
        with self.assertRaisesRegex(ValueError, "differs"):
            install(self.bundle, self.ha, self.data)
        (self.target / "link").symlink_to(self.data)
        with self.assertRaisesRegex(ValueError, "symbolic"):
            install(self.bundle, self.ha, self.data)

    def test_resume_after_old_directory_rename(self):
        original = Path.rename
        def interrupted(path, target):
            if path.name == "stage":
                raise OSError("power interrupted")
            return original(path, target)
        with patch.object(Path, "rename", interrupted):
            with self.assertRaises(OSError):
                install(self.bundle, self.ha, self.data)
        self.assertFalse(self.target.exists())
        self.assertEqual(install(self.bundle, self.ha, self.data)["state"], "installed")

    def test_missing_current_target_without_matching_journal_is_rejected(self):
        workspace = self.ha / ".shs-companion-install"
        workspace.mkdir()
        self.target.rename(workspace / "backup")
        with self.assertRaises(ValueError):
            install(self.bundle, self.ha, self.data)


class StorageTests(unittest.TestCase):
    def test_census_retention_and_indexed_query(self):
        with tempfile.TemporaryDirectory() as directory:
            from datetime import datetime, timezone
            db = Diagnostics(Path(directory))
            db.record("2000-01-01T00:00:00+00:00", {"resources": None})
            db.record(datetime.now(timezone.utc).isoformat(), {"resources": {"cpu_percent": 2}})
            state = db.snapshot()
            self.assertEqual(state["tables"][0]["rows"], 1)
            self.assertEqual(len(state["history"]), 1)
            self.assertGreater(state["tables"][0]["bytes"], 0)
            self.assertTrue(any("INDEX" in line for line in state["query_plan"]))
            self.assertEqual(next(op for op in state["operations"] if op["name"] == "record_resources")["count"], 2)


class IngressTests(AioHTTPTestCase):
    async def get_application(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        static = Path(self.directory.name)
        (static / "index.html").write_text("SHS")
        class Snapshot:
            def payload(self): return {"control_owner": "HA"}
        return create_app(Snapshot(), static)

    async def test_non_ingress_socket_is_rejected_even_with_spoofed_headers(self):
        async with self.client.get("/api/state", headers={"X-Forwarded-For": "172.30.32.2", "X-Remote-User-Id": "admin"}) as response:
            self.assertEqual(response.status, 403)


class DashboardTests(unittest.IsolatedAsyncioTestCase):
    async def test_dashboard_starts_without_claiming_control_or_a_loaded_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'bundle.json').write_text(json.dumps({'protocol':2,'integration_version':'paired'}))
            (root/'app.json').write_text('{"version":"app"}')
            dashboard = Dashboard(root,root)
            self.assertEqual(dashboard.payload()['control_owner'],'Migration pending')
            self.assertIsNone(dashboard.payload()['snapshot'])
            self.assertEqual(dashboard.payload()['required_companion'],'paired')

            from types import SimpleNamespace
            self.assertIsNone(dashboard.payload()['recovery'])
            engine=dashboard.engine=SimpleNamespace(started=False,battery=None)
            self.assertEqual(dashboard.payload()['recovery'],{'processed_receipt':None})
            engine.battery=SimpleNamespace(_processing={'receipt':42,'complete':False})
            self.assertEqual(dashboard.payload()['recovery'],{'processed_receipt':41})
            engine.battery._processing['complete']=True
            self.assertEqual(dashboard.payload()['recovery'],{'processed_receipt':42})
            engine.started=True
            self.assertIsNone(dashboard.payload()['recovery'])
