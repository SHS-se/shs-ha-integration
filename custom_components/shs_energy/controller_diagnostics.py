"""Local all-mode controller download; never runs a controller or a simulation."""
from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone

if __package__:
    from .controller_observations import diagnostic_inventory, observation_entities, field_entities
    from .presentation import execution_view
    from .verification import observation, evaluation_record, VerificationJournal
    from .api_contract import INTEGRATION_VERSION
    from .configuration_fields import _control_fields, POWER_FIELD, OPTIONAL_TEMPERATURE_FIELD
else:
    from controller_observations import diagnostic_inventory, observation_entities, field_entities
    from presentation import execution_view
    from verification import observation, evaluation_record, VerificationJournal
    from api_contract import INTEGRATION_VERSION
    from configuration_fields import _control_fields, POWER_FIELD, OPTIONAL_TEMPERATURE_FIELD


def session_summary(attempts, evaluations, samples):
    return {"verification_groups": len(attempts), "verification_checks": sum(r["count"] for r in attempts),
            "runtime_groups": len(evaluations), "runtime_evaluations": sum(r["count"] for r in evaluations),
            "observation_samples": len(samples),
            "first_observation_at": samples[0]["at"] if samples else None,
            "last_observation_at": samples[-1]["at"] if samples else None,
            "integration_versions": sorted({r["integration_version"] for r in attempts + evaluations}),
            "coverage": VerificationJournal.coverage_for(attempts)}


def controller_diagnostics(controller, panel):
    """Combine the shared device view with current state and retained evidence."""
    report = controller.verification.export()
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
    for sample in report["samples"]:
        sample["observations"] = {k: v for k, v in sample["observations"].items() if k not in excluded}
        sample["device_power"] = {k: v for k, v in sample["device_power"].items() if k not in excluded}
        sample["energy_intervals"] = {k: v for k, v in sample["energy_intervals"].items() if k not in excluded}
        for value in sample["household"].values():
            if excluded.intersection(value["sources"]):
                value.update(average_w=None, difference_w=None, quality="excluded_source")
    for context in report["sample_contexts"].values():
        context["devices"] = [d for d in context["devices"] if d["key"] not in excluded]
    slots = {row["slot_id"] for row in retained + report["samples"]}
    report["configurations"] = {k: v for k, v in report["configurations"].items() if k in scopes}
    report["slots"] = {k: v for k, v in report["slots"].items() if k in slots}
    attempts = {row["group_id"]: row for row in report["attempts"]}
    for row in report["evaluations"]:
        linked = row.get("verification_group_id")
        row["verification_link_status"] = "retained" if linked in attempts else "not_retained" if linked else "not_recorded"
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
            "configuration": deepcopy(options), "plan": deepcopy(controller.coordinator.optimisation_plan),
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
    return report
