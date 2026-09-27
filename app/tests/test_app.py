import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from aiohttp.test_utils import AioHTTPTestCase
from shs_app.companion import hashes, install
from shs_app.server import create_app, Observer
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
        (self.bundle / "shs_energy/new.py").write_text("new")
        (self.bundle / "bundle.json").write_text(json.dumps({"integration_version": "v2", "files": hashes(self.bundle / "shs_energy"), "replaceable": [hashes(self.target)]}))
        (self.ha / ".storage").mkdir()
        (self.ha / ".storage/history").write_text("must survive")

    def test_explicit_install_preserves_history_and_is_idempotent(self):
        self.assertEqual(install(self.bundle, self.ha, self.data)["state"], "installed")
        self.assertEqual((self.ha / ".storage/history").read_text(), "must survive")
        self.assertEqual((self.ha / "custom_components/.shs_energy-before-app/old.py").read_text(), "previous")
        self.assertEqual(install(self.bundle, self.ha, self.data)["state"], "installed")

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
            if path.name == ".shs_energy-stage":
                raise OSError("power interrupted")
            return original(path, target)
        with patch.object(Path, "rename", interrupted):
            with self.assertRaises(OSError):
                install(self.bundle, self.ha, self.data)
        self.assertFalse(self.target.exists())
        self.assertEqual(install(self.bundle, self.ha, self.data)["state"], "installed")

    def test_missing_current_target_without_matching_journal_is_rejected(self):
        self.target.rename(self.target.with_name(".shs_energy-before-app"))
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


class ObserverTests(unittest.IsolatedAsyncioTestCase):
    async def test_version_mismatch_clears_snapshot_and_failure_marks_stale(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "bundle.json").write_text(json.dumps({"protocol": 1, "integration_version": "v2"}))
            (root / "app.json").write_text('{"version":"v1"}')
            observer = Observer(root, root)
            async def good(path): return {"protocol": 1, "integration_version": "v2", "entries": []}
            observer.get = good
            await observer.observe()
            self.assertEqual(observer.connection["state"], "connected")
            async def broken(path): raise TimeoutError("timed out")
            observer.get = broken
            await observer.observe()
            self.assertEqual(observer.connection["state"], "disconnected")
            self.assertIsNotNone(observer.snapshot)
            async def mismatch(path): return {"protocol": 1, "integration_version": "v3"}
            observer.get = mismatch
            await observer.observe()
            self.assertEqual(observer.connection["state"], "incompatible")
            self.assertIsNone(observer.snapshot)
