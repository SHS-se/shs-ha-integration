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
from shs_core.network_traffic import NetworkTraffic


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
        from shs_core import api
        self.api = vars(api)

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

    async def test_acceptance_returns_receipt_without_waiting_or_resending_capture(self):
        client = self.api["ShsApiClient"](None, "https://example", "secret")
        receipt = {"job_id": "job", "state": "pending", "pending": True, "retry_after_ms": 1000, "actual_slots_accepted": 1}
        client._request = AsyncMock(return_value=receipt)
        self.assertEqual(await client.push_optimisation([{"start_ts": "quarter"}],
            {"snapshot_id": "frozen"}, replan_request_id="manual"), receipt)
        client._request.assert_awaited_once()
        body = client._request.call_args.kwargs["json_body"]
        self.assertEqual(body["planning_exchange_version"], 2)
        self.assertEqual(body["replan_request_id"], "manual")
        self.assertEqual(body["snapshot"], {"snapshot_id": "frozen"})

    async def test_status_sends_only_receipt_identity_and_protocol(self):
        client = self.api["ShsApiClient"](None, "https://example", "secret")
        client._request = AsyncMock(return_value={"job_id": "job", "state": "superseded", "pending": False})
        await client.planning_status("job")
        self.assertEqual(client._request.call_args.kwargs["json_body"], {
            "api_version": 1, "planning_exchange_version": 2, "job_id": "job",
        })
        client._request.return_value = {"job_id": "different", "state": "superseded", "pending": False}
        with self.assertRaisesRegex(self.api["ShsApiError"], "another job"):
            await client.planning_status("job")

    async def test_submission_lookup_sends_only_captured_snapshot_identity(self):
        client = self.api["ShsApiClient"](None, "https://example", "secret")
        client._request = AsyncMock(return_value={"job_id": "job", "snapshot_id": "snapshot", "state": "pending", "pending": True, "retry_after_ms": 1000})
        await client.planning_submission_status("snapshot")
        self.assertEqual(client._request.call_args.kwargs["json_body"], {
            "api_version": 1, "planning_exchange_version": 2, "snapshot_id": "snapshot",
        })
        client._request.return_value["snapshot_id"] = "different"
        with self.assertRaisesRegex(self.api["ShsApiError"], "another job"):
            await client.planning_submission_status("snapshot")

    async def test_invalid_receipts_and_errors_are_not_retried(self):
        for invalid in [-1, 0, float("nan"), True, "1", None]:
            client = self.api["ShsApiClient"](None, "https://example", "secret")
            client._request = AsyncMock(return_value={"job_id": "job", "state": "pending", "pending": True, "retry_after_ms": invalid})
            with self.assertRaisesRegex(self.api["ShsApiError"], "job receipt"):
                await client.push_optimisation([], {"snapshot_id": "frozen"})
            self.assertEqual(client._request.await_count, 1)
        client._request = AsyncMock(side_effect=self.api["ShsApiError"]("worker failed"))
        with self.assertRaisesRegex(self.api["ShsApiError"], "worker failed"):
            await client.push_optimisation([], {"snapshot_id": "frozen"})
        self.assertEqual(client._request.await_count, 1)

    async def test_synchronous_plan_response_without_durable_receipt_is_refused(self):
        client = self.api["ShsApiClient"](None, "https://example", "secret")
        client._request = AsyncMock(return_value={"plan": {"plan_id": "legacy"}})
        with self.assertRaisesRegex(self.api["ShsApiError"], "job receipt"):
            await client.push_optimisation([], {"snapshot_id": "frozen"})

    async def test_published_receipt_binds_plan_identity(self):
        client = self.api["ShsApiClient"](None, "https://example", "secret")
        receipt = {"job_id": "job", "state": "published", "pending": False,
                   "plan_id": "plan", "snapshot_id": "snapshot", "plan": {"plan_id": "plan", "snapshot_id": "snapshot"}}
        client._request = AsyncMock(return_value=receipt)
        self.assertEqual(await client.planning_status("job"), receipt)
        receipt["plan_id"] = "other"
        with self.assertRaisesRegex(self.api["ShsApiError"], "job receipt"):
            await client.planning_status("job")


if __name__ == "__main__":
    unittest.main()
