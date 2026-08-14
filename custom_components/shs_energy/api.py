"""HTTP client for the Smart Home Solutions edge functions."""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlencode

import aiohttp

_LOGGER = logging.getLogger(__name__)

REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=30)


class ShsApiError(Exception):
    """Base error talking to SHS."""


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
        headers = {}
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
                    payload: dict[str, Any] = await resp.json()
                except (aiohttp.ContentTypeError, ValueError):
                    payload = {}

                if resp.status == 401:
                    raise ShsAuthError(payload.get("error", "unauthorized"))
                if resp.status == 402:
                    raise ShsSubscriptionInactiveError("subscription_inactive")
                if resp.status >= 400:
                    # The server names the offending value in `detail`; without
                    # it a rejected batch gives no clue which row was at fault.
                    message = f"{path} failed: {resp.status} {payload.get('error', '')}"
                    detail = payload.get("detail")
                    raise ShsApiError(f"{message} ({detail})" if detail else message)
                return payload
        except aiohttp.ClientError as err:
            raise ShsApiError(f"connection error calling {path}: {err}") from err

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
        *,
        device_inventory_complete: bool = False,
    ) -> dict[str, Any]:
        """Push aggregate, per-device and thermal quarters, plus a plan."""
        body: dict[str, Any] = {
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
        if snapshot is not None:
            body["snapshot"] = snapshot
        return await self._request(
            "POST",
            "energy-optimisation-ingest",
            json_body=body,
        )
