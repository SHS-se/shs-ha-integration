"""Bounded controller evidence, with simulated and real evaluations separate."""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from time import monotonic
from uuid import uuid4

if __package__:
    from .battery_commands import OPERATIONS as BATTERY_OPERATIONS
else:
    from battery_commands import OPERATIONS as BATTERY_OPERATIONS

MAX_GROUPS = 2000
MAX_LIFECYCLE_EVENTS = 500
MAX_SAMPLES = 720
SAVE_INTERVAL_SECONDS = 60
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


def _signature(record):
    """Compare decisions, not telemetry that changes without affecting commands."""
    signature = {key: value for key, value in record.items() if key not in (
        "at", "last_at", "count", "observations", "last_observations",
        "final_observations", "last_final_observations", "completed_at", "group_id")}
    signature["commands"] = [{key: value for key, value in command.items() if key not in ("at", "completed_at")}
                             for command in record.get("commands", [])]
    # Pool temperature is supporting evidence; actual setpoints, limited state,
    # reasons and command values still distinguish decisions.
    signature["result"] = {key: value for key, value in record.get("result", {}).items()
                           if key != "water_temperature_c"}
    return signature


def observation(state):
    return {"state": state.state if state else None,
            "attributes": dict(state.attributes) if state else {},
            "last_reported": str(state.last_reported) if getattr(state, "last_reported", None) else None,
            "last_updated": str(state.last_updated) if getattr(state, "last_updated", None) else None}


def evaluation_record(device, mode, options, slot, plan, version, expected=()):
    configuration = json.dumps({"version": version, "options": options}, sort_keys=True, default=str)
    return {"device": device, "scope": device + ":" + hashlib.sha256(configuration.encode()).hexdigest()[:16],
            "at": datetime.now(timezone.utc).isoformat(), "mode": mode,
            "integration_version": version, "plan_id": plan.get("plan_id"),
            "slot_start": slot.get("start") if slot else None, "slot": deepcopy(slot),
            "plan_schema_version": plan.get("schema_version"), "plan_issued_at": plan.get("issued_at"),
            "configuration": deepcopy(options), "expected_operations": list(expected),
            "operations": [], "outcome": "blocked", "commands": [], "observations": {}}


class VerificationJournal:
    def __init__(self, store):
        self.store = store
        self.events = []
        self.session_id = None
        self.attempts = []
        self.evaluations = []
        self.samples = []
        self.sample_contexts = {}
        self.discarded_samples = 0
        self.configurations = {}
        self.slots = {}
        self.discarded = 0
        self.dirty = False
        self.last_saved = monotonic()

    async def load(self):
        saved = await self.store.async_load()
        if saved is None:
            return
        version = saved.get("schema_version", 1)
        if version not in (1, 2, 3, 4):
            raise ValueError(f"Unsupported verification journal schema: {version}")
        self.events = saved.get("lifecycle_events", [])
        self.discarded = saved.get("discarded_attempts", 0)
        self.evaluations = saved["evaluations"] if version >= 3 else []
        if version == 1:
            # One-time roll-forward: compact existing evidence on upgrade.
            for attempt in saved["attempts"]:
                self._record({**attempt, "configuration": saved["configurations"][attempt["scope"]]})
        else:
            self.attempts = saved["attempts"]
            self.configurations = saved["configurations"]
            self.slots = saved["slots"]
        if version == 4:
            self.samples = saved["samples"]
            self.sample_contexts = saved["sample_contexts"]
            self.discarded_samples = saved["discarded_samples"]
        if version < 4:
            for record in self.attempts + self.evaluations:
                record.setdefault("group_id", str(uuid4()))
            self.dirty = True
            await self.flush()

    async def lifecycle(self, event, version, reason):
        previous = (self.events, self.session_id, self.dirty)
        if event == "start":
            self.session_id = str(uuid4())
        record = {"event": event, "at": datetime.now(timezone.utc).isoformat(),
                  "integration_version": version, "session_id": self.session_id, "reason": reason}
        if event == "start" and self.events:
            record["previous_session_missing_stop"] = self.events[-1]["event"] != "stop"
        self.events = [*self.events, record][-MAX_LIFECYCLE_EVENTS:]
        self.dirty = True
        try:
            await self.flush()
        except Exception:
            self.events, self.session_id, self.dirty = previous
            raise

    def _record(self, attempt, *, runtime=False):
        record = deepcopy(attempt)
        if self.session_id is not None:
            record["session_id"] = self.session_id
        configuration = record.pop("configuration")
        slot = record.pop("slot")
        slot_id = hashlib.sha256(json.dumps(slot, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        record.update(slot_id=slot_id, count=1, last_at=record["at"], group_id=str(uuid4()))
        collection = self.evaluations if runtime else self.attempts
        previous_index = next((i for i in range(len(collection) - 1, -1, -1)
                               if collection[i]["device"] == record["device"]), None)
        previous = collection[previous_index] if previous_index is not None else None
        repeated = previous is not None and _signature(previous) == _signature(record)
        attempts = list(collection)
        if repeated:
            attempts[previous_index] = {**previous, "count": previous["count"] + 1,
                                       "last_at": record["at"],
                                       "last_observations": record.get("observations", {}),
                                       "last_final_observations": record.get("final_observations", {})}
        else:
            attempts.append(record)
        removed = attempts[:-MAX_GROUPS]
        attempts = attempts[-MAX_GROUPS:]
        retained = attempts + (self.attempts if runtime else self.evaluations)
        scopes = {row["scope"] for row in retained}
        slot_ids = {row["slot_id"] for row in retained + self.samples}
        self.configurations = {key: value for key, value in self.configurations.items() if key in scopes}
        self.configurations[record["scope"]] = configuration
        self.slots = {key: value for key, value in self.slots.items() if key in slot_ids}
        self.slots[slot_id] = slot
        if runtime:
            self.evaluations = attempts
        else:
            self.attempts = attempts
        self.discarded += sum(row["count"] for row in removed)
        self.dirty = True
        return repeated, previous["group_id"] if repeated else record["group_id"]

    async def append(self, attempt, *, runtime=False):
        previous = (self.attempts, self.evaluations, self.configurations, self.slots, self.discarded, self.dirty)
        repeated, group_id = self._record(attempt, runtime=runtime)
        try:
            # New decisions/failures are durable immediately. Repeated checks
            # checkpoint at most once a minute, and flush on clean shutdown.
            if not repeated or monotonic() - self.last_saved >= SAVE_INTERVAL_SECONDS:
                await self.flush()
        except Exception:
            self.attempts, self.evaluations, self.configurations, self.slots, self.discarded, self.dirty = previous
            raise

        return group_id

    async def sample(self, options, devices, read, *, at, version, slot, plan_id):
        if __package__:
            from .controller_observations import measurement_sample
        else:
            from controller_observations import measurement_sample
        context = {"integration_version": version, "configuration": deepcopy(options),
                   "devices": [{key: deepcopy(row.get(key)) for key in
                                ("key", "name", "controller_id", "mode", "mapping", "configured_mapping", "statistic_id", "category", "planning_role", "control_type")}
                               for row in devices]}
        context_id = hashlib.sha256(json.dumps(context, sort_keys=True, default=str).encode()).hexdigest()
        slot_id = hashlib.sha256(json.dumps(slot, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        sample = measurement_sample(options, devices, read, at=at, session_id=self.session_id,
            context_id=context_id, slot_id=slot_id, plan_id=plan_id, slot=slot,
            previous=self.samples[-1] if self.samples else None)
        previous = (self.samples, self.sample_contexts, self.slots, self.discarded_samples, self.dirty)
        self.discarded_samples += max(0, len(self.samples) + 1 - MAX_SAMPLES)
        self.samples = [*self.samples, sample][-MAX_SAMPLES:]
        contexts = {row["context_id"] for row in self.samples}
        self.sample_contexts = {key: value for key, value in self.sample_contexts.items() if key in contexts}
        self.sample_contexts[context_id] = context
        slots = {row["slot_id"] for row in self.samples + self.attempts + self.evaluations}
        self.slots = {key: value for key, value in self.slots.items() if key in slots}
        self.slots[slot_id] = deepcopy(slot)
        self.dirty = True
        try:
            await self.flush()
        except Exception:
            self.samples, self.sample_contexts, self.slots, self.discarded_samples, self.dirty = previous
            raise

    async def flush(self):
        if not self.dirty:
            return
        await self.store.async_save({"schema_version": 4, "samples": self.samples, "sample_contexts": self.sample_contexts,
                                    "discarded_samples": self.discarded_samples, "attempts": self.attempts, "evaluations": self.evaluations,
                                    "configurations": self.configurations, "slots": self.slots,
                                    "lifecycle_events": self.events,
                                    "discarded_attempts": self.discarded})
        self.dirty = False
        self.last_saved = monotonic()

    @staticmethod
    def coverage_for(attempts):
        coverage = {}
        for attempt in attempts:
            # A mapping change starts a separate coverage scope. Old actuator
            # evidence cannot commission a newly selected piece of equipment.
            scope = attempt["scope"]
            item = coverage.setdefault(scope, {"scope": scope, "device": attempt["device"], "expected": attempt["expected_operations"], "observed": set()})
            if attempt["outcome"] == "verified":
                item["observed"].update(attempt["operations"])
        for item in coverage.values():
            item["observed"] = sorted(item["observed"])
            item["missing"] = sorted(set(item["expected"]) - set(item["observed"]))
            item["covered"] = len(item["observed"])
            item["total"] = len(item["expected"])
        return list(coverage.values())

    def export(self):
        return {"schema_version": 4, "session_id": self.session_id, "exported_at": datetime.now(timezone.utc).isoformat(),
                "lifecycle_events": deepcopy(self.events),
                "retention": {"max_lifecycle_events": MAX_LIFECYCLE_EVENTS, "max_groups": MAX_GROUPS,
                              "max_runtime_groups": MAX_GROUPS, "discarded_attempts": self.discarded,
                              "max_samples": MAX_SAMPLES, "sample_interval_seconds": 60, "discarded_samples": self.discarded_samples},
                "coverage_definition": "Successful command-generation branches for each configuration. Includes simulated handover. Does not prove physical response, all numeric values, failure paths or transitions between slots.",
                "configurations": deepcopy(self.configurations), "slots": deepcopy(self.slots),
                "aggregation_definition": "Consecutive equivalent decisions per device, scoped to configuration and plan slot. at/observations/commands describe the first check; final_observations contains its last actual reads. last_at, last_observations and last_final_observations describe repeated checks; count is the number of represented checks. Verification never proves physical response; runtime results retain the controller's device-specific evidence.",
                "runtime_definition": "Observed controller evaluations, including real service attempts and handover. Transport acceptance and setting readback do not by themselves prove physical delivery. No evaluations are invented for passive devices or periods before recording began. Simulation commands appear only in attempts; a runtime evaluation can include real release commands before verification.",
                "measurement_definition": "Read-only samples of all non-excluded inventory devices, collected about once a minute without control evaluations. Raw sample timestamps, source reporting times, modes and active slots are retained. Counter deltas estimate average power between sample boundaries; source reporting delay limits alignment. source_interval_average_w uses the actual reporting interval. A reporting interval differing by over five seconds from the sample interval is excluded from household sums; reports before the active slot are excluded from plan comparisons. Missing, stale and reset readings are not zero. No interpolation across sessions, configuration changes or gaps over two minutes. Planned comparisons require the same active plan and slot at both boundaries. Household plan values may include hypothetical Planning and Control verification devices; differences do not by themselves identify a controller fault. These samples are not proof of a causal response to a command.",
                "samples": deepcopy(self.samples), "sample_contexts": deepcopy(self.sample_contexts),
                "coverage": self.coverage_for(self.attempts), "attempts": deepcopy(self.attempts),
                "evaluations": deepcopy(self.evaluations)}
