"""Local all-mode controller download; never runs a controller or a simulation."""
from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
import zlib

if __package__:
    from .controller_observations import diagnostic_inventory, observation_entities, field_entities
    from .presentation import execution_view
    from .verification import observation, evaluation_record, VerificationJournal
    from .api_contract import INTEGRATION_VERSION
    from .configuration_fields import _control_fields, POWER_FIELD, OPTIONAL_TEMPERATURE_FIELD
    from .runtime_json import Records, record_json
else:
    from controller_observations import diagnostic_inventory, observation_entities, field_entities
    from presentation import execution_view
    from verification import observation, evaluation_record, VerificationJournal
    from api_contract import INTEGRATION_VERSION
    from configuration_fields import _control_fields, POWER_FIELD, OPTIONAL_TEMPERATURE_FIELD
    from runtime_json import Records, record_json

# JSON fragments reach zlib in batches of this size. Compression releases the
# GIL, so the event loop keeps running beside the worker thread.
_COMPRESS_BATCH = 1 << 20
# On a real 131 MB export, level 3 took under half the time of the default
# level 6 for a file about a quarter larger (13.1 MB instead of 10.6 MB).
_COMPRESS_LEVEL = 3


def session_summary(attempts, evaluations, samples):
    return {"verification_groups": len(attempts), "verification_checks": sum(r["count"] for r in attempts),
            "runtime_groups": len(evaluations), "runtime_evaluations": sum(r["count"] for r in evaluations),
            "observation_samples": len(samples),
            "first_observation_at": samples[0]["at"] if samples else None,
            "last_observation_at": samples[-1]["at"] if samples else None,
            "integration_versions": sorted({r["integration_version"] for r in attempts + evaluations}),
            "coverage": VerificationJournal.coverage_for(attempts)}


def controller_diagnostics(controller, panel):
    """Combine the shared device view with current state and retained evidence.

    The report shares the verification journal's records and the current plan
    rather than copying them, and holds the battery journals as immutable
    Records. It is read-only: pass it to `report_parts` before the next await.
    """
    report = controller.verification.export(copy=False)
    options = panel["configuration"]
    excluded = set(options.get("excluded_device_readings", []))
    rows, unassigned = diagnostic_inventory(panel["devices"], panel["meter_inventory"], options)
    for item in unassigned:
        references = observation_entities({}, [{"key": item["key"], "mapping": item["mapping"]}]) - excluded
        item["observations"] = {entity: observation(controller.hass.states.get(entity)) for entity in sorted(references)}
    identities = {row["controller_id"] for row in rows}
    for device in rows:
        identity = device["controller_id"]
        latest = next((row for row in reversed(report["evaluations"]) if row["device"] == identity), None)
        suggestions = device.get("suggested_mapping", {})
        suggested_entities = field_entities(suggestions, (*_control_fields({**device, **suggestions}), POWER_FIELD, OPTIONAL_TEMPERATURE_FIELD)) - excluded
        device["suggested_observations"] = {entity: observation(controller.hass.states.get(entity)) for entity in sorted(suggested_entities)}
        device["last_evaluated_at"] = latest["last_at"] if latest else None
        device["last_evaluated_mode"] = latest["mode"] if latest else None
        device["last_controller_result"] = deepcopy(controller.status.get(identity))
        device["execution_status"] = execution_view(device["mode"], controller.status.get(identity))
        device["current_configuration_scope"] = evaluation_record(identity, device["mode"], options, None, {}, INTEGRATION_VERSION)["scope"]
    for field in ("attempts", "evaluations", "coverage"):
        report[field] = [row for row in report[field] if row["device"] in identities]
    retained = report["attempts"] + report["evaluations"]
    scopes = {row["scope"] for row in retained}
    # Journal records are shared: filtered and annotated rows are new dicts.
    report["samples"] = [_without_excluded(sample, excluded) for sample in report["samples"]]
    report["sample_contexts"] = {key: {**context, "devices": [d for d in context["devices"] if d["key"] not in excluded]}
                                 for key, context in report["sample_contexts"].items()}
    slots = {row["slot_id"] for row in retained + report["samples"]}
    report["configurations"] = {k: v for k, v in report["configurations"].items() if k in scopes}
    report["slots"] = {k: v for k, v in report["slots"].items() if k in slots}
    attempts = {row["group_id"]: row for row in report["attempts"]}
    report["evaluations"] = [{**row, "verification_link_status": "retained" if row.get("verification_group_id") in attempts
                              else "not_retained" if row.get("verification_group_id") else "not_recorded"}
                             for row in report["evaluations"]]
    current_session = report["session_id"]
    current, history = [], []
    for field in ("attempts", "evaluations", "samples"):
        current.append([r for r in report[field] if current_session is not None and r.get("session_id") == current_session])
        history.append([r for r in report[field] if current_session is None or r.get("session_id") != current_session])
    report["current_session"] = {"session_id": current_session, "integration_version": INTEGRATION_VERSION,
        "started_at": next((e["at"] for e in report["lifecycle_events"] if e["event"] == "start" and e.get("session_id") == current_session), None),
        **session_summary(*current)}
    report["historical_summary"] = session_summary(*history)
    current_scopes = {row["current_configuration_scope"] for row in rows}
    report["current_session"]["current_configuration_coverage"] = controller.verification.coverage_for(
        [row for row in current[0] if row["scope"] in current_scopes])
    entities = observation_entities(options, rows)
    report.update(
        kind="controller_diagnostics",
        scope="All inventory devices except explicit reading exclusions, regardless of mode. Unassigned local mappings are listed separately and do not count as devices. Configuration and household plans retain shared context; this is not a redacted export.",
        current={
            "captured_at": datetime.now(timezone.utc).isoformat(), "devices": rows,
            "device_counts_by_mode": dict(Counter(row["mode"] for row in rows)),
            "unassigned_mappings": unassigned,
            "configuration": deepcopy(options), "plan": controller.coordinator.optimisation_plan,
            "active_slot": deepcopy(controller.coordinator.current_plan_slot),
            "operation": deepcopy(panel["operation"]), "readiness": deepcopy(panel["readiness"]),
            "observations": {entity: observation(controller.hass.states.get(entity)) for entity in sorted(entities)},
            "ownership": {key: deepcopy(value) for key, value in controller.records.items() if key in identities},
            "overrides": {key: value for key, value in controller.overrides.items() if key in identities},
            "failure_latched": sorted(identities & controller.failed.keys()),
            "recording_error": controller.diagnostics_error,
            "failed_evaluations_this_session": controller.diagnostics_failed_evaluations,
            "sampling_error": controller.diagnostics_sampling_error,
            "failed_samples_this_session": controller.diagnostics_failed_samples,
        },
        controller_metrics=controller.metrics.snapshot(),
    )
    runtime = getattr(controller, "battery_runtime", None)
    if runtime is not None:
        report["battery_execution"] = runtime.snapshot(include_evidence=True)
    return report


def _without_excluded(sample, excluded):
    household = {key: {**value, "average_w": None, "difference_w": None, "quality": "excluded_source"}
                 if excluded.intersection(value["sources"]) else value
                 for key, value in sample["household"].items()}
    return {**sample, "observations": {k: v for k, v in sample["observations"].items() if k not in excluded},
            "device_power": {k: v for k, v in sample["device_power"].items() if k not in excluded},
            "energy_intervals": {k: v for k, v in sample["energy_intervals"].items() if k not in excluded},
            "household": household}


def report_summary(report):
    """The counts the panel reports once a download completes."""
    session = report["current_session"]
    return {"devices": len(report["current"]["devices"]), "runtime_evaluations": session["runtime_evaluations"],
            "verification_checks": session["verification_checks"], "observation_samples": session["observation_samples"]}


def report_parts(value, dumps):
    """JSON bytes of everything except Records, which stay for `report_fragments`.

    Run it straight after `controller_diagnostics`, before any await: the
    report shares live records. Only mappings that hold Records are written
    key by key; every other value is a single `dumps` (compact JSON bytes).
    """
    if isinstance(value, Records):
        return [value]
    if not _holds_records(value):
        return [dumps(value)]
    parts = [b"{"]
    for index, (key, item) in enumerate(value.items()):
        parts.append((b"," if index else b"") + dumps(key) + b":")
        parts.extend(report_parts(item, dumps))
    parts.append(b"}")
    return parts


def _holds_records(value):
    return isinstance(value, dict) and any(isinstance(item, Records) or _holds_records(item) for item in value.values())


def report_fragments(parts, dumps):
    """The report's JSON as fragments, encoding Records one record at a time; safe in any thread."""
    for part in parts:
        if isinstance(part, Records):
            yield from record_json(part.value, dumps)
        else:
            yield part


def gzip_report(parts, dumps, level=_COMPRESS_LEVEL):
    """Gzip-compressed JSON of `report_parts`, for a worker thread."""
    compressor = zlib.compressobj(level, zlib.DEFLATED, 31)
    compressed, pending, size = [], [], 0
    for fragment in report_fragments(parts, dumps):
        pending.append(fragment)
        size += len(fragment)
        if size >= _COMPRESS_BATCH:
            compressed.append(compressor.compress(b"".join(pending)))
            pending, size = [], 0
    compressed.append(compressor.compress(b"".join(pending)))
    compressed.append(compressor.flush())
    return b"".join(compressed)
