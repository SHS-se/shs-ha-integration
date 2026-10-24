"""Deferrable-service construction for the optimisation snapshot.

Home Assistant owns the clock, the local timezone and live entity states; this
module receives all three and reaches for none of them. That is deliberate:
this logic used to sit inside the coordinator, which the test suite cannot
import at all because Home Assistant is not installed in CI. The result was
that the planner's most failure-prone code was covered only by assertions
against the coordinator's *source text*, which pass just as happily when the
behaviour is wrong.

Each service is selected by the control contract its devices were mapped with
(see :func:`device_controls.planning_path`) and sized from the meters that
service actually controls, never from the whole meter category.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone, tzinfo
from typing import Any, Callable, Optional
from math import isfinite

from .planning_warnings import planning_warning

from .const import (
    EV_CHARGE_EFFICIENCY,
    EU_AC_PHASE_COUNT,
    EU_AC_PHASE_VOLTAGE,
    OPT_DEVICE_CONTROL_MAPPINGS,
    OPT_EV_CONNECTED_ENTITY,
    OPT_EV_ENERGY_REMAINING_ENTITY,
    OPT_EV_SOC_ENTITY,
    OPT_EV_TARGET_SOC_ENTITY,
    OPT_EV_ENABLED,
    OPT_POOL_ENABLED,
    OPT_POOL_START_TEMPERATURE_ENTITY,
    OPT_POOL_STOP_TEMPERATURE_ENTITY,
    OPT_POOL_WATER_TEMPERATURE_ENTITY,
    OPT_EV_CHARGE_EFFICIENCY,
    OPT_EV_KWH_PER_KM,
    DEFAULT_EV_KWH_PER_KM,
)
from .const import (
    OPTIMISATION_PROFILE_DAYS,
)
from .device_controls import CONTROL_TYPES, planning_path, mapped_planning_path
from .optimisation import (
    OptimisationInputError,
    REMEDY_DEFECT,
    REMEDY_WAITING,
    build_device_load_model,
    boiler_daily_energy_forecast,
    service_energy_today,
    discrete_current_control,
    normalized_fraction,
    parse_number,
    state_is_on,
    validate_service_windows,
)

# Reads one entity as ``{"state": str, "attributes": dict}``, raising
# OptimisationInputError when it is missing or unavailable.
EntityReader = Callable[[str], dict[str, Any]]
# Resolves one control mapping's reviewed watts, or its live power sensor.
PowerReader = Callable[[dict[str, Any]], Optional[float]]


def _positive_option(
    options: dict[str, Any],
    key: str,
    fallback: float,
) -> float:
    """Read a positive numeric option, falling back rather than failing.

    A blank field or a zero would otherwise reach the electrical model, where
    it divides. Falling back to the shipped default keeps the home plannable
    and leaves the wrong value visible in the panel where it can be corrected.
    """
    value = options.get(key)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value) if float(value) > 0 else fallback
    if isinstance(value, str) and value.strip():
        try:
            parsed = float(value.replace(",", "."))
        except ValueError:
            return fallback
        return parsed if parsed > 0 else fallback
    return fallback


def pool_heating_actuators(
    models: list[dict[str, Any]],
    mappings: dict[str, Any],
    *,
    pool_water_entity: str | None = None,
) -> set[str]:
    """Native switches that together enable the pool's mapped heater."""
    entities: set[str] = set()
    for model in models:
        if model.get("planning_role") != "controllable" or mapped_planning_path(
            model, mappings.get(model["key"]) or {}, pool_water_entity
        ) != "pool":
            continue
        mapping = mappings.get(model["key"], {})
        actuators = mapping.get("actuator_entity_ids", [])
        if not actuators:
            return set()
        entities.update(actuators)
        entities.update(mapping.get("companion_actuator_entity_ids", []))
    return entities


def pool_heating_running(models, mappings, entity_state, *, pool_water_entity=None) -> bool | None:
    """Whether the mapped heating switches are already enabled, never inferred from watts."""
    entities = pool_heating_actuators(models, mappings, pool_water_entity=pool_water_entity)
    if not entities:
        return None
    states = [entity_state(entity) for entity in sorted(entities)]
    if any(state == "off" or state is False for state in states):
        return False
    if all(state == "on" or state is True for state in states):
        return True
    return None


def pool_heater_settings(options, entity_payload) -> dict:
    """The heater's own start and stop temperatures, as it is set right now.

    The planner projects when the heat pump itself will stop, so it needs the
    equipment's settings rather than a copy someone typed in once. SHS enables
    the heater through its mapped switch and never writes either setting.
    """
    entities = {
        "start": options.get(OPT_POOL_START_TEMPERATURE_ENTITY),
        "stop": options.get(OPT_POOL_STOP_TEMPERATURE_ENTITY),
    }
    missing = [
        key for end, key in (("start", OPT_POOL_START_TEMPERATURE_ENTITY), ("stop", OPT_POOL_STOP_TEMPERATURE_ENTITY))
        if not (isinstance(entities[end], str) and entities[end].strip())
    ]
    if missing:
        raise OptimisationInputError(
            "Pool heater start and stop temperatures are not configured",
            fix={"kind": "fields", "fields": [{"key": key} for key in missing]},
        )
    return {
        "start_c": parse_number(entity_payload(entities["start"])["state"], entities["start"]),
        "stop_c": parse_number(entity_payload(entities["stop"])["state"], entities["stop"]),
        "control": "external_enable",
        "source_entity_ids": entities,
    }



def pool_electrical_history(models, history, start, captured):
    """Metered current-quarter input; switch outages do not erase counter deltas.

    Recorder supplies the boundary state and changes through capture. Missing
    or trailing unavailable states remain missing. Any observed counter reset
    invalidates this interval, even if the final total exceeds the first one.
    """
    counters = []
    for model in models:
        if model.get('planning_service') != 'pool':
            continue
        first = last = previous = None
        valid = True
        for at, state, attributes in history.get(model['statistic_id'], []):
            if state in ('unknown', 'unavailable', None):
                value = None
            else:
                unit = (attributes or {}).get('unit_of_measurement')
                kind = (attributes or {}).get('state_class')
                try:
                    value = float(state) * {'Wh': .001, 'kWh': 1, 'MWh': 1000}[unit]
                except (ValueError, TypeError, KeyError):
                    valid = False
                    break
                if kind not in ('total', 'total_increasing') or not isfinite(value) or value < 0:
                    valid = False
                    break
                if previous is not None and value < previous:
                    valid = False
                    break
                previous = value
            if at <= start:
                first = value
            last = value
        if valid and first is not None and last is not None:
            counters.append(dict(device_key=model['key'], start_kwh=first, end_kwh=last))
    return dict(start=start.isoformat(), end=captured.isoformat(), counters=counters)


def pool_heating_runtime(entities, history, captured) -> dict:
    """Confirmed enabled age. Unknown/reconnected observations do not restart a run.

    Recorder rows are consumed in their supplied order. Their timestamps measure
    duration; they are never used to reject or reorder received live observations.
    An initial on row gives a lower bound on an already-running heater's age.
    """
    ages = []
    lower_bound = False
    for entity in sorted(entities):
        started = None
        previous = None
        interrupted = False
        basis = "observed_on_lower_bound"
        for at, state, _attributes in history.get(entity, []):
            if state == "off" or state is False:
                started = None
                previous = "off"
                interrupted = False
            elif (state == "on" or state is True) and started is None:
                started = at
                basis = "confirmed_transition" if previous == "off" and not interrupted else "observed_on_lower_bound"
                previous = "on"
                interrupted = False
            elif state not in ("on", True):
                interrupted = True
        if started is None:
            return {"heating_elapsed_seconds": None}
        ages.append(max(0, (captured - started).total_seconds()))
        lower_bound = lower_bound or basis == "observed_on_lower_bound"
    return {"heating_elapsed_seconds": min(ages) if ages else None,
            "heating_elapsed_basis": "observed_on_lower_bound" if lower_bound else "confirmed_transition"}


def pool_heating_history(entities, history, start, captured) -> dict | None:
    """Observed enabled intervals, including runs that stopped before capture.

    Each actuator's recorder rows retain their supplied order. Intersecting their
    enabled intervals expresses the same AND as ``pool_heating_running``. Missing
    or unavailable observations leave history unknown, never a fabricated off run.
    """
    if not entities:
        return None
    combined = None
    complete = True
    for entity in sorted(entities):
        segments = []
        previous_at = None
        previous_state = None
        run_start = None
        basis = "observed_on_lower_bound"
        for at, state, _attributes in history.get(entity, []):
            if previous_at is not None:
                segments.append((previous_at, at, previous_state, run_start, basis))
            known = "on" if state in ("on", True) else "off" if state in ("off", False) else None
            if known == "on" and previous_state != "on":
                run_start = at
                basis = "confirmed_transition" if previous_state == "off" else "observed_on_lower_bound"
            elif known != "on":
                run_start = None
            previous_at, previous_state = at, known
        if previous_at is not None:
            segments.append((previous_at, captured, previous_state, run_start, basis))
        covered_until = start
        enabled = []
        for left, right, state, began, run_basis in segments:
            left, right = max(start, left), min(captured, right)
            if right <= left:
                continue
            if left > covered_until or state is None:
                complete = False
            covered_until = max(covered_until, right)
            if state == "on":
                enabled.append((left, right, began, run_basis))
        if covered_until < captured or previous_at is None:
            complete = False
        if combined is None:
            combined = enabled
        else:
            intersection = []
            for left, right, began, run_basis in combined:
                for other_left, other_right, other_began, other_basis in enabled:
                    overlap_start, overlap_end = max(left, other_left), min(right, other_right)
                    if overlap_end > overlap_start:
                        latest_start = max(began, other_began)
                        latest_basis = run_basis if began >= other_began else other_basis
                        intersection.append((overlap_start, overlap_end, latest_start, latest_basis))
            combined = intersection
    # Repeated recorder observations of the same enabled run are one interval.
    intervals = []
    for left, right, began, basis in combined:
        elapsed = max(0, (left - began).total_seconds())
        if intervals and intervals[-1]["end"] == left.isoformat() and intervals[-1]["elapsed_basis"] == basis:
            previous = intervals[-1]
            age = previous["elapsed_seconds_at_start"] + (left - datetime.fromisoformat(previous["start"])).total_seconds()
            if age == elapsed:
                previous["end"] = right.isoformat()
                continue
        intervals.append({"start": left.isoformat(), "end": right.isoformat(),
                          "elapsed_seconds_at_start": elapsed, "elapsed_basis": basis})
    return {"start": start.isoformat(), "end": captured.isoformat(), "complete": complete, "intervals": intervals}


def build_services(
    options: dict[str, Any],
    horizon: list[datetime],
    device_models: list[dict[str, Any]],
    *,
    read_entity: EntityReader,
    local_tz: tzinfo,
    ev_capacity_kwh: float | None = None,
    device_actuals: list[dict[str, Any]] = (),
) -> tuple[list[dict[str, Any]], dict[str, int], dict[str, Any] | None]:
    """Build the deferrable services, their sample counts and EV battery state.

    ``ev_capacity_kwh`` is the battery size last derived from the car's own
    readings, used while it reports no charge to derive one from.
    """
    first = horizon[0]
    end = horizon[-1] + timedelta(minutes=15)

    services: list[dict[str, Any]] = []
    samples: dict[str, int] = {}
    ev_battery: dict[str, Any] | None = None
    switched_off = disabled_store_paths(options)
    raw_mappings = options.get(OPT_DEVICE_CONTROL_MAPPINGS, {})
    if not isinstance(raw_mappings, dict):
        raise OptimisationInputError("device control mappings must be an object")

    def mapped_controls(
        path: str,
    ) -> list[tuple[dict[str, Any], dict[str, Any]]]:
        """Return the models one planning model owns, with their mappings.

        Selection is by control contract, never by meter category: the same
        category may hold both a deferrable service and a room heater.
        """
        if path in switched_off:
            return []
        pairs: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for model in device_models:
            if mapped_planning_path(model, raw_mappings.get(str(model["key"])) or {},
                                    options.get(OPT_POOL_WATER_TEMPERATURE_ENTITY)) != path:
                continue
            control_type = model["control_type"]
            mapping = raw_mappings.get(str(model["key"]))
            if (
                not isinstance(mapping, dict)
                or mapping.get("control_type") != control_type
            ):
                raise OptimisationInputError(
                    f"{model['name']} has no saved {control_type} mapping"
                )
            pairs.append((model, mapping))
        return pairs

    def required_entity(option_key: str, label: str) -> str:
        entity_id = options.get(option_key)
        if not isinstance(entity_id, str) or not entity_id.strip():
            raise OptimisationInputError(f"{label} is not configured")
        return entity_id

    pool_controls = mapped_controls("pool")
    if pool_controls:
        category = "pool_heating"
        device = "pool"
        rated_power_w = sum(
            (model.get("active_power_w") or 0)
            for model, _mapping in pool_controls
        )
        if rated_power_w < 0:
            raise OptimisationInputError("Pool heating power must be positive")
        samples[category] = 0  # No historical daily energy requirement.
        # Carry the hardware contract even with no history or heat demand.
        # Temperature and the value curve determine energy in the store planner.
        if rated_power_w > 0:
            services.append({
                "id": f"{device}:{first.isoformat()}",
                "device": device,
                "earliest_start": first.isoformat(),
                "deadline": end.isoformat(),
                "required_kwh": 0.0,
                "control": {"type": "fixed_power", "power_w": rated_power_w},
                "priority": 2,
                "baseline_preferred_start": first.isoformat(),
            })

    boiler_controls = mapped_controls("boiler")
    if boiler_controls:
        boiler_models = [model for model, _mapping in boiler_controls]
        expected_w = [
            round(sum(model["forecast_w_by_slot"][index]
                      for model in boiler_models), 2)
            for index in range(len(horizon))
        ]
        ratings = [model.get("active_power_w") for model in boiler_models]
        rated_power_w = (sum(ratings) if all(
            type(watts) in (int, float) and isfinite(watts) and watts > 0
            for watts in ratings
        ) else None)
        # Demand is a forecast, while active watts can be a current sensor
        # reading or a learned running level. Their difference is not a
        # configuration failure and must not discard the household plan.
        samples["hot_water"] = sum(
            int(model["profile_sample_count"]) for model in boiler_models
        )
        by_day: dict[date, list[int]] = {}
        for index, slot in enumerate(horizon):
            by_day.setdefault(slot.astimezone(local_tz).date(), []).append(index)
        for day, indices in sorted(by_day.items()):
            # A packed run can deliver the day's energy before its historical
            # clock-time profile. Replanning must subtract measured delivery,
            # not simply schedule the remaining tail of that old profile again.
            target_kwh = sum(
                model["inference"]["daily_energy_kwh_by_weekday"][day.weekday()]
                for model in boiler_models
            )
            delivered_kwh = service_energy_today(
                device_actuals, (model["key"] for model in boiler_models), day, local_tz,
            )
            required_kwh = max(0.0, target_kwh - delivered_kwh)
            services.append({
                "id": f"boiler:{day.isoformat()}",
                "device": "boiler",
                "earliest_start": horizon[indices[0]].isoformat(),
                "deadline": (
                    horizon[indices[-1]] + timedelta(minutes=15)
                ).isoformat(),
                "required_kwh": round(required_kwh, 5),
                "control": {
                    "type": "duty_cycle",
                    "rated_power_w": rated_power_w,
                    "expected_power_w_by_slot": expected_w,
                },
                "priority": 1,
            })

    charge_efficiency = _positive_option(
        options, OPT_EV_CHARGE_EFFICIENCY, EV_CHARGE_EFFICIENCY
    )
    kwh_per_km = _positive_option(
        options, OPT_EV_KWH_PER_KM, DEFAULT_EV_KWH_PER_KM
    )

    ev_controls = mapped_controls("ev")
    if ev_controls:
        connected_id = required_entity(
            OPT_EV_CONNECTED_ENTITY,
            "Vehicle connected-state entity",
        )
        control_signatures: set[tuple[str, float, float]] = set()
        for model, mapping in ev_controls:
            control_signatures.add((
                str(mapping.get("control_entity_id") or ""),
                parse_number(
                    mapping.get("minimum_value"),
                    f"{model['name']} minimum current",
                ),
                parse_number(
                    mapping.get("maximum_value"),
                    f"{model['name']} maximum current",
                ),
            ))
        if (
            len(control_signatures) != 1
            or not next(iter(control_signatures), ("", 0, 0))[0]
        ):
            raise OptimisationInputError(
                "EV charging meters must share one variable-power number entity and range"
            )
        current_entity, configured_min, configured_max = next(
            iter(control_signatures)
        )
        current_payload = read_entity(current_entity)
        if current_payload["attributes"].get("unit_of_measurement") != "A":
            raise OptimisationInputError(
                f"{current_entity} must declare unit A"
            )
        # The saved mapping defines the installation's operating range.
        # Entity min/max metadata may change with the vehicle's current state;
        # it must not invalidate the commissioned range or household planning.
        configured_step = parse_number(
            current_payload["attributes"].get("step"),
            f"{current_entity} step",
        )
        if configured_step <= 0:
            raise OptimisationInputError(
                f"{current_entity} step must be positive"
            )
        _ = read_entity(connected_id)  # fail early on a broken control route
    # Vehicle state is a measurement; the control route is a separate contract.
    # Reading it here rather than inside the control branch is what lets an
    # unrouted charger still report the car — the planner declines to dispatch
    # what it may not control, and now says so instead of omitting the store.
    if options.get(OPT_EV_CONNECTED_ENTITY) and "ev" not in switched_off:
        connected_id = required_entity(
            OPT_EV_CONNECTED_ENTITY,
            "Vehicle connected-state entity",
        )
        connected = state_is_on(read_entity(connected_id)["state"])
        soc_id = required_entity(
            OPT_EV_SOC_ENTITY,
            "Vehicle battery SOC entity",
        )
        target_id = required_entity(
            OPT_EV_TARGET_SOC_ENTITY,
            "Vehicle charge-limit entity",
        )
        soc_payload = read_entity(soc_id)
        soc = normalized_fraction(
            soc_payload["state"], OPT_EV_SOC_ENTITY,
            soc_payload["attributes"].get("unit_of_measurement"),
        )
        target_payload = read_entity(target_id)
        target = normalized_fraction(
            target_payload["state"], OPT_EV_TARGET_SOC_ENTITY,
            target_payload["attributes"].get("unit_of_measurement"),
        )
        remaining_entity = required_entity(
            OPT_EV_ENERGY_REMAINING_ENTITY,
            "Vehicle usable-energy-remaining entity",
        )
        remaining = parse_number(
            read_entity(remaining_entity)["state"], remaining_entity
        )
        # An empty car is realistic state, not an error: its battery size is
        # the one last derived from a reading with charge in it. The snapshot
        # builder leaves the car out when no such reading exists yet.
        if soc > 0 and remaining > 0:
            capacity = remaining / soc
        elif ev_capacity_kwh:
            capacity = ev_capacity_kwh
        else:
            raise OptimisationInputError(
                "vehicle battery size cannot be derived while it reports no charge"
            )

        ev_battery = {
            "name": str(
                soc_payload["attributes"].get("friendly_name") or "EV battery"
            ),
            "connected": connected,
            "capacity_kwh": round(capacity, 3),
            "soc": round(soc, 6),
            "charge_limit_soc": round(target, 6),
            "charge_efficiency": round(charge_efficiency, 4),
            "kwh_per_km": round(kwh_per_km, 4),
            "available_from": first.isoformat(),
            "priority": 3,
            "source_entity_ids": {
                "connected": connected_id,
                "soc": soc_id,
                "target_soc": target_id,
                "energy_remaining": remaining_entity,
                "charge_current": current_entity if ev_controls else None,
            },
        }

        # Only a routed charger produces a service. Without one the vehicle is
        # reported and not planned, which is the state the store diagnostics
        # exist to name.
        required = (
            max(0.0, target - soc) * capacity / charge_efficiency
            if ev_controls else 0.0
        )
        # The service is also the charger's hardware contract. Keep it in the
        # snapshot regardless of cable state or remaining energy. Planning
        # assumes the car can be plugged in; cable state stays live telemetry.
        # Schema-6 marginal dispatch may still
        # buy cheap surplus, and the server must never invent phase/current
        # limits for that decision.
        if ev_controls:
            control = discrete_current_control(
                configured_min,
                configured_max,
                configured_step,
                EU_AC_PHASE_COUNT,
                EU_AC_PHASE_VOLTAGE,
                label=current_entity,
            )
            services.append({
                "id": f"ev:{end.isoformat()}",
                "device": "ev",
                "earliest_start": first.isoformat(),
                "deadline": end.isoformat(),
                "required_kwh": round(required, 3),
                "control": control,
                "priority": 3,
                "baseline_preferred_start": first.isoformat(),
            })
    validate_service_windows(services, horizon)
    return services, samples, ev_battery


# Telemetry that proves a home owns a service, against the planning path that
# has to exist before the planner can act on it.
#
# Each option is paired with the label it carries on the configuration panel,
# because an option key is an implementation detail: telling a customer that
# `ev_soc_entity` is configured names something they have never seen and cannot
# search for.
# Which store each toggle switches off, by the planning path the rest of the
# system already routes on.
_STORE_ENABLED_OPTION: dict[str, str] = {
    "pool": OPT_POOL_ENABLED,
    "ev": OPT_EV_ENABLED,
}


def disabled_store_paths(options: dict[str, Any]) -> set[str]:
    """Return the planning paths this home has switched off.

    One source of truth, because the alternative is three: the services built
    here, the capability flags the coordinator derives from routed meters, and
    the unplanned-service report. Those are computed in different places from
    different inputs, and a store that is off has to disappear from all three or
    the snapshot contradicts itself — a capability claiming a service the
    snapshot does not carry is the failure `planned_paths` was introduced to
    stop.

    Absent means enabled. `optimisation_defaults` supplies True, and reading a
    missing key as off would silently unplan every home that upgrades before it
    next saves its configuration.
    """
    return {
        path
        for path, key in _STORE_ENABLED_OPTION.items()
        if key in options and not options[key]
    }


_SERVICE_EVIDENCE: tuple[
    tuple[str, str, tuple[tuple[str, str], ...], str, str], ...
] = (
    (
        "ev",
        "vehicle",
        (
            (OPT_EV_CONNECTED_ENTITY, "Vehicle connected state"),
            (OPT_EV_SOC_ENTITY, "Vehicle battery SOC"),
            (OPT_EV_TARGET_SOC_ENTITY, "Vehicle target SOC"),
            (OPT_EV_ENERGY_REMAINING_ENTITY, "Usable energy remaining"),
        ),
        "ev_charging",
        "Controllable \u00b7 Variable power",
    ),
    (
        "pool",
        "pool",
        ((OPT_POOL_WATER_TEMPERATURE_ENTITY, "Pool water temperature"),),
        "pool_heating",
        "Controllable \u00b7 Switch schedule",
    ),
)


def unplanned_services(
    options: dict[str, Any],
    planned_paths: set[str | None],
    meters: dict[str, dict[str, Any]] | None = None,
) -> list[str]:
    """Name each service this home has telemetry for but no control route.

    Not an error: a home may own a pool thermometer and no controllable heater,
    and that is a legitimate configuration. It must never be *silent*, though,
    and it was. The capability flags are derived from the routed device models,
    so a meter the website left in base load drops the whole store: the server
    receives no vehicle at all, builds no bid for it, and writes no diagnostic
    row explaining the omission. A car sat plugged in below its own charge
    limit for two days while surplus was exported, and every surface said
    "ready" with no errors.

    The asymmetry is what makes this worth reporting. Telemetry is configured
    per service and the control route is configured per *meter*, on the other
    side of the boundary, so the two can disagree without either side looking
    wrong on its own. Comparing them is the only place that disagreement is
    visible.

    The message names what the customer can see: the panel's own field labels,
    the entity ids they chose, and the meter whose role has to change. Naming
    option keys instead sent them looking for settings that do not exist.
    """
    reports: list[str] = []
    switched_off = disabled_store_paths(options)
    for path, subject, fields, category, control in _SERVICE_EVIDENCE:
        if path in planned_paths:
            continue
        # A store the customer switched off is not a home missing a control
        # route. Reporting it would send them to the website to fix a meter
        # they deliberately took out of planning.
        if path in switched_off:
            continue
        category_devices = [meter for meter in (meters or {}).values() if meter.get("category") == category and planning_path(meter.get("control_type"), category) != "room"]
        if category_devices and all(meter.get("planning_role") == "base_load" and meter.get("planning_choice_at") for meter in category_devices):
            continue
        configured = [
            f"{label} ({options[key]})"
            for key, label in fields
            if isinstance(options.get(key), str) and options[key].strip()
        ]
        if not configured:
            continue
        # The meters that could carry this service, by the website's own names.
        candidates = sorted(
            str(meter.get("name") or key)
            for key, meter in (meters or {}).items()
            if meter.get("category") == category
        )
        remedy = (
            f"set {' or '.join(candidates)} to \u201c{control}\u201d on the website"
            if candidates
            else (
                f"no meter is classified as {category.replace('_', ' ')} on the "
                "website, so add one there first"
            )
        )
        reports.append(
            f"A {subject} is configured here \u2014 {', '.join(configured)} \u2014 "
            f"but no meter is set to control it, so it is not being planned. "
            f"To fix: {remedy}."
        )
    return reports


def thermostat_facts(mapping, read_state):
    """Capture a native thermostat without inventing missing sensor/action data.

    These are snapshot observations, not persisted device training parameters.
    A number-only target cannot report its own sensor and HVAC latch.
    """
    from .device_commands import actuator_targets
    targets = actuator_targets(mapping)
    if len(targets) != 1 or not targets[0].startswith("climate."):
        return None
    state = read_state(targets[0])
    if state is None or state.state != "heat":
        return None
    attrs = state.attributes
    fields = dict(minimum_c="min_temp", maximum_c="max_temp", step_c="target_temp_step",
                  current_temperature_c="current_temperature", target_temperature_c="temperature")
    values = {key: attrs.get(attribute) for key, attribute in fields.items()}
    if (any(type(v) not in (int, float) or not isfinite(v) for v in values.values())
            or values["step_c"] <= 0 or values["maximum_c"] < values["minimum_c"]
            or not values["minimum_c"] <= values["target_temperature_c"] <= values["maximum_c"]
            or attrs.get("hvac_action") not in ("heating", "idle", "off")):
        return None
    return dict(entity_id=targets[0], **values,
                hvac_action="heating" if attrs["hvac_action"] == "heating" else "idle")


def build_device_models(
    devices: list[dict[str, Any]],
    device_actuals: list[dict[str, Any]],
    horizon: list[datetime],
    control_mappings: dict[str, Any],
    *,
    mapped_power_w: PowerReader,
    local_tz: tzinfo,
    pool_water_entity: str | None = None,
    boiler_running_power_w: dict[str, float] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Build control models independently of historical profile readiness.

    Every device is inspected before anything is raised, so a multi-device
    setup is not repaired one rediscovered failure at a time.

    Each entry of ``devices`` is updated in place with the power, sample count
    and inference just learned. That is load-bearing rather than incidental:
    the caller uploads the same list and persists those fields as the device
    metadata for the next run.
    """
    device_models: list[dict[str, Any]] = []
    # Every device is inspected before anything is raised. One device's gap
    # used to hide the rest, which turned a multi-device setup into a queue
    # of one-at-a-time repairs.
    device_gaps: list[str] = []
    degraded: list[dict[str, str]] = []
    for device in devices:
        planning_role = device["planning_role"]
        control_type = device["control_type"]
        if (
            planning_role == "base_load" and control_type is not None
        ) or (
            planning_role == "controllable"
            and control_type not in CONTROL_TYPES
        ) or planning_role not in ("base_load", "controllable"):
            device_gaps.append(
                f"{device['name']} has an invalid planning role or control type"
            )
            continue
        empirical = build_device_load_model(
            device_actuals, device["key"], str(local_tz),
            minimum_samples=2, allow_partial=True,
        )
        device["profile_status"] = (
            "ready" if empirical["method"] == "pooled_shape_weekday_level_v1"
            else "warming" if empirical["sample_count"] else "unavailable"
        )
        empirical_active_power_w = empirical["active_power_w"]
        mapping = control_mappings.get(str(device["key"]), {})
        # Named apart from the `mapped_power_w` reader deliberately: binding the
        # result to the reader's own name works for the first device and then
        # calls a float on the second.
        path = mapped_planning_path(device, mapping, pool_water_entity)
        reviewed_power_w = mapped_power_w(
            mapping if isinstance(mapping, dict) else {}
        )
        previous_power_w = device.get("active_power_w")
        if not (type(previous_power_w) in (int, float)
                and isfinite(previous_power_w) and previous_power_w > 0):
            previous_power_w = None
        active_power_w = (
            round(reviewed_power_w, 1) if reviewed_power_w is not None
            else empirical_active_power_w if empirical_active_power_w is not None
            else previous_power_w
        )
        if path == "boiler":
            # Quarter means describe energy, not an element's running draw.
            # Preserve observed on-cycle power while its live sensor is idle.
            previous = device["inference"].get("running_power_w")
            previous = previous if type(previous) in (int, float) and isfinite(previous) and previous > 0 else 0
            live = reviewed_power_w if reviewed_power_w is not None and reviewed_power_w >= 500 else 0
            active_power_w = (boiler_running_power_w or {}).get(device["key"]) or max(previous, live) or None
        device["active_power_w"] = active_power_w
        device["profile_sample_count"] = int(empirical["sample_count"])
        device["inference"] = {
            **device["inference"],
            "history_days": OPTIMISATION_PROFILE_DAYS,
            "profile": empirical["method"],
            "daily_energy_kwh_by_weekday": boiler_daily_energy_forecast(
                device_actuals, device["key"], str(local_tz), horizon[0], empirical["by_weekday"],
            ) if path == "boiler" else {
                weekday: sum(profile) / 4_000
                for weekday, profile in empirical["by_weekday"].items()
            },
            **({"running_power_w": active_power_w} if path == "boiler" else {}),
        }
        if planning_role == "base_load":
            continue
        warnings = []
        if path != "ev" and (active_power_w is None or active_power_w <= 0):
            warnings.append(planning_warning(
                "running_power_unknown", str(device["key"]),
                device["name"] + ": running power has not been observed yet.",
                "Control intent remains in the plan; electrical demand is explicitly uncertain.",
            ))
        if path != "ev" and empirical["method"] != "pooled_shape_weekday_level_v1":
            warnings.append(planning_warning(
                "device_history_estimated", str(device["key"]),
                device["name"] + ": historical demand has incomplete coverage.",
                "Planning keeps the device and uses the available history rather than a measured full profile.",
                evidence={"observations": empirical["sample_count"]},
            ))
        device["planning_warnings"] = warnings
        forecast_w: list[float] = []
        for start in horizon:
            local = start.astimezone(local_tz)
            forecast_w.append(empirical["by_weekday"][local.weekday()][
                local.hour * 4 + local.minute // 15
            ])
        device_models.append({
            **device,
            "load_type": device.get(
                "load_type", device["suggested_load_type"]
            ),
            "planning_role": "controllable",
            "control_type": control_type,
            "forecast_w_by_slot": forecast_w,
            **({"planning_service": "pool"} if path == "pool" else {}),
        })
    if device_gaps:
        raise OptimisationInputError(*device_gaps, remedy=REMEDY_DEFECT)
    return device_models, degraded


def build_operating_scope(options, devices, device_models, device_actuals, horizon):
    """Freeze external electrical demand before any hypothetical thermal enrichment.

    The last completed meter quarter conditions only the current quarter. The
    subsequent empirical forecast remains an expectation, not a claim that the
    observed running state persists through the planning horizon.
    """
    from .operating_modes import operating_mode_identity, system_member_keys
    from math import isfinite
    owners = system_member_keys(devices, options)
    model_owners = {model["key"]: "$" + owners[model["key"]] if model["key"] in owners else model["key"]
                    for model in device_models}
    modes = operating_mode_identity(options, model_owners.values())
    previous_start = horizon[0] - timedelta(minutes=15)
    recent = next((row.get("device_energy_kwh", {}) for row in device_actuals
                   if datetime.fromisoformat(row["start"]) == previous_start), {})
    external = {}
    for model in device_models:
        key = model["key"]
        if modes[model_owners[key]] == "controlling":
            continue
        energy = recent.get(key)
        observation = None
        if isinstance(energy, (int, float)) and not isinstance(energy, bool) and isfinite(energy) and 0 <= energy <= 25:
            observation = {"start": previous_start.isoformat(), "end": horizon[0].isoformat(),
                           "average_w": energy * 4000, "source": "completed_meter_quarter"}
        external[key] = {"forecast_w_by_slot": list(model["forecast_w_by_slot"]),
                         "recent_observation": observation}
    return {"modes": modes, "device_owners": model_owners, "external_demands": external}
