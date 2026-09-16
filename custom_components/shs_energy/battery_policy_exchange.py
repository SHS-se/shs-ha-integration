"""Receive policy diagnostics without granting ownership or sending device commands."""
from __future__ import annotations

import asyncio
from copy import deepcopy
import json
from math import isfinite
import re
from uuid import uuid4

if __package__:
    from .battery_execution_policy import read_execution_policy
else:
    from battery_execution_policy import read_execution_policy


_COMMON = {"schema", "request_id", "plan_id", "snapshot_id", "purpose", "control_authority", "status"}


def read_policy_delivery(value, request, now_ms):
    """A delivered model is still separate from local runtime admission."""
    if not isinstance(value, dict):
        raise ValueError("policy delivery must be an object")
    fields = _COMMON | ({"reasons"} if value.get("status") == "blocked" else
        {"policy", "context_hash", "energy_basis", "energy_origin_kwh", "native_context"})
    if (set(value) != fields or value.get("schema") != "battery-policy-delivery-v1"
            or value.get("purpose") != "verification" or value.get("control_authority") is not False
            or any(value.get(k) != request[k] for k in ("request_id", "plan_id", "snapshot_id"))):
        raise ValueError("policy delivery identity or authority mismatch")
    if value["status"] == "blocked":
        reasons = value["reasons"]
        if not isinstance(reasons, list) or not reasons or len(reasons) > 32 or any(not isinstance(r, str) or not 0 < len(r) <= 2000 for r in reasons):
            raise ValueError("invalid policy blocking reasons")
        return None
    if value["status"] != "delivered" or request["native_context"] is None or value["native_context"] != request["native_context"]:
        raise ValueError("policy differs from the requested native context")
    if not isinstance(value["context_hash"], str) or not re.fullmatch(r"sha256:[a-f0-9]{64}", value["context_hash"]):
        raise ValueError("invalid native context hash")
    if (value["energy_basis"] != "usable_kwh_above_min_soc" or
            type(value["energy_origin_kwh"]) not in (int, float) or
            not isfinite(value["energy_origin_kwh"]) or value["energy_origin_kwh"] < 0):
        raise ValueError("invalid policy energy basis")
    policy = read_execution_policy(json.dumps(value["policy"], allow_nan=False).encode())
    summary, context = policy.summary, request["native_context"]
    if (not summary.from_ms <= now_ms < summary.until_ms or
            summary.actuals_origin_ms != context["source_cut_ms"] or
            summary.until_ms > context["valid_until_ms"] or
            summary.identity.context.intent_revision != request["snapshot_id"] or
            summary.identity.context.catalog_revision != context["catalog_revision"] or
            value["policy"]["supply_scope"] != context["supply_scope"] or
            value["policy"]["operations"] != context["operations"] or
            value["policy"]["reference_id"] != context["reference_id"] or
            value["policy"]["domain"] != context["domain"]):
        raise ValueError("stale or mismatched policy model")
    return policy


class BatteryPolicyUnavailableError(ValueError):
    """A service/policy failure must never direct the user to actuator setup."""
    def __init__(self, status):
        error = status.get("error", {})
        invalid = error.get("code") in {"invalid_response_envelope", "invalid_policy_response"}
        reason = ("Battery policy service returned an invalid response" if invalid else
                  "Battery policy service is unavailable" if status.get("state") == "unreachable" else
                  "Battery policy is unavailable: " + ", ".join(status.get("reasons", [])))
        if error.get("request_id"):
            reason += f" [request_id={error['request_id']}]"
        super().__init__(reason)
        self.fix = {"kind": "diagnostics"}
        self.next_step = ("The service response needs investigation. Download diagnostics and report the request ID. "
                          "Battery setup changes will not correct this response error." if invalid else
                          "Policy delivery retries automatically. If this persists, download diagnostics for investigation.")


class BatteryPolicyExchange:
    """One exchange owner; replies cannot outlive their captured local context.

    read_context supplies admitted plan IDs and a local configuration revision.
    A native context is optional only here: its absence is sent explicitly and
    results in blocked delivery, never inferred ratings, permissions or evidence.
    No port accepts HA services, transitions, writer grants or policy admission.
    """
    def __init__(self, request, read_context, persist, now_ms):
        self._request, self._read_context = request, read_context
        self._persist, self._now_ms = persist, now_ms
        self._lock = asyncio.Lock()
        self._closed = False
        self._context = None
        self._next_ms = 0
        self.energy_origin_kwh = None
        self.policy = None
        self.status = {"state": "not_requested", "reasons": [], "control_authority": False}

    def snapshot(self):
        if self._closed:
            return deepcopy(self.status)
        if self._read_context() != self._context:
            return {"state": "blocked", "reasons": ["local_context_changed"], "control_authority": False}
        if self.policy and self._now_ms() >= self.policy.summary.until_ms:
            return {"state": "blocked", "reasons": ["policy_expired"], "control_authority": False}
        return deepcopy(self.status)

    async def refresh(self, _now=None):
        if self._closed or self._lock.locked():
            return
        async with self._lock:
            context = deepcopy(self._read_context())
            if context is None:
                self._context, self.policy = None, None
                self.status = {"state": "not_requested", "reasons": [], "control_authority": False}
                return
            now = self._now_ms()
            if context == self._context and now < self._next_ms:
                return
            self._context, self.policy = context, None
            native = context["native_context"]
            if native is not None and native.get("config_revision") != context["config_revision"]:
                self.status = {"state": "blocked", "reasons": ["native_context_configuration_mismatch"], "control_authority": False}
                self._next_ms = now + 60_000
                return
            request = {"api_version": 1, "request_id": str(uuid4()), "purpose": "verification",
                "plan_id": context["plan_id"], "snapshot_id": context["snapshot_id"],
                "native_context": deepcopy(context["native_context"])}
            self.status = {"state": "requesting", "reasons": [], "control_authority": False}
            try:
                result = await self._request(request)
                if self._closed:
                    return
                if self._read_context() != context:
                    self.status = {"state": "blocked", "reasons": ["local_context_changed"], "control_authority": False}
                    self._next_ms = 0
                    return
                policy = read_policy_delivery(result, request, self._now_ms())
                status = {"state": "delivered_not_admitted" if policy else "blocked",
                    "reasons": ["runtime_admission_required"] if policy else result["reasons"],
                    "control_authority": False, "plan_id": request["plan_id"], "at_ms": self._now_ms()}
                # Persist diagnostics before advertising successful delivery.
                await self._persist({"schema": "battery-policy-delivery-log-v1", **status})
                if self._closed:
                    return
                if self._read_context() != context:
                    self.status = {"state": "blocked", "reasons": ["local_context_changed"], "control_authority": False}
                    self._next_ms = 0
                    return
                self.energy_origin_kwh = result.get("energy_origin_kwh")
                self.policy, self.status = policy, status
                self._next_ms = max(self._now_ms() + 60_000, policy.summary.refresh_after_ms) if policy else (now // 900_000 + 1) * 900_000
            except Exception as error:
                if self._closed:
                    return
                self.policy = None
                if self._read_context() != context:
                    self.status = {"state": "blocked", "reasons": ["local_context_changed"], "control_authority": False}
                    self._next_ms = 0
                    return
                self.status = {"state": "unreachable", "reasons": [f"{type(error).__name__}: {error}"], "control_authority": False,
                    "error": {"code": getattr(error, "code", "invalid_policy_response" if isinstance(error, ValueError) else "policy_exchange_failed"),
                              "request_id": getattr(error, "request_id", None)}}
                self._next_ms = self._now_ms() + 60_000

    def close(self):
        self._closed = True
        self.policy = None
        self.status = {"state": "stopped", "reasons": [], "control_authority": False}
