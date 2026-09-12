"""Local, session-scoped controller counters; no telemetry values are exported."""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from time import perf_counter


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).digest()


def timing():
    return {"completed": 0, "elapsed_ms": 0.0, "max_elapsed_ms": 0.0, "over_5_seconds": 0}


def record_time(totals, started):
    elapsed = (perf_counter() - started) * 1000
    totals["completed"] += 1
    totals["elapsed_ms"] += elapsed
    totals["max_elapsed_ms"] = max(totals["max_elapsed_ms"], elapsed)
    totals["over_5_seconds"] += int(elapsed > 5000)


class ControllerMetrics:
    def __init__(self, integration_version):
        self.integration_version = integration_version
        self.started_at = datetime.now(timezone.utc).isoformat()
        self.started = perf_counter()
        self.triggers = {}
        self.devices = {}
        self.previous = {}
        self.active = None

    def trigger(self, source, skipped=None):
        if source not in {"startup", "timer", "slot_boundary", "coordinator_update", "manual"}:
            source = "manual"
        totals = self.triggers.setdefault(source, {
            "requested": 0, "skipped_busy": 0, "skipped_inactive": 0, **timing(),
        })
        totals["requested"] += 1
        if skipped:
            totals["skipped_" + skipped] += 1
        return totals

    def begin_device(self, device, context):
        # Entity ids can identify rooms/people. Only opaque generic-device keys
        # leave this meter. Cap cardinality across configuration edits as well.
        key = device if device in {"battery", "ev", "pool"} else "device:" + fingerprint(device).hex()[:12]
        if key not in self.devices and len(self.devices) >= 128:
            key = "overflow"
        totals = self.devices.setdefault(key, {
            "first_observation": 0, "changed_inputs": 0, "unchanged_inputs": 0,
            "unchanged_inputs_new_reports": 0, "modes": {}, **timing(),
        })
        mode = context["mode"]
        totals["modes"][mode] = totals["modes"].get(mode, 0) + 1
        self.active = {"key": key, "started": perf_counter(),
                       "context": fingerprint(context), "values": {}, "reports": {}}

    def observe(self, entity, state):
        active = self.active
        if active is None or entity in active["values"]:
            return
        # First real read only: simulated writes and later live readbacks must
        # not replace the input with the result of this evaluation's commands.
        active["values"][entity] = fingerprint(
            None if state is None else (state.state, dict(state.attributes)))
        active["reports"][entity] = str(getattr(state, "last_reported", None))

    def end_device(self):
        active, self.active = self.active, None
        key = active["key"]
        totals = self.devices[key]
        current = (active["context"], active["values"], active["reports"])
        previous = self.previous.get(key)
        if previous is None:
            totals["first_observation"] += 1
        elif current[:2] != previous[:2]:
            totals["changed_inputs"] += 1
        else:
            totals["unchanged_inputs"] += 1
            totals["unchanged_inputs_new_reports"] += int(current[2] != previous[2])
        self.previous[key] = current
        record_time(totals, active["started"])

    def snapshot(self):
        return {
            "schema_version": 1, "integration_version": self.integration_version,
            "started_at": self.started_at,
            "observed_seconds": perf_counter() - self.started,
            "scope": "Since integration load; in memory only; resets on reload/restart",
            "timing_basis": "Elapsed wall time including awaits and measurement overhead; not CPU time",
            "input_basis": "Plan/configuration/ownership context and first real state/attribute reads per device evaluation; report timestamps counted separately. Unchanged inputs do not imply a safe-to-skip evaluation: time-based protections still apply. No complete sensor event stream is recorded.",
            "triggers": deepcopy(self.triggers), "devices": deepcopy(self.devices),
        }
