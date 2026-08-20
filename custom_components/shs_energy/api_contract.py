"""Generated surface of the private SHS Home Assistant API contract.

Normative source: smart-home-solutions-t-by/contracts/ha-api/openapi.json.
Domain invariants for plans remain in :mod:`optimisation`.
"""

from __future__ import annotations

from typing import Any

API_VERSION = 1
SNAPSHOT_SCHEMA_VERSION = 6
SUPPORTED_SNAPSHOT_SCHEMA_VERSIONS = frozenset({5, 6})
SUPPORTED_PLAN_SCHEMA_VERSIONS = frozenset({5, 6})
MINIMUM_SNAPSHOT_SCHEMA_VERSION = 5
MINIMUM_PLAN_SCHEMA_VERSION = 5
INTEGRATION_VERSION = "0.7.0-beta.30"


class ApiContractError(ValueError):
    """The server and this integration do not share a usable contract."""


def _integer_versions(value: Any, label: str) -> frozenset[int]:
    if (
        not isinstance(value, list)
        or not value
        or any(isinstance(item, bool) or not isinstance(item, int) for item in value)
    ):
        raise ApiContractError(f"integration status {label} is invalid")
    return frozenset(value)


def validate_server_contract(status: Any) -> None:
    """Fail before planning when status advertises no mutually readable API."""
    if not isinstance(status, dict) or status.get("api_version") != API_VERSION:
        raise ApiContractError("server API version is unsupported")
    snapshots = _integer_versions(
        status.get("supported_snapshot_schema_versions"),
        "snapshot schema list",
    )
    plans = _integer_versions(
        status.get("supported_plan_schema_versions"),
        "plan schema list",
    )
    if SNAPSHOT_SCHEMA_VERSION not in snapshots:
        raise ApiContractError(
            f"server does not accept snapshot schema {SNAPSHOT_SCHEMA_VERSION}"
        )
    if SNAPSHOT_SCHEMA_VERSION not in plans:
        raise ApiContractError(
            f"server cannot return plan schema {SNAPSHOT_SCHEMA_VERSION}"
        )
    minimum_snapshot = status.get("minimum_snapshot_schema_version")
    minimum_plan = status.get("minimum_plan_schema_version")
    if (
        isinstance(minimum_snapshot, bool)
        or not isinstance(minimum_snapshot, int)
        or isinstance(minimum_plan, bool)
        or not isinstance(minimum_plan, int)
        or minimum_snapshot > SNAPSHOT_SCHEMA_VERSION
        or minimum_plan > max(SUPPORTED_PLAN_SCHEMA_VERSIONS)
    ):
        raise ApiContractError("server minimum contract is unsupported")
    latest_request = status.get("latest_plan_request_id")
    if latest_request is not None and not isinstance(latest_request, str):
        raise ApiContractError("integration status request correlation is invalid")
