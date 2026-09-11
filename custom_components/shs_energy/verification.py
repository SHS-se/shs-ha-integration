"""One bounded, persistent verification journal for every local controller."""
from copy import deepcopy
from datetime import datetime, timezone

if __package__:
    from .battery_commands import OPERATIONS as BATTERY_OPERATIONS
else:
    from battery_commands import OPERATIONS as BATTERY_OPERATIONS

MAX_ATTEMPTS = 20000
OPERATIONS = {
    "battery": (*sorted(BATTERY_OPERATIONS), "handover"),
    "pool": ("heat", "defer", "handover"),
    "ev": ("charge", "stop", "handover"),
    "setpoint": ("setpoint", "handover"),
    "switch_schedule": ("on", "off", "handover"),
    "permit_inhibit": ("permit", "inhibit", "handover"),
}


def operation_name(device, kind, slot, result):
    if device == "battery":
        return slot["battery_command"]["operation"]
    if device == "pool":
        return "heat" if slot["pool_w"] > 0 else "defer"
    if device == "ev":
        return "stop" if result["state"] == "stopped" else "charge"
    if kind == "setpoint":
        return "setpoint"
    command = slot["device_commands"][device.removeprefix("device:")]
    if kind == "permit_inhibit":
        return "permit" if command["permitted"] else "inhibit"
    return "on" if command["on_seconds"] else "off"


class VerificationJournal:
    def __init__(self, store):
        self.store = store
        self.attempts = []
        self.configurations = {}
        self.discarded = 0

    async def load(self):
        saved = await self.store.async_load() or {}
        self.attempts = saved.get("attempts", [])
        self.configurations = saved.get("configurations", {})
        self.discarded = saved.get("discarded_attempts", 0)

    async def append(self, attempt):
        record = deepcopy(attempt)
        configuration = record.pop("configuration")
        attempts = [*self.attempts, record]
        removed = max(0, len(attempts) - MAX_ATTEMPTS)
        attempts = attempts[removed:]
        scopes = {row["scope"] for row in attempts}
        configurations = {key: value for key, value in self.configurations.items() if key in scopes}
        configurations[record["scope"]] = configuration
        saved = {"attempts": attempts, "configurations": configurations,
                 "discarded_attempts": self.discarded + removed}
        await self.store.async_save(saved)
        self.attempts, self.configurations = attempts, configurations
        self.discarded = saved["discarded_attempts"]

    def export(self):
        coverage = {}
        for attempt in self.attempts:
            # A mapping change starts a separate coverage scope. Old actuator
            # evidence cannot commission a newly selected piece of equipment.
            scope = attempt["scope"]
            item = coverage.setdefault(scope, {"device": attempt["device"], "expected": attempt["expected_operations"], "observed": set()})
            if attempt["outcome"] == "verified":
                item["observed"].update(attempt["operations"])
        for item in coverage.values():
            item["observed"] = sorted(item["observed"])
            item["missing"] = sorted(set(item["expected"]) - set(item["observed"]))
            item["covered"] = len(item["observed"])
            item["total"] = len(item["expected"])
        return {"schema_version": 1, "exported_at": datetime.now(timezone.utc).isoformat(),
                "retention": {"max_attempts": MAX_ATTEMPTS, "discarded_attempts": self.discarded},
                "coverage_definition": "Successful command-generation branches for each configuration. Includes simulated handover. Does not prove physical response, all numeric values, failure paths or transitions between slots.",
                "configurations": deepcopy(self.configurations),
                "coverage": list(coverage.values()), "attempts": deepcopy(self.attempts)}
