"""Exercise byte accounting and the real API method without an HA installation."""
import ast
import asyncio
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).parents[1] / "custom_components/shs_energy"
sys.path.append(str(ROOT))
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
                ClientTimeout=lambda **kwargs: SimpleNamespace(**kwargs)),
              "NetworkTraffic": NetworkTraffic, "API_VERSION": 1, "INTEGRATION_VERSION": "test",
              "MAX_REPLAN_ERROR_CHARS": 1000, "SUPPORTED_PLAN_SCHEMA_VERSIONS": {6}}
        exec(compile(tree, "api.py", "exec"), ns)
        self.api = ns

    async def call(self, status=200, payload=None, interrupted=False, path="integration-status"):
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
        def request(*args, **kwargs):
            self.last_timeout = kwargs["timeout"].total
            return response
        client = self.api["ShsApiClient"](SimpleNamespace(request=request), "https://example", "secret")
        if status >= 400 or interrupted or not payload.get("ok"):
            with self.assertRaises(self.api["ShsApiError"]):
                await client._request("POST", path)
        else:
            self.assertEqual(await client._request("POST", path), payload["data"])
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

    async def test_planning_has_time_for_server_deadline_without_extending_other_requests(self):
        await self.call()
        self.assertEqual(self.last_timeout, 30)
        await self.call(path="energy-optimisation-ingest")
        self.assertEqual(self.last_timeout, 150)

    async def test_pending_planning_continues_identical_payload_until_ready(self):
        client = self.api["ShsApiClient"](None, "https://example", "secret")
        ready = {"plan": {"plan_id": "finished"}, "actual_slots_accepted": 1}
        client._request = AsyncMock(side_effect=[
            {"pending": True, "retry_after_ms": 1000},
            {"pending": True, "retry_after_ms": 2345}, ready,
        ])
        with patch.object(self.api["asyncio"], "sleep", new_callable=AsyncMock) as sleep:
            result = await client.push_optimisation([{"start_ts": "quarter"}],
                {"snapshot_id": "frozen"}, replan_request_id="manual")
        self.assertEqual(result, ready)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [1, 2.345])
        calls = client._request.call_args_list
        self.assertEqual(len(calls), 3)
        self.assertEqual(calls[0], calls[1])
        self.assertEqual(calls[1], calls[2])
        self.assertEqual(calls[0].kwargs["json_body"]["replan_request_id"], "manual")

    async def test_pending_planning_does_not_retry_failed_or_invalid_responses(self):
        for invalid in [-1, float("nan"), True, "1", None]:
            client = self.api["ShsApiClient"](None, "https://example", "secret")
            client._request = AsyncMock(return_value={"pending": True, "retry_after_ms": invalid})
            with self.assertRaisesRegex(self.api["ShsApiError"], "continuation delay"):
                await client.push_optimisation([], {"snapshot_id": "frozen"})
            self.assertEqual(client._request.await_count, 1)
        client._request = AsyncMock(side_effect=self.api["ShsApiError"]("worker failed"))
        with self.assertRaisesRegex(self.api["ShsApiError"], "worker failed"):
            await client.push_optimisation([], {"snapshot_id": "frozen"})
        self.assertEqual(client._request.await_count, 1)


if __name__ == "__main__":
    unittest.main()
