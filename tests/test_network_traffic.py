"""Exercise byte accounting and the real API method without an HA installation."""
import ast
import asyncio
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest

ROOT = Path(__file__).parents[1] / "custom_components/shs_energy"
sys.path.insert(0, str(ROOT))
from network_traffic import NetworkTraffic


class TrafficTests(unittest.TestCase):
    def test_query_redaction_unknown_sizes_and_snapshot_isolation(self):
        meter = NetworkTraffic()
        meter.record("GET", "integration-prices?token=secret", None, None, None, True, 12)
        meter.record("POST", "integration-status", {"name": "å"}, 200, 8, False, 20)
        report = meter.snapshot()
        self.assertNotIn("secret", json.dumps(report))
        self.assertEqual(report["total"]["requests"], 2)
        self.assertEqual(report["total"]["responses_unmeasured"], 1)
        self.assertEqual(report["total"]["response_body_bytes"], 8)
        report["endpoints"]["POST integration-status"]["requests"] = 90
        self.assertEqual(meter.snapshot()["total"]["requests"], 2)

    def test_bounded_cardinality(self):
        meter = NetworkTraffic()
        for i in range(100):
            meter.record("GET", "endpoint-" + "a" * i, None, 200, 1, False, 1)
        self.assertLessEqual(len(meter.snapshot()["endpoints"]), 65)
        self.assertEqual(meter.snapshot()["total"]["response_body_bytes"], 100)


class ApiTrafficTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        class ClientError(Exception):
            pass
        tree = ast.parse((ROOT / "api.py").read_text())
        # Use the entire real client with only its external imports replaced.
        tree.body = [node for node in tree.body if not (
            isinstance(node, ast.ImportFrom) and node.level > 0
            or isinstance(node, ast.Import) and any(a.name == "aiohttp" for a in node.names)
        )]
        ns = {"aiohttp": SimpleNamespace(ClientError=ClientError, ContentTypeError=ClientError,
                ClientTimeout=lambda **kwargs: None),
              "NetworkTraffic": NetworkTraffic, "API_VERSION": 1, "INTEGRATION_VERSION": "test",
              "MAX_REPLAN_ERROR_CHARS": 1000, "SUPPORTED_PLAN_SCHEMA_VERSIONS": {6}}
        exec(compile(tree, "api.py", "exec"), ns)
        self.api = ns

    async def call(self, status=200, payload=None, interrupted=False):
        if payload is None:
            payload = {"api_version": 1, "ok": True, "data": {"name": "å"}, "request_id": "test"}
        raw = json.dumps(payload, ensure_ascii=False).encode()

        class Response:
            headers = {}
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                pass
            async def read(self):
                if interrupted:
                    raise asyncio.TimeoutError()
                return raw
            async def json(self):
                return payload
        response = Response()
        response.status = status
        client = self.api["ShsApiClient"](SimpleNamespace(request=lambda *a, **kw: response), "https://example", "secret")
        if status >= 400 or interrupted or not payload.get("ok"):
            with self.assertRaises(self.api["ShsApiError"]):
                await client.status()
        else:
            self.assertEqual(await client.status(), payload["data"])
        return client.traffic.snapshot()["total"], len(raw)

    async def test_success_measures_actual_decoded_utf8_body(self):
        total, size = await self.call()
        self.assertEqual(total["response_body_bytes"], size)
        self.assertEqual(total["failed_requests"], 0)

    async def test_http_and_contract_failures_still_count_the_body(self):
        for status, payload in ((503, {"error": "unavailable"}), (200, {"message": "bad envelope"})):
            total, size = await self.call(status, payload)
            self.assertEqual(total["response_body_bytes"], size)
            self.assertEqual(total["failed_requests"], 1)
            self.assertEqual(total["http_errors"], int(status >= 400))

    async def test_body_timeout_is_unknown_not_measured_as_empty(self):
        total, _ = await self.call(interrupted=True)
        self.assertEqual(total["responses_unmeasured"], 1)
        self.assertEqual(total["responses_measured"], 0)
        self.assertEqual(total["failed_requests"], 1)


if __name__ == "__main__":
    unittest.main()
