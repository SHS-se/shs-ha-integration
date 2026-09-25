"""Observed appliance runs, independent of plans, permission and ownership.

Only real observations and prepared real starts mutate this journal. Unknown
readings preserve a run; receipt order, never source timestamps, orders events.
"""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from math import isfinite

def minimum_run_errors(mapping):
    value = mapping.get("minimum_on_seconds")
    if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value) or value < 0):
        return {"minimum_on_seconds": ["Minimum run time must be a finite number of zero or more seconds"]}
    return {}


def run_bindings(options, models):
    if __package__:
        from .device_controls import mapped_planning_path
    else:
        from device_controls import mapped_planning_path
    models = {model["key"]: model for model in models}
    result = {}
    for key, mapping in options.get("device_control_mappings", {}).items():
        if minimum_run_errors(mapping) or not mapping.get("minimum_on_seconds"):
            continue
        model = models.get(key, {"key": key, "control_type": mapping.get("control_type")})
        path = mapped_planning_path(model, mapping, options.get("pool_water_temperature_entity"))
        kind = mapping.get("control_type")
        targets = list(mapping.get("actuator_entity_ids", []))
        source = targets[0] if targets else None
        temperature = None
        if path == "ev":
            source = options.get("ev_charge_switch_entity")
            targets = [source, mapping.get("control_entity_id")]
        elif path != "pool" and kind in ("setpoint", "variable_power"):
            target = mapping.get("setpoint_entity_id" if kind == "setpoint" else "control_entity_id")
            if target:
                targets = [target]
                source = target
            if kind == "setpoint":
                temperature = mapping.get("temperature_entity_id")
        if not source:
            continue
        result[key] = {"key": key, "owner": path if path in ("pool", "ev") else "device:" + key,
                       "source": source, "targets": [target for target in targets if target],
                       "kind": "switch" if path in ("pool", "ev") or kind != "setpoint" else "setpoint",
                       "temperature": temperature, "minimum_seconds": float(mapping["minimum_on_seconds"])}
    return result


def _number(value):
    try:
        value = float(value)
        return value if isfinite(value) else None
    except (TypeError, ValueError):
        return None


def enabled(binding, read):
    state = read(binding["source"])
    if state is None or state.state in ("unknown", "unavailable"):
        return None
    domain = binding["source"].split(".")[0]
    if domain == "climate":
        return state.state != "off"
    if domain in ("switch", "input_boolean", "binary_sensor"):
        return {"on": True, "off": False}.get(state.state)
    value = _number(state.state)
    if value is None:
        return None
    if binding["kind"] == "setpoint":
        temperature = read(binding["temperature"]) if binding["temperature"] else None
        measured = _number(temperature.state) if temperature else None
        return value > measured if measured is not None else None
    return value > 0


class RunStateUnavailable(ValueError):
    def __init__(self, entity):
        self.entity = entity
        super().__init__(f"{entity}: enabled state is unavailable for minimum run time")


class MinimumRuns:
    def __init__(self, saved=None):
        self.records = deepcopy(saved or {})
        self.bindings = {}
        self.revision = 0
        self.saved_revision = 0
        for record in self.records.values():
            datetime.fromisoformat(record["since"])
            if not isfinite(record["minimum_seconds"]) or record["minimum_seconds"] < 0:
                raise ValueError("Invalid persisted minimum run time")

    @property
    def dirty(self):
        return self.revision != self.saved_revision

    def configure(self, options, models, now=None):
        now = now or datetime.now(timezone.utc)
        self.bindings = run_bindings(options, models)
        for key, record in list(self.records.items()):
            binding = self.bindings.get(key)
            previous = record["binding"]
            if binding and any(binding[field] != previous[field] for field in ("source", "targets", "temperature")):
                # A replacement actuator cannot inherit another device's start.
                self.records.pop(key)
                key = key + "@" + previous["source"]
                self.records[key] = record
                self.revision += 1
            # Keep a removed actuator's promise only until that run may end.
            release = self.release_at(key)
            if release and release > now and key not in self.bindings:
                self.bindings[key] = previous

    @property
    def entities(self):
        return {entity for binding in self.bindings.values()
                for entity in (binding["source"], binding["temperature"], *binding["targets"]) if entity}

    def observe(self, read, now, *, entity=None, received=False):
        for key, binding in self.bindings.items():
            if entity and entity not in (binding["source"], binding["temperature"], *binding["targets"]):
                continue
            active = enabled(binding, read)
            if active is None:
                continue
            record = self.records.get(key)
            if record and record.get("pending_start") and not active and not received:
                continue
            if record and record.get("pending_start") and active:
                record.pop("pending_start")
                record["since"] = now.isoformat()
                self.revision += 1
            if record is None or active != record["active"]:
                state = read(binding["source"])
                # Bootstrap elapsed time from HA only once. Received transitions
                # use our clock, so source timestamps cannot reorder observations.
                derived = binding["kind"] == "setpoint" and not binding["source"].startswith("climate.")
                at = now if received or record or derived else getattr(state, "last_changed", now)
                self.records[key] = {"active": active, "since": at.isoformat(),
                    "minimum_seconds": binding["minimum_seconds"], "binding": deepcopy(binding)}
                self.revision += 1
            elif active and binding["minimum_seconds"] > record["minimum_seconds"]:
                record["minimum_seconds"] = binding["minimum_seconds"]
                self.revision += 1

    def release_at(self, key):
        record = self.records.get(key)
        if not record or not record["active"]:
            return None
        return datetime.fromisoformat(record["since"]) + timedelta(seconds=record["minimum_seconds"])

    def blocked_until(self, entity, value, read, now):
        deadline = None
        for key, binding in self.bindings.items():
            if entity not in binding["targets"]:
                continue
            state = read(entity)
            stopping = value == "off" or isinstance(value, (int, float)) and value <= 0
            if binding["kind"] == "setpoint" and isinstance(value, (int, float)) and state is not None:
                previous = _number(state.attributes.get("temperature") if entity.startswith("climate.") else state.state)
                stopping = previous is not None and value < previous
            if not stopping:
                continue
            if enabled(binding, read) is None:
                raise RunStateUnavailable(binding["source"])
            release = self.release_at(key)
            if release and release > now:
                deadline = max(deadline, release) if deadline else release
        return deadline

    def prepare_start(self, entity, value, read, now):
        prepared = {}
        for key, binding in self.bindings.items():
            if entity != binding["source"]:
                continue
            activating = value == "on" or isinstance(value, (int, float)) and value > 0
            record = self.records.get(key)
            if binding["kind"] == "setpoint" and isinstance(value, (int, float)):
                temperature = read(binding["temperature"]) if binding["temperature"] else None
                measured = _number(temperature.state) if temperature else None
                activating = measured is not None and value > measured
            if activating and (record is None or not record["active"]):
                self.records[key] = {"active": True, "pending_start": True, "since": now.isoformat(),
                    "minimum_seconds": binding["minimum_seconds"], "binding": deepcopy(binding)}
                prepared[key] = self.records[key]
                self.revision += 1
        return prepared

    def cancel_unsent_start(self, prepared, read, now):
        for key, record in prepared.items():
            if self.records.get(key) is record and record.get("pending_start"):
                record.pop("pending_start")
                record["active"] = enabled(record["binding"], read) is not False
                record["since"] = now.isoformat()
                self.revision += 1

    def snapshot(self, now):
        result = {}
        for key, binding in self.bindings.items():
            release = self.release_at(key)
            record = self.records.get(key)
            result[key] = {"minimum_seconds": binding["minimum_seconds"],
                          "remaining_seconds": max(0, (release - now).total_seconds()) if release else 0,
                          "running": bool(record and record["active"])}
        return result
