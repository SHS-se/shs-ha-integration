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

try:  # pragma: no cover - exercised by both import paths
    from .const import (
        EV_CHARGE_EFFICIENCY,
        EV_MIN_RUN_SLOTS,
        EV_PHASE_COUNT,
        EV_PHASE_VOLTAGE,
        OPT_DEVICE_CONTROL_MAPPINGS,
        OPT_EV_CONNECTED_ENTITY,
        OPT_EV_DEPARTURE_ENTITY,
        OPT_EV_ENERGY_REMAINING_ENTITY,
        OPT_EV_SOC_ENTITY,
        OPT_EV_TARGET_SOC_ENTITY,
        OPT_POOL_WATER_TEMPERATURE_ENTITY,
    )
    from .const import OPTIMISATION_PROFILE_DAYS
    from .device_controls import CONTROL_TYPES, planning_path
    from .optimisation import (
        OptimisationInputError,
        build_device_load_model,
        daily_requirement,
        discrete_current_control,
        normalized_fraction,
        parse_number,
        service_daily_energy,
        service_energy_today,
        state_is_on,
        validate_service_windows,
    )
except ImportError:  # The test suite imports these helpers as flat modules,
    # without Home Assistant installed, so the package parent does not exist.
    from const import (  # type: ignore[no-redef]
        EV_CHARGE_EFFICIENCY,
        EV_MIN_RUN_SLOTS,
        EV_PHASE_COUNT,
        EV_PHASE_VOLTAGE,
        OPT_DEVICE_CONTROL_MAPPINGS,
        OPT_EV_CONNECTED_ENTITY,
        OPT_EV_DEPARTURE_ENTITY,
        OPT_EV_ENERGY_REMAINING_ENTITY,
        OPT_EV_SOC_ENTITY,
        OPT_EV_TARGET_SOC_ENTITY,
        OPT_POOL_WATER_TEMPERATURE_ENTITY,
    )
    from const import OPTIMISATION_PROFILE_DAYS  # type: ignore[no-redef]
    from device_controls import (  # type: ignore[no-redef]
        CONTROL_TYPES,
        planning_path,
    )
    from optimisation import (  # type: ignore[no-redef]
        OptimisationInputError,
        build_device_load_model,
        daily_requirement,
        discrete_current_control,
        normalized_fraction,
        parse_number,
        service_daily_energy,
        service_energy_today,
        state_is_on,
        validate_service_windows,
    )

# Reads one entity as ``{"state": str, "attributes": dict}``, raising
# OptimisationInputError when it is missing or unavailable.
EntityReader = Callable[[str], dict[str, Any]]
# Resolves one control mapping's reviewed watts, or its live power sensor.
PowerReader = Callable[[dict[str, Any]], Optional[float]]


def build_services(
    options: dict[str, Any],
    daily_changes: dict[str, dict[str, float]],
    device_actuals: list[dict[str, Any]],
    horizon: list[datetime],
    device_models: list[dict[str, Any]],
    *,
    read_entity: EntityReader,
    local_tz: tzinfo,
    today: date,
) -> tuple[list[dict[str, Any]], dict[str, int], dict[str, Any] | None]:
    """Build the deferrable services, their sample counts and EV battery state."""
    first = horizon[0]
    end = horizon[-1] + timedelta(minutes=15)

    services: list[dict[str, Any]] = []
    samples: dict[str, int] = {}
    ev_battery: dict[str, Any] | None = None
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
        pairs: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for model in device_models:
            if planning_path(model["control_type"], model["category"]) != path:
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

    def measured_daily_kwh(
        controls: list[tuple[dict[str, Any], dict[str, Any]]],
    ) -> dict[str, float]:
        return service_daily_energy(
            daily_changes,
            (str(model["statistic_id"]) for model, _mapping in controls),
        )

    def completed_today_kwh(
        controls: list[tuple[dict[str, Any], dict[str, Any]]],
    ) -> float:
        return service_energy_today(
            device_actuals,
            (str(model["key"]) for model, _mapping in controls),
            today,
            local_tz,
        )

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
            parse_number(model.get("active_power_w"), f"{model['name']} power")
            for model, _mapping in pool_controls
        )
        minimum_run = 1
        for model, mapping in pool_controls:
            value = mapping.get("min_run_slots")
            if value in (None, ""):
                continue
            parsed = parse_number(value, f"{model['name']} minimum run")
            if not parsed.is_integer() or parsed < 1:
                raise OptimisationInputError(
                    f"{model['name']} minimum run must be a positive whole number"
                )
            minimum_run = max(minimum_run, int(parsed))
        requirement, count = daily_requirement(
            measured_daily_kwh(pool_controls), category
        )
        samples[category] = count
        completed_today = completed_today_kwh(pool_controls)
        by_day: dict[date, list[int]] = {}
        for index, slot in enumerate(horizon):
            by_day.setdefault(slot.astimezone(local_tz).date(), []).append(index)
        for day, indices in sorted(by_day.items()):
            day_end = datetime.combine(
                day + timedelta(days=1),
                datetime.min.time(),
                tzinfo=local_tz,
            ).astimezone(timezone.utc)
            if day_end > end:
                continue
            required = requirement
            if day == today:
                required = max(0.0, requirement - completed_today)
            if required <= 0:
                continue
            earliest = horizon[indices[0]]
            deadline = day_end
            active_indices = [
                index
                for index in indices
                if sum(
                    float(model["forecast_w_by_slot"][index])
                    for model, _mapping in pool_controls
                ) > 0
            ]
            baseline = horizon[active_indices[0]] if active_indices else earliest
            services.append({
                "id": f"{device}:{day.isoformat()}",
                "device": device,
                "earliest_start": earliest.isoformat(),
                "deadline": deadline.isoformat(),
                "required_kwh": round(required, 3),
                "control": {
                    "type": "fixed_power",
                    "power_w": rated_power_w,
                },
                "min_run_slots": minimum_run,
                "priority": 2,
                "baseline_preferred_start": baseline.isoformat(),
            })

    boiler_controls = mapped_controls("boiler")
    if boiler_controls:
        boiler_models = [model for model, _mapping in boiler_controls]
        expected_w = [
            round(sum(model["forecast_w_by_slot"][index]
                      for model in boiler_models), 2)
            for index in range(len(horizon))
        ]
        rated_power_w = sum(
            parse_number(model.get("active_power_w"), f"{model['name']} power")
            for model in boiler_models
        )
        if max(expected_w, default=0) > rated_power_w + 1e-6:
            raise OptimisationInputError(
                "empirical water-heater expected power exceeds its reviewed rating"
            )
        inhibit_values: list[int] = []
        for model, mapping in boiler_controls:
            maximum_inhibit = parse_number(
                mapping.get("max_inhibit_slots"),
                f"{model['name']} maximum inhibit",
            )
            if not maximum_inhibit.is_integer() or maximum_inhibit < 1:
                raise OptimisationInputError(
                    f"{model['name']} maximum inhibit must be a positive whole number"
                )
            inhibit_values.append(int(maximum_inhibit))
        maximum_inhibit = min(inhibit_values)
        samples["hot_water"] = sum(
            int(model["profile_sample_count"]) for model in boiler_models
        )
        by_day: dict[date, list[int]] = {}
        for index, slot in enumerate(horizon):
            by_day.setdefault(slot.astimezone(local_tz).date(), []).append(index)
        for day, indices in sorted(by_day.items()):
            required_kwh = sum(expected_w[index] for index in indices) / 4_000
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
                    "max_consecutive_inhibit_slots": maximum_inhibit,
                },
                "priority": 1,
            })

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
        entity_min_raw = current_payload["attributes"].get("min")
        entity_max_raw = current_payload["attributes"].get("max")
        if entity_min_raw is not None:
            entity_min = parse_number(entity_min_raw, f"{current_entity} min")
            if configured_min < entity_min:
                raise OptimisationInputError(
                    "configured EV current minimum is below the entity bound"
                )
        if entity_max_raw is not None:
            entity_max = parse_number(entity_max_raw, f"{current_entity} max")
            if configured_max > entity_max:
                raise OptimisationInputError(
                    "configured EV current maximum is above the entity bound"
                )
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
    if options.get(OPT_EV_CONNECTED_ENTITY):
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
            "Vehicle target SOC entity",
        )
        soc_payload = read_entity(soc_id)
        soc = normalized_fraction(soc_payload["state"], OPT_EV_SOC_ENTITY)
        target = normalized_fraction(
            read_entity(target_id)["state"], OPT_EV_TARGET_SOC_ENTITY
        )
        remaining_entity = required_entity(
            OPT_EV_ENERGY_REMAINING_ENTITY,
            "Vehicle usable-energy-remaining entity",
        )
        remaining = parse_number(
            read_entity(remaining_entity)["state"], remaining_entity
        )
        if soc <= 0:
            raise OptimisationInputError(
                "vehicle SOC must be above zero to derive usable battery capacity"
            )
        if remaining <= 0:
            raise OptimisationInputError(
                f"{remaining_entity} must report positive usable energy"
            )
        capacity = remaining / soc

        departure: datetime | None = None
        departure_entity = options.get(OPT_EV_DEPARTURE_ENTITY)
        if connected and isinstance(departure_entity, str) and departure_entity:
            departure_raw = read_entity(departure_entity)["state"]
            try:
                departure = datetime.fromisoformat(
                    str(departure_raw).replace("Z", "+00:00")
                )
            except ValueError as err:
                raise OptimisationInputError(
                    f"{OPT_EV_DEPARTURE_ENTITY} must contain an ISO timestamp"
                ) from err
            if departure.tzinfo is None:
                raise OptimisationInputError(
                    f"{OPT_EV_DEPARTURE_ENTITY} timestamp must include a timezone"
                )
            departure = departure.astimezone(timezone.utc)
            if departure <= first or departure > end:
                raise OptimisationInputError(
                    "connected EV departure must fall inside the 72-hour horizon"
                )

        ev_battery = {
            "name": str(
                soc_payload["attributes"].get("friendly_name") or "EV battery"
            ),
            "connected": connected,
            "capacity_kwh": round(capacity, 3),
            "soc": round(soc, 6),
            "departure_target_soc": round(target, 6),
            "charge_efficiency": EV_CHARGE_EFFICIENCY,
            "available_from": first.isoformat() if connected else None,
            "departure": departure.isoformat() if departure else None,
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
            max(0.0, target - soc) * capacity / EV_CHARGE_EFFICIENCY
            if connected and ev_controls else 0.0
        )
        if required > 0:
            planning_deadline = departure or end
            control = discrete_current_control(
                configured_min,
                configured_max,
                configured_step,
                EV_PHASE_COUNT,
                EV_PHASE_VOLTAGE,
                label=current_entity,
            )
            services.append({
                "id": f"ev:{planning_deadline.isoformat()}",
                "device": "ev",
                "earliest_start": first.isoformat(),
                "deadline": planning_deadline.isoformat(),
                "required_kwh": round(required, 3),
                "control": control,
                "min_run_slots": EV_MIN_RUN_SLOTS,
                "priority": 3,
                "baseline_preferred_start": first.isoformat(),
            })
    validate_service_windows(services, horizon)
    return services, samples, ev_battery


# Telemetry that proves a home owns a service, against the planning path that
# has to exist before the planner can act on it.
_SERVICE_EVIDENCE: tuple[tuple[str, str, tuple[str, ...], str], ...] = (
    (
        "ev",
        "a vehicle",
        (
            OPT_EV_CONNECTED_ENTITY,
            OPT_EV_SOC_ENTITY,
            OPT_EV_TARGET_SOC_ENTITY,
            OPT_EV_ENERGY_REMAINING_ENTITY,
        ),
        "set the EV charging meter to variable-power control on the website",
    ),
    (
        "pool",
        "a pool",
        (OPT_POOL_WATER_TEMPERATURE_ENTITY,),
        "set the pool heating meter to switch-schedule control on the website",
    ),
)


def unplanned_services(
    options: dict[str, Any],
    planned_paths: set[str | None],
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
    """
    reports: list[str] = []
    for path, subject, option_keys, remedy in _SERVICE_EVIDENCE:
        if path in planned_paths:
            continue
        configured = sorted(
            key for key in option_keys
            if isinstance(options.get(key), str) and options[key].strip()
        )
        if not configured:
            continue
        reports.append(
            f"{subject} is configured ({', '.join(configured)}) but no meter "
            f"routes to the {path} planner, so it is not being planned; "
            f"{remedy}"
        )
    return reports


def build_device_models(
    devices: list[dict[str, Any]],
    device_actuals: list[dict[str, Any]],
    horizon: list[datetime],
    control_mappings: dict[str, Any],
    *,
    mapped_power_w: PowerReader,
    local_tz: tzinfo,
) -> list[dict[str, Any]]:
    """Return the controllable models, learning each device's recent profile.

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
        try:
            empirical = build_device_load_model(
                device_actuals,
                device["key"],
                str(local_tz),
                minimum_samples=2,
            )
        except OptimisationInputError:
            if planning_role == "controllable":
                device_gaps.append(
                    f"{device['name']} needs a complete empirical profile "
                    "before it can be controllable"
                )
            continue
        empirical_active_power_w = empirical["active_power_w"]
        mapping = control_mappings.get(str(device["key"]), {})
        # Named apart from the `mapped_power_w` reader deliberately: binding the
        # result to the reader's own name works for the first device and then
        # calls a float on the second.
        reviewed_power_w = mapped_power_w(
            mapping if isinstance(mapping, dict) else {}
        )
        active_power_w = (
            round(reviewed_power_w, 1)
            if reviewed_power_w is not None
            else empirical_active_power_w
        )
        device["active_power_w"] = active_power_w
        device["profile_sample_count"] = int(empirical["sample_count"])
        device["inference"] = {
            **device["inference"],
            "history_days": OPTIMISATION_PROFILE_DAYS,
            "profile": empirical["method"],
        }
        if planning_role == "base_load":
            continue
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
        })
    if device_gaps:
        raise OptimisationInputError(*device_gaps)
    return device_models
