"""Durable battery writer fence shared by legacy commands and runtime grants."""
from __future__ import annotations

from copy import deepcopy

if __package__:
    from .home_runtime import WriterGrant, WriterIdentity
else:
    from home_runtime import WriterGrant, WriterIdentity


class BatteryWriterFence:
    """The existing controller lock drains all legacy writes before a handover.

    A crash after fencing never resurrects legacy ownership. A grant must be
    explicitly renewed against current configuration and physical evidence.
    """
    def __init__(self, store, lock, read_options, now_ms, read_identity):
        self._store, self._lock, self._options, self._now_ms = store, lock, read_options, now_ms
        self._identity = read_identity
        self._record = None
        self._grant = None
        self._closed = False
        self._fault = None

    def _surfaces(self):
        options = self._options()
        return sorted({value for key in ("battery_mode_entity", "battery_charge_limit_entity", "battery_discharge_limit_entity")
                       if isinstance(value := options.get(key), str)})

    def snapshot(self):
        return {"owner": self._record["owner"] if self._record else "unavailable", "epoch": self._record["epoch"] if self._record else 0,
                "grant_current": self.is_current(self._grant, self._identity()),
                "fault": self._fault}

    async def open(self):
        async with self._lock:
            if self._record is not None or self._closed:
                raise ValueError("writer fence can only open once")
            try:
                value = await self._store.async_load()
                if value is None:
                    value = {"schema": "battery-writer-v1", "owner": "legacy", "epoch": 0, "surfaces": self._surfaces()}
                if (type(value) is not dict or set(value) != {"schema", "owner", "epoch", "surfaces"}
                        or value["schema"] != "battery-writer-v1" or value["owner"] not in ("legacy", "fenced", "runtime")
                        or type(value["epoch"]) is not int or not 0 <= value["epoch"] < 2**53
                        or type(value["surfaces"]) is not list or len(value["surfaces"]) > 32
                        or any(type(s) is not str or not s for s in value["surfaces"])
                        or value["surfaces"] != sorted(set(value["surfaces"]))):
                    raise ValueError("invalid battery writer journal")
                self._record = deepcopy(value)
                if value["owner"] == "runtime":
                    self._record["owner"] = "fenced"
                await self._store.async_save(deepcopy(self._record))
            except Exception as error:
                self._fault = type(error).__name__
                self._record = {"schema": "battery-writer-v1", "owner": "fenced", "epoch": 0, "surfaces": self._surfaces()}
                raise

    def check_legacy(self, entity):
        surfaces = set(self._surfaces()) | set(self._record["surfaces"] if self._record else ())
        if entity in surfaces and (self._closed or self._fault or self._record is None or self._record["owner"] != "legacy"):
            raise ValueError("battery writer is fenced; legacy command and restoration are disabled")

    async def take_over(self, identity: WriterIdentity, expires_at_ms: int, release):
        """Caller must admit installation evidence before requesting a writer.

        release runs under the shared lock and must confirm the old owner's
        restoration is complete. Returning a grant does not install a policy.
        """
        if not isinstance(identity, WriterIdentity) or identity != self._identity():
            raise ValueError("an exact writer identity is required")
        async with self._lock:
            if self._closed or self._fault or self._record is None:
                raise ValueError("battery writer journal unavailable")
            if type(expires_at_ms) is not int or not self._now_ms() < expires_at_ms <= self._now_ms() + 900_000:
                raise ValueError("writer grant must expire within this admission window")
            if identity != self._identity():
                raise ValueError("writer configuration changed before handover")
            if self._record["owner"] == "legacy":
                await release()
            # Fence in memory before awaiting persistence. Uncertain writes
            # retain the fence; they cannot fall back to the legacy controller.
            self._grant = None
            self._record = {"schema": "battery-writer-v1", "owner": "fenced", "epoch": self._record["epoch"] + 1,
                            "surfaces": sorted(set(self._record["surfaces"]) | set(self._surfaces()))}
            try:
                await self._store.async_save(deepcopy(self._record))
                if self._closed or self._now_ms() >= expires_at_ms or identity != self._identity():
                    raise ValueError("writer admission expired during persistence")
                grant = WriterGrant(identity.owner_id, self._record["epoch"], identity.config_revision,
                                    identity.control_surface_revision, expires_at_ms)
                self._record = {**self._record, "owner": "runtime"}
                await self._store.async_save(deepcopy(self._record))
                if self._closed or self._now_ms() >= expires_at_ms or identity != self._identity():
                    raise ValueError("writer admission expired during persistence")
                self._grant = grant
                return grant
            except Exception as error:
                self._grant = None
                self._record = {**self._record, "owner": "fenced"}
                self._fault = type(error).__name__
                raise

    def is_current(self, grant, identity):
        return (not self._closed and not self._fault and isinstance(identity, WriterIdentity)
                and identity == self._identity() and grant is not None and grant == self._grant
                and self._record["owner"] == "runtime" and self._now_ms() < grant.expires_at_ms
                and (grant.owner_id, grant.config_revision, grant.control_surface_revision) ==
                    (identity.owner_id, identity.config_revision, identity.control_surface_revision))

    def close(self):
        self._closed = True
        self._grant = None
