"""HTTP client for the Smart Home Solutions edge functions."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import ssl
from time import monotonic
from typing import Any
from urllib.parse import urlencode
from uuid import uuid4

import aiohttp
import certifi

from .network_traffic import NetworkTraffic
from .command_journal import encoded

from .execution_replan import ExecutionReplan
from .api_contract import (
    API_VERSION,
    INTEGRATION_VERSION,
    MAX_REPLAN_ERROR_CHARS,
    validate_server_contract,
    SUPPORTED_PLAN_SCHEMA_VERSIONS,
)

_LOGGER = logging.getLogger(__name__)
# Explicitly use the patched Mozilla bundle, including when Home Assistant's
# shared session was created with an older host trust store.
_TLS_CONTEXT = ssl.create_default_context(cafile=certifi.where())

PLANNING_EXCHANGE_VERSION = 3

REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=30)
# Preparation may include history reads. Solving runs as a durable cloud job
# and returns a receipt rather than keeping this connection open.
PLANNING_REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=150)
NOTIFICATION_REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=120)


class ShsApiError(Exception):
    """Base error talking to SHS."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "api_error",
        request_id: str | None = None,
        path: str | None = None,
        retryable: bool = False,
        recapture: bool = False,
    ) -> None:
        self.message = message
        self.code = code
        self.request_id = request_id
        self.path = path
        self.retryable = retryable
        self.recapture = recapture
        suffix = f" [request_id={request_id}]" if request_id else ""
        super().__init__(f"{message}{suffix}")


class ShsConnectionError(ShsApiError):
    """A transport or remote service availability failure that can reconnect."""


class ShsAuthError(ShsApiError):
    """Device token rejected (revoked/invalid)."""


class ShsSubscriptionInactiveError(ShsApiError):
    """Subscription lapsed — server refused the request with 402."""


class ShsPairingError(ShsApiError):
    """Pairing code was rejected."""


class ShsApiClient:
    """Minimal async client for pairing, status, tariff, and ingest endpoints."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        base_url: str,
        device_token: str | None = None,
        app_version: str | None = None,
    ) -> None:
        self._session = session
        self._base_url = base_url.rstrip("/")
        self._device_token = device_token
        # The SHS app that runs this core, when there is one. The website shows
        # it beside the plan HA reports, next to the core's own version.
        self._app_version = app_version
        self.traffic = NetworkTraffic()
        self.connection_progress = lambda state: None

    async def _request(
        self,
        method: str,
        path: str,
        json_body: dict[str, Any] | None = None,
        authenticated: bool = True,
        timeout=None,
        notification=False,
    ) -> dict[str, Any]:
        request_id = str(uuid4())
        headers = {
            "X-Request-ID": request_id,
            "X-SHS-API-Version": str(API_VERSION),
            "X-SHS-Integration-Version": INTEGRATION_VERSION,
        }
        if self._app_version:
            headers["X-SHS-App-Version"] = self._app_version
        if authenticated:
            if not self._device_token:
                raise ShsAuthError("no device token configured")
            headers["Authorization"] = f"Bearer {self._device_token}"

        started = monotonic()
        status: int | None = None
        response_bytes: int | None = None
        failed = True
        try:
            async with self._session.request(
                method,
                f"{self._base_url}/{path}",
                json=json_body,
                headers=headers,
                ssl=_TLS_CONTEXT,
                timeout=timeout or (PLANNING_REQUEST_TIMEOUT if path == "energy-optimisation-ingest" else REQUEST_TIMEOUT),
                trace_request_ctx={"connected": self.connection_progress if notification else None},
            ) as resp:
                status = resp.status
                # aiohttp caches this decompressed body; json() reuses it.
                response_bytes = len(await resp.read())
                try:
                    decoded: Any = await resp.json()
                    payload = decoded if isinstance(decoded, dict) else {}
                except (aiohttp.ContentTypeError, ValueError):
                    payload = {}

                response_request_id = (
                    payload.get("request_id")
                    if isinstance(payload.get("request_id"), str)
                    else resp.headers.get("X-Request-ID") or request_id
                )
                error_info = payload.get("error_info")
                structured_error = (
                    error_info if isinstance(error_info, dict) else None
                )

                if resp.status == 401:
                    raise ShsAuthError(
                        str(
                            structured_error.get("message")
                            if structured_error
                            else "device token rejected"
                        ),
                        code=str(
                            structured_error.get("code")
                            if structured_error
                            else "unauthorized"
                        ),
                        request_id=response_request_id,
                    )
                if resp.status == 402:
                    raise ShsSubscriptionInactiveError(
                        "subscription inactive",
                        code="subscription_inactive",
                        request_id=response_request_id,
                    )
                if resp.status >= 400:
                    if structured_error:
                        details = structured_error.get("details")
                        message = str(
                            structured_error.get("message")
                            or structured_error.get("code")
                            or "request failed"
                        )
                        if details not in (None, ""):
                            message = f"{message} ({details})"
                        error_type = ShsConnectionError if structured_error.get("retryable") is True else ShsApiError
                        raise error_type(
                            message,
                            code=str(structured_error.get("code") or "api_error"),
                            request_id=response_request_id,
                            path=(
                                str(structured_error["path"])
                                if structured_error.get("path") is not None
                                else None
                            ),
                            retryable=bool(structured_error.get("retryable")),
                            recapture=(structured_error.get("code") == "accepted_reference_changed"
                                       and isinstance(details, dict) and details.get("recapture") is True),
                        )
                    error_type = ShsConnectionError if resp.status >= 500 or resp.status == 429 else ShsApiError
                    raise error_type(
                        f"{path} returned HTTP {resp.status} without an SHS API envelope",
                        code="upstream_transport_failure",
                        request_id=response_request_id,
                        retryable=resp.status >= 500 or resp.status == 429,
                    )
                if (
                    payload.get("api_version") != API_VERSION
                    or payload.get("ok") is not True
                    or not isinstance(payload.get("data"), dict)
                    or not isinstance(payload.get("request_id"), str)
                ):
                    raise ShsApiError(
                        f"{path} returned an invalid SHS API envelope",
                        code="invalid_response_envelope",
                        request_id=response_request_id,
                    )
                failed = False
                return payload["data"]
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise ShsConnectionError(
                f"connection error calling {path}: {err}",
                code="upstream_transport_failure",
                request_id=request_id,
                retryable=True,
            ) from err
        finally:
            self.traffic.record(method, path, json_body, status, response_bytes,
                                failed, (monotonic() - started) * 1000)

    @property
    def base_url(self):
        return self._base_url

    async def pair(
        self, pairing_code: str, device_name: str
    ) -> dict[str, Any]:
        """Exchange a pairing code for a device token (unauthenticated)."""
        try:
            return await self._request(
                "POST",
                "pair-device",
                json_body={"code": pairing_code, "device_name": device_name},
                authenticated=False,
            )
        except ShsAuthError as err:
            # 401 here means the code was wrong/expired, not a token problem.
            raise ShsPairingError(str(err)) from err

    async def wait_for_replan(self, after: str | None) -> str | None:
        """Long wait with device authentication; the server wakes on a request."""
        query = {"wait_for_replan": "true"}
        if after is not None:
            query["after"] = after
        status = await self._request("GET", "integration-status?" + urlencode(query),
                                     timeout=NOTIFICATION_REQUEST_TIMEOUT, notification=True)
        validate_server_contract(status)
        return status.get("pending_replan_request_id")

    async def status(self) -> dict[str, Any]:
        """Fetch subscription status for the paired customer."""
        return await self._request("GET", "integration-status")

    async def report_runtime(self, runtime: dict[str, Any], *, input_accuracy: dict[str, Any] | None = None) -> dict[str, Any]:
        """Report current local readiness, independently of plan acceptance.

        Input accuracy travels beside the runtime: a website that predates it
        ignores the extra field instead of rejecting the runtime.
        """
        body = {"api_version": API_VERSION, "runtime": runtime}
        if input_accuracy is not None:
            body["input_accuracy"] = input_accuracy
        result = await self._request("POST", "integration-status", json_body=body)
        if result.get("runtime_received") is not True:
            raise ShsApiError("Website did not confirm storing the current HA status")
        return result

    async def request_replan(self, *, reason: str = "integration_replan") -> str:
        """Queue the same household request used by the website button."""
        result = await self._request(
            "POST", "integration-status",
            json_body={"api_version": API_VERSION, "request_replan": True,
                       "reason": reason},
        )
        request_id = result.get("replan_request_id")
        if not isinstance(request_id, str) or not request_id:
            raise ShsApiError("Website did not confirm the replan request")
        return request_id

    async def report_replan_failure(
        self, replan_request_id: str, error: str
    ) -> dict[str, Any]:
        """Say why a replan the household asked for could not be produced.

        Sent on the status endpoint rather than one of its own: it is the same
        conversation, and the reply is the status document either way. Without
        it the website has nothing to show but a spinner, because a snapshot
        this house cannot build is not something the server can discover.
        """
        return await self._request(
            "POST",
            "integration-status",
            json_body={
                "replan_request_id": replan_request_id,
                # The server caps this; sending more would be refused outright
                # and lose the explanation altogether.
                "error": error[:MAX_REPLAN_ERROR_CHARS],
            },
        )

    async def tariff(self) -> dict[str, Any]:
        """Fetch the global catalogue and questionnaire-derived home inputs."""
        return await self._request("GET", "integration-tariff")

    async def prices(
        self,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> dict[str, Any]:
        """Fetch SHS-calculated supplier prices for the requested local dates."""
        query = {
            key: value
            for key, value in (("from", start_date), ("to", end_date))
            if value is not None
        }
        path = "integration-prices"
        if query:
            path = f"{path}?{urlencode(query)}"
        return await self._request("GET", path)

    async def push_readings(
        self,
        readings: list[dict[str, Any]],
        calculations: list[dict[str, Any]] | None = None,
        supplier_costs: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Push daily readings and monthly calculations; idempotent server-side."""
        return await self._request(
            "POST",
            "ha-energy-ingest",
            json_body={
                "readings": readings,
                "calculations": calculations or [],
                "supplier_costs": supplier_costs or [],
            },
        )

    async def push_optimisation(self, *args, **kwargs) -> dict[str, Any]:
        """Send a non-durable telemetry exchange using the shared envelope."""
        return await self.submit_optimisation(self.prepare_optimisation_submission(*args, **kwargs))

    @staticmethod
    def prepare_optimisation_submission(
        actual_slots: list[dict[str, Any]],
        snapshot: dict[str, Any] | None = None,
        devices: list[dict[str, Any]] | None = None,
        thermal_slots: list[dict[str, Any]] | None = None,
        price_slots: list[dict[str, Any]] | None = None,
        pool_slots: list[dict[str, Any]] | None = None,
        *,
        device_inventory_complete: bool = False,
        replan_request_id: str | None = None,
        replan_recommendation: str | None = None,
        execution_replan: ExecutionReplan | None = None,
        equipment: dict[str, bool] | None = None,
        replaces_job_id: str | None = None,
    ) -> dict[str, Any]:
        """Build one canonical credential-free telemetry/capture envelope."""
        body: dict[str, Any] = {
            "api_version": API_VERSION,
            "accepted_plan_schema_versions": sorted(
                SUPPORTED_PLAN_SCHEMA_VERSIONS
            ),
            "integration_version": INTEGRATION_VERSION,
            "actual_slots": actual_slots,
            "devices": devices or [],
        }
        if execution_replan is not None and snapshot is None:
            raise ValueError("execution replanning requires a captured snapshot")
        if replan_recommendation:
            body["replan_recommendation"] = replan_recommendation[:1000]
        if equipment is not None:
            body["equipment"] = equipment
        if device_inventory_complete:
            body["device_inventory_complete"] = True
        # Optional quarter series are omitted when this is only a device
        # inventory exchange; an explicitly complete empty inventory remains
        # meaningful through device_inventory_complete.
        if thermal_slots:
            body["thermal_slots"] = thermal_slots
        if price_slots:
            body["price_slots"] = price_slots
        if pool_slots:
            body["pool_slots"] = pool_slots
        if snapshot is not None:
            body["snapshot"] = snapshot
            body["planning_exchange_version"] = PLANNING_EXCHANGE_VERSION
            if execution_replan is not None:
                body["execution_replan"] = execution_replan
            # Only a snapshot can answer a request, because only a snapshot
            # produces a plan. The server refuses the pair without one.
            if replan_request_id is not None:
                body["replan_request_id"] = replan_request_id
            if replaces_job_id is not None:
                body["replaces_job_id"] = replaces_job_id
        elif replaces_job_id is not None:
            raise ValueError("recapture requires a fresh snapshot")
        # Match the durable journal before the FIRST send: nested key order
        # contributes to the server capture hash, including after restart.
        return json.loads(encoded(body))

    async def submit_optimisation(self, body: dict[str, Any]) -> dict[str, Any]:
        """Send the exact credential-free envelope journalled by the caller."""
        result = await self._request(
            "POST", "energy-optimisation-ingest", json_body=body,
        )
        requires_receipt = any(body.get(key) is not None for key in
                               ("execution_replan", "replan_request_id", "replaces_job_id"))
        if requires_receipt or "job_id" in result or result.get("pending") is True or result.get("plan") is not None:
            self._validate_planning_receipt(result)
            if result.get("snapshot_id") != body.get("snapshot", {}).get("snapshot_id"):
                raise ShsApiError("planning response belongs to another snapshot", code="invalid_response_envelope", path="energy-optimisation-ingest")
        return result

    @staticmethod
    def _validate_planning_receipt(result: dict[str, Any]) -> None:
        """Reject receipts which cannot identify and deliver accepted work."""
        state = result.get("state")
        valid = (
            isinstance(result.get("job_id"), str) and bool(result["job_id"])
            and state in ("pending", "published", "failed", "superseded")
            and result.get("pending") is (state == "pending")
        )
        if state == "pending":
            delay = result.get("retry_after_ms")
            valid = (valid and not isinstance(delay, bool)
                     and isinstance(delay, (int, float))
                     and math.isfinite(delay) and delay > 0)
        if state == "published":
            plan = result.get("plan")
            valid = (valid and isinstance(plan, dict)
                     and isinstance(result.get("plan_id"), str) and bool(result["plan_id"])
                     and isinstance(result.get("snapshot_id"), str) and bool(result["snapshot_id"])
                     and result["plan_id"] == plan.get("plan_id")
                     and result["snapshot_id"] == plan.get("snapshot_id"))
        last_error = result.get("last_error")
        if last_error is not None:
            valid = (valid and state == "pending" and isinstance(last_error, dict)
                     and all(isinstance(last_error.get(key), str) and bool(last_error[key])
                             for key in ("code", "detail")))
        recapture = result.get("recapture")
        if recapture is not None:
            valid = (valid and state == "failed" and result.get("code") in ("planner_upgraded", "capture_horizon_ended", "accepted_reference_changed")
                     and isinstance(recapture, dict) and set(recapture) == {"replaces_job_id"}
                     and recapture["replaces_job_id"] == result.get("job_id"))
        if not valid:
            raise ShsApiError("planning response has an invalid job receipt", code="invalid_response_envelope", path="energy-optimisation-ingest")

    async def _planning_status(self, identity: str, value: str) -> dict[str, Any]:
        result = await self._request("POST", "energy-optimisation-ingest", json_body={
            "api_version": API_VERSION, "planning_exchange_version": PLANNING_EXCHANGE_VERSION,
            identity: value,
        })
        self._validate_planning_receipt(result)
        if (identity == "job_id" and result["job_id"] != value) or (
            identity == "snapshot_id" and result.get("snapshot_id") != value
        ):
            raise ShsApiError("planning response belongs to another job", code="invalid_response_envelope", path="energy-optimisation-ingest")
        return result

    async def planning_status(self, job_id: str) -> dict[str, Any]:
        """Read accepted work without repeating capture or telemetry."""
        return await self._planning_status("job_id", job_id)

    async def planning_submission_status(self, snapshot_id: str) -> dict[str, Any]:
        """Recover an acceptance whose response was lost in transport."""
        return await self._planning_status("snapshot_id", snapshot_id)


    async def acknowledge_optimisation_plan(
        self,
        plan: dict[str, Any],
        outcome: str,
        error: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Acknowledge the exact generated plan after local validation."""
        return await self._request(
            "POST",
            "energy-optimisation-plan-ack",
            json_body={
                "api_version": API_VERSION,
                "plan_id": plan["plan_id"],
                "snapshot_id": plan["snapshot_id"],
                "plan_schema_version": plan["schema_version"],
                "integration_version": INTEGRATION_VERSION,
                "outcome": outcome,
                "error": error,
            },
        )


def connection_trace():
    """Expose actual request transport activity; never treat it as API recovery."""
    trace = aiohttp.TraceConfig()
    async def headers_sent(session, context, params):
        callback = (context.trace_request_ctx or {}).get('connected')
        if callback:
            callback('connected')
    trace.on_request_headers_sent.append(headers_sent)
    return trace
