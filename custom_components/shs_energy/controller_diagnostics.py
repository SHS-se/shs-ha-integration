"""Local all-mode controller download; never runs a controller or a simulation."""
from copy import deepcopy
from datetime import datetime, timezone
import re

if __package__:
    from .operating_modes import device_mode
    from .verification import observation
else:
    from operating_modes import device_mode
    from verification import observation


def _entity_ids(value):
    if isinstance(value, dict):
        for key, item in value.items():
            yield from _entity_ids(key)
            yield from _entity_ids(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _entity_ids(item)
    elif isinstance(value, str) and re.fullmatch(r"[a-z_]+\.[a-z0-9_]+", value):
        yield value


def controller_diagnostics(controller, panel):
    """Combine the shared device view with current state and retained evidence."""
    report = controller.verification.export()
    options = panel["configuration"]
    excluded = set(options.get("excluded_device_readings", []))
    devices = {row["key"]: deepcopy(row) for row in panel["devices"]}
    # Keep unmodelled/unmapped meters and locally configured devices visible,
    # even when the Schedule view has no executable card for them.
    for row in panel["meter_inventory"]:
        devices.setdefault(row["key"], deepcopy(row))
    for key, mapping in options.get("device_control_mappings", {}).items():
        devices.setdefault(key, {"key": key, "mapping": deepcopy(mapping)})
    excluded_controllers = {"device:" + key for key in excluded}
    rows = []
    for key, device in devices.items():
        identity = device.get("permission", {}).get("controller_id") or device.get("system") or "device:" + key
        if key in excluded:
            excluded_controllers.add(identity)
            continue
        device.update(controller_id=identity, mode=device_mode(options, identity))
        latest = next((row for row in reversed(report["evaluations"]) if row["device"] == identity), None)
        device["last_evaluated_at"] = latest["last_at"] if latest else None
        device["last_evaluated_mode"] = latest["mode"] if latest else None
        device["execution_status"] = deepcopy(controller.status.get(identity))
        rows.append(device)
    identities = {row["controller_id"] for row in rows} - excluded_controllers
    for field in ("attempts", "evaluations", "coverage"):
        report[field] = [row for row in report[field] if row["device"] in identities]
    retained = report["attempts"] + report["evaluations"]
    scopes = {row["scope"] for row in retained}
    slots = {row["slot_id"] for row in retained}
    report["configurations"] = {k: v for k, v in report["configurations"].items() if k in scopes}
    report["slots"] = {k: v for k, v in report["slots"].items() if k in slots}
    entities = set(_entity_ids(options)) | set(_entity_ids(rows))
    report.update(
        kind="controller_diagnostics",
        scope="All configured devices except explicit reading exclusions, regardless of operating mode. Current configuration and the whole household plan retain shared context; this is not a redacted export.",
        current={
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "devices": rows,
            "configuration": deepcopy(options),
            "plan": deepcopy(controller.coordinator.optimisation_plan),
            "active_slot": deepcopy(controller.coordinator.current_plan_slot),
            "operation": deepcopy(panel["operation"]),
            "readiness": deepcopy(panel["readiness"]),
            "observations": {entity: observation(controller.hass.states.get(entity)) for entity in sorted(entities)},
            "ownership": {key: deepcopy(value) for key, value in controller.records.items() if key in identities},
            "overrides": {key: value for key, value in controller.overrides.items() if key in identities},
            "failure_latched": sorted(identities & controller.failed.keys()),
            "recording_error": controller.diagnostics_error,
            "failed_evaluations_this_session": controller.diagnostics_failed_evaluations,
        },
        controller_metrics=controller.metrics.snapshot(),
    )
    return report
