"""HTTP client for the Smart Home Solutions edge functions."""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from urllib.parse import urlencode
from uuid import uuid4

import aiohttp

from .api_contract import (
    API_VERSION,
    INTEGRATION_VERSION,
    SUPPORTED_PLAN_SCHEMA_VERSIONS,
)

_LOGGER = logging.getLogger(__name__)

REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=30)


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
    ) -> None:
        self.code = code
        self.request_id = request_id
        self.path = path
        self.retryable = retryable
        suffix = f" [request_id={request_id}]" if request_id else ""
        super().__init__(f"{message}{suffix}")


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
    ) -> None:
        self._session = session
        self._base_url = base_url.rstrip("/")
        self._device_token = device_token

    async def _request(
        self,
        method: str,
        path: str,
        json_body: dict[str, Any] | None = None,
        authenticated: bool = True,
    ) -> dict[str, Any]:
        request_id = str(uuid4())
        headers = {
            "X-Request-ID": request_id,
            "X-SHS-API-Version": str(API_VERSION),
            "X-SHS-Integration-Version": INTEGRATION_VERSION,
        }
        if authenticated:
            if not self._device_token:
                raise ShsAuthError("no device token configured")
            headers["Authorization"] = f"Bearer {self._device_token}"

        try:
            async with self._session.request(
                method,
                f"{self._base_url}/{path}",
                json=json_body,
                headers=headers,
                timeout=REQUEST_TIMEOUT,
            ) as resp:
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
                        raise ShsApiError(
                            message,
                            code=str(structured_error.get("code") or "api_error"),
                            request_id=response_request_id,
                            path=(
                                str(structured_error["path"])
                                if structured_error.get("path") is not None
                                else None
                            ),
                            retryable=bool(structured_error.get("retryable")),
                        )
                    raise ShsApiError(
                        f"{path} returned HTTP {resp.status} without an SHS API envelope",
                        code="upstream_transport_failure",
                        request_id=response_request_id,
                        retryable=resp.status >= 500,
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
                return payload["data"]
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise ShsApiError(
                f"connection error calling {path}: {err}",
                code="upstream_transport_failure",
                request_id=request_id,
                retryable=True,
            ) from err

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

    async def status(self) -> dict[str, Any]:
        """Fetch subscription status for the paired customer."""
        return await self._request("GET", "integration-status")

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

    async def push_optimisation(
        self,
        actual_slots: list[dict[str, Any]],
        snapshot: dict[str, Any] | None = None,
        devices: list[dict[str, Any]] | None = None,
        thermal_slots: list[dict[str, Any]] | None = None,
        price_slots: list[dict[str, Any]] | None = None,
        pool_slots: list[dict[str, Any]] | None = None,
        *,
        device_inventory_complete: bool = False,
    ) -> dict[str, Any]:
        """Push aggregate, per-device and thermal quarters, plus a plan."""
        body: dict[str, Any] = {
            "api_version": API_VERSION,
            "accepted_plan_schema_versions": sorted(
                SUPPORTED_PLAN_SCHEMA_VERSIONS
            ),
            "integration_version": INTEGRATION_VERSION,
            "actual_slots": actual_slots,
            "devices": devices or [],
        }
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
        return await self._request(
            "POST",
            "energy-optimisation-ingest",
            json_body=body,
        )

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
