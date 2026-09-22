"""One local command owner for binding system and per-device schedules.

The journal is saved *before* taking ownership. Restarts and mapping edits restore
using the old mapping, never through newly selected entities. Service completion
and physical confirmation are deliberately separate statuses.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timezone, timedelta
import logging
from math import isfinite
from typing import Any
from types import SimpleNamespace
from time import perf_counter

try:
    from .api_contract import INTEGRATION_VERSION
    from .operating_modes import device_mode, EXECUTING_MODES
    from .verification import OPERATIONS, operation_name, evaluation_record, observation
    from .controller_metrics import ControllerMetrics, fingerprint, record_time
    from .battery_commands import validate_battery_command, battery_mode_key
    from .configuration_values import resolve_battery_quantities, resolve_quantity
    from .device_commands import actuator_targets, execution_setup_errors, validate_commands
    from .device_controls import battery_control_errors, pool_control_errors, pool_control_mapping, mapped_planning_path, planning_path
except ImportError:  # Pure executor tests, without importing Home Assistant.
    from api_contract import INTEGRATION_VERSION
    from operating_modes import device_mode, EXECUTING_MODES
    from verification import OPERATIONS, operation_name, evaluation_record, observation
    from controller_metrics import ControllerMetrics, fingerprint, record_time
    from battery_commands import validate_battery_command, battery_mode_key
    from configuration_values import resolve_battery_quantities, resolve_quantity
    from device_commands import actuator_targets, execution_setup_errors, validate_commands
    from device_controls import battery_control_errors, pool_control_errors, pool_control_mapping, mapped_planning_path, planning_path

_LOGGER = logging.getLogger(__name__)
DEVICES = ("battery", "ev", "pool")
CONFIRM_SECONDS = 15
# Battery feedback gates power commands; pool and EV telemetry may report more slowly.
BATTERY_MAX_AGE_SECONDS = 120
POOL_MAX_AGE_SECONDS = 15 * 60
EV_MAX_AGE_SECONDS = 15 * 60
# Restarts and reconnects hide an entity for about a minute; longer needs attention.
OBSERVATION_GRACE_SECONDS = 5 * 60
EXTERNAL_CHANGE = "Actuator changed outside SHS; set this device to Verification and back to Controlling to resume"
INHIBIT_LIMIT = "Paused for its maximum of {:g} quarters, so it may run for a quarter before the plan can pause it again"


class ControlObservationError(ValueError):
    """An unusable observation with a concrete inspection destination."""
    def __init__(self, message, entity, next_step, *, unavailable=False, stale=False):
        super().__init__(message)
        self.entity = entity
        self.unavailable = unavailable
        self.stale = stale
        self.details = {"next_step": next_step,
                        "fix": {"kind": "entity", "entity_id": entity} if entity else {"kind": "device"}}

    @property
    def transient(self):
        """A reading interrupted by a restart or reconnect, which returns by itself."""
        return self.unavailable or self.stale


class ActuatorUnavailableError(ControlObservationError):
    """The entity SHS writes cannot be reached, so nothing can be sent or handed back yet."""


class PlanChangedError(ValueError):
    """A superseded execution snapshot, with evidence for the exact stop reason."""
    def __init__(self, code, message, context):
        super().__init__(f"plan changed or expired during execution: {message}")
        self.details = {"blocked_reason": code, "blocked_context": context}


class ControlDeadlineError(ValueError):
    """The next eligible transition already has an explicit wake-up deadline."""


def correction_details(error):
    return error.details if isinstance(error, (ControlObservationError, PlanChangedError)) else {}


def finite(value: Any) -> float:
    if isinstance(value, bool):
        raise ValueError("boolean is not a numeric command")
    result = float(value)
    if not isfinite(result):
        raise ValueError("non-finite value")
    return result


def checked_state(state, entity, max_age=None):
    if state is None or state.state in ("unknown", "unavailable"):
        raise ControlObservationError(
            f"{entity or 'required entity'} is unavailable", entity,
            "Check the source entity and its integration for an unavailable reading. "
            "If the entity was replaced, select its replacement in this device's setup.",
            unavailable=True,
        )
    if max_age is not None:
        reported = getattr(state, "last_reported", state.last_updated)
        age = (datetime.now(timezone.utc) - reported).total_seconds()
        if not 0 <= age <= max_age:
            raise ControlObservationError(
                f"{entity} is stale (last reported {reported.isoformat()}; maximum age {max_age} seconds)",
                entity,
                f"Check that this sensor and its source integration report at least every {max_age} seconds, "
                "including while its value is unchanged.",
                stale=True,
            )
    return state


def battery_limit_value(entity, watts, state):
    if state is None:
        raise ValueError(f"{entity} is unavailable")
    unit = state.attributes.get("unit_of_measurement")
    if not entity.startswith("number.") or unit not in ("W", "kW"):
        raise ValueError(f"{entity} must be a W or kW limit control")
    low, high = finite(state.attributes["min"]), finite(state.attributes["max"])
    step = finite(state.attributes.get("step", 1))
    if low != 0 or high <= 0 or step <= 0:
        raise ValueError(f"{entity} must support non-negative limits including zero")
    value = finite(watts) / (1000 if unit == "kW" else 1)
    if not 0 <= value <= high:
        raise ValueError(f"{entity}: requested ceiling is outside hardware bounds")
    return int(value / step + 1e-8) * step


class ScheduledController:
    """HA adapter supplied by the caller, so execution is behaviour-testable."""

    def __init__(self, hass, coordinator, store, options, verification=None, *, entity_registry=None):
        self.verification = verification
        self.entity_registry = entity_registry
        self.verifying = False
        self.verification_commands = []
        self.shadow = {}
        self.verification_observations = {}
        self.hass = hass
        self.coordinator = coordinator
        self.store = store
        self.options = options
        self.records: dict[str, dict] = {}
        self.status = {device: {"state": "disabled"} for device in DEVICES}
        self.listeners = set()
        self.lock = asyncio.Lock()
        self.battery_writer_fence = None
        self.closed = False
        self.restoring = False
        self.active_options = None
        self.active_slot = None
        self.active_plan_id = None
        self.failed = {}
        self.initialized = False
        self.command_times = {}
        self.overrides = {}
        self.requested_types = {}
        self.requested_systems = set()
        self.requested_error = None
        self.metrics = ControllerMetrics(INTEGRATION_VERSION)
        self.scheduler = None
        self.observation_changed = asyncio.Event()
        self.diagnostic_evaluation = None
        self.diagnostics_error = None
        self.diagnostics_failed_evaluations = 0
        self.retired_pool_temperature_settings = {}
        self.diagnostics_sampling_error = None
        self.diagnostics_failed_samples = 0

    def add_listener(self, listener, reports=None):
        """`reports` selects the status keys a listener shows; None receives all."""
        subscription = (listener, reports)
        self.listeners.add(subscription)
        return lambda: self.listeners.discard(subscription)

    def report(self, device, state, **details):
        if self.verifying:
            return
        if device == "pool" and self.retired_pool_temperature_settings:
            details["retired_temperature_settings"] = deepcopy(self.retired_pool_temperature_settings)
            details["control_notice"] = "Old temperature control has been removed. Check the heater's own temperature settings; SHS now only uses its switch."
        value = {"state": state, **details}
        if self.status.get(device) == value:
            return
        self.status[device] = value
        _LOGGER.info("Scheduled %s: %s", device, value)
        for listener, reports in tuple(self.listeners):
            if reports is None or reports(device):
                listener()

    def publish_battery_status(self):
        """Mirror the battery owner's own status without evaluating other devices."""
        runtime = getattr(self, "battery_runtime", None)
        if runtime is None or self.closed or not self.initialized:
            return
        status = runtime.snapshot()
        self.report("battery", status["state"], reason=status["reason"], battery_runtime=status,
                    **{key: status[key] for key in ("fix", "next_step", "retry_automatically") if key in status})

    def observed_state(self, entity, *, max_age=None):
        state = self.hass.states.get(entity) if entity else None
        if self.diagnostic_evaluation is not None and not self.verifying and entity:
            value = observation(state)
            self.diagnostic_evaluation["observations"].setdefault(entity, value)
            self.diagnostic_evaluation.setdefault("final_observations", {})[entity] = value
        self.metrics.observe(entity, state)
        if self.scheduler is not None:
            self.scheduler.observe(self.device, entity, state, max_age)
        return state

    def state(self, entity, *, max_age=None):
        state = (self.shadow.get(entity) if self.verifying else None) or self.observed_state(entity, max_age=max_age)
        if self.verifying and entity not in self.shadow:
            self.verification_observations[entity] = observation(state)
        return checked_state(state, entity, max_age)

    def actuator_state(self, entity):
        try:
            return self.state(entity)
        except ControlObservationError as error:
            if not error.unavailable:
                raise
            raise ActuatorUnavailableError(str(error), entity, error.details["next_step"], unavailable=True) from None

    def number(self, entity, *, max_age=None):
        return finite(self.state(entity, max_age=max_age).state)

    def watts(self, entity):
        state = self.state(entity, max_age=BATTERY_MAX_AGE_SECONDS)
        unit = state.attributes.get("unit_of_measurement")
        if unit not in ("W", "kW"):
            raise ValueError(f"{entity} must report W or kW")
        return finite(state.state) * (1000 if unit == "kW" else 1)

    def fraction(self, entity, *, max_age=None):
        state = self.state(entity, max_age=max_age)
        value = finite(state.state)
        if state.attributes.get("unit_of_measurement") == "%":
            value /= 100
        if not 0 <= value <= 1:
            raise ValueError(f"{entity} must report SOC as % or a fraction")
        return value

    def inactive_status(self, device, options):
        """An explicit setting that takes this device out of SHS control, or None.

        These are the only reasons SHS hands a device back: its execution-mode
        select, the website or local choices that remove it from that select
        (demotion, exclusion, a disabled system), a manual override the user
        configured for the purpose, and an external change that stays latched
        until the select leaves Controlling. An override entity that cannot be
        read raises instead, so it holds rather than releases.
        """
        mode = device_mode(options, device)
        if device in self.overrides:
            return {"state": "overridden", "reason": self.overrides[device]}
        if mode not in EXECUTING_MODES:
            return {"state": mode, "reason": "Observing only; no SHS commands" if mode == "monitoring" else
                    "Planning only; no SHS commands or command verification"}
        known = self.requested_error is None
        if device.startswith("device:"):
            key = device.removeprefix("device:")
            mapping = options.get("device_control_mappings", {}).get(key, {})
            if key in options.get("excluded_device_readings", []):
                return {"state": "disabled", "reason": "Device readings are explicitly excluded"}
            if known and self.requested_types.get(key) != mapping.get("control_type"):
                return {"state": "disabled", "reason": "Device mapping does not match the requested control method"}
            override = mapping.get("control_override_entity")
        else:
            if "$" + device in options.get("excluded_device_readings", []):
                return {"state": "disabled", "reason": "Device is excluded from SHS"}
            if known and device not in self.requested_systems:
                return {"state": "disabled", "reason": "Device is not included in the household plan"}
            if not options.get(f"{device}_enabled", True):
                return {"state": "disabled", "reason": "Device is disabled in SHS configuration"}
            override = options.get(f"{device}_control_override_entity")
        if override and self.state(override).state != "off":
            return {"state": "overridden", "reason": "Configured manual override is active"}
        return None

    def hold_reason(self, device, options):
        """Website choices that cannot be read hold the device; they never release it."""
        if self.requested_error is not None:
            return "The website's current choices could not be read"
        return None

    def eligible(self, device, options):
        return self.inactive_status(device, options) is None and self.hold_reason(device, options) is None

    def eligible_now(self, device, options):
        try:
            return self.eligible(device, options)
        except ValueError:
            return False

    def holding(self, device, reason):
        """Say that the device keeps the last setting SHS sent, when there is one."""
        return f"{reason}; holding the last setting SHS sent" if device in self.records else reason

    def check_authority(self):
        if self.restoring:
            return
        options = self.options()
        if options != self.active_options or self.closed:
            raise ValueError("configuration changed during execution")
        plan, slot = self.coordinator.binding_plan_for(self.device, options)
        if slot != self.active_slot:
            now = datetime.now(timezone.utc)
            old_start = self.active_slot["start"]
            old_end = datetime.fromisoformat(old_start.replace("Z", "+00:00")) + timedelta(minutes=15)
            current_start = slot["start"] if slot else None
            status = self.coordinator.operational_status
            context = {
                "original_plan_id": self.active_plan_id,
                "current_plan_id": (self.coordinator.optimisation_plan or {}).get("plan_id"),
                "original_slot_start": old_start, "original_slot_end": old_end.isoformat(),
                "current_slot_start": current_start,
                "original_slot_expired": now >= old_end,
                "plan_status": {key: status.get(key) for key in ("state", "reason", "actionable")},
                "changed_slot_fields": sorted(key for key in self.active_slot.keys() | slot.keys()
                                               if self.active_slot.get(key) != slot.get(key)) if slot else [],
            }
            if slot is not None and current_start != old_start:
                code = "slot_rollover" if now >= old_end else "slot_replaced"
                message = f"active slot moved from {old_start} to {current_start}"
            elif slot is not None:
                code, message = "slot_revised", "instructions for the same slot were revised"
            elif not status["actionable"]:
                code, message = "plan_not_actionable", status["reason"]
            elif now >= old_end:
                code, message = "slot_expired", "original slot ended and no current binding slot is available"
            else:
                code, message = "no_binding_slot", "no current binding slot is available"
            raise PlanChangedError(code, message, context)
        if not self.eligible(self.device, options):
            raise ValueError("control disabled or manually overridden")

    async def command(self, entity, value):
        if self.battery_writer_fence is not None and not self.verifying:
            self.battery_writer_fence.check_legacy(entity)
        self.check_authority()
        domain = entity.split(".")[0]
        state = ((self.shadow.get(entity) if self.verifying else None) or self.observed_state(entity)) if getattr(self, "device", None) == "battery" and domain == "number" else self.actuator_state(entity)
        if state is None:
            raise ValueError(f"{entity} is unavailable")
        if domain in ("number", "input_number"):
            value = finite(value)
            low = finite(state.attributes["min"])
            high = finite(state.attributes["max"])
            step = finite(state.attributes.get("step", 1))
            if not low <= value <= high or step <= 0:
                raise ValueError(f"{entity}: target outside hardware bounds")
            if abs((value-low)/step - round((value-low)/step)) > 1e-5:
                raise ValueError(f"{entity}: target is not a supported step")
            service, data = "set_value", {"value": value}
            try:
                equal = abs(finite(state.state) - value) < 1e-6
            except (TypeError, ValueError):
                equal = False
        elif domain == "climate":
            value = finite(value)
            low, high = finite(state.attributes["min_temp"]), finite(state.attributes["max_temp"])
            if state.state != "heat" or self.hass.config.units.temperature_unit != "°C":
                raise ValueError(f"{entity}: setpoint execution requires an active Celsius heating thermostat")
            if not low <= value <= high:
                raise ValueError(f"{entity}: target outside hardware bounds")
            service, data = "set_temperature", {"temperature": value}
            equal = abs(finite(state.attributes["temperature"]) - value) < 1e-6
        elif domain in ("select", "input_select"):
            if value not in state.attributes.get("options", []):
                raise ValueError(f"{entity}: unsupported mode {value}")
            service, data = "select_option", {"option": value}
            equal = state.state == value
        elif domain in ("switch", "input_boolean"):
            if value not in ("on", "off"):
                raise ValueError(f"{entity}: invalid switch state")
            service, data = f"turn_{value}", {}
            equal = state.state == value
        else:
            raise ValueError(f"{entity}: unsupported actuator domain")
        command = {"at": datetime.now(timezone.utc).isoformat(), "phase": "handover" if self.restoring else "plan",
                   "domain": domain, "service": service, "data": {"entity_id": entity, **data},
                   "value": value, "observed_value": state.state}
        if self.verifying:
            self.verification_commands.append({**command,
                "trigger": "if control stopped now" if self.restoring else "current binding slot",
                "would_call": not equal})
            attributes = dict(state.attributes)
            if domain == "climate":
                attributes["temperature"] = value
            self.shadow[entity] = SimpleNamespace(state=state.state if domain == "climate" else str(value), attributes=attributes)
            record = self.records.get(self.device)
            if record is not None and not self.restoring:
                record.setdefault("last_commands", {})[entity] = value
            return
        command.update(called=False, transport="not_sent", settings_confirmation="not_checked")
        if self.diagnostic_evaluation is not None:
            self.diagnostic_evaluation["commands"].append(command)
        try:
            record = self.records.get(getattr(self, "device", ""))
            if record is not None and not self.restoring:
                record.setdefault("last_commands", {})[entity] = value
                await self.save()
            self.check_authority()
            if not equal:
                if self.battery_writer_fence is not None:
                    self.battery_writer_fence.check_legacy(entity)
                runtime=getattr(self,"battery_runtime",None)
                if runtime is not None and self.device!="battery" and not runtime.before_external_command(self.device):
                    if self.scheduler is not None:
                        self.scheduler.device_deadline(self.device,"battery_headroom",datetime.now(timezone.utc)+timedelta(seconds=5))
                    raise ControlDeadlineError("Waiting for battery charging to release grid capacity")
                self.command_times[entity] = datetime.now(timezone.utc)
                command.update(called=True, transport="ambiguous")
                await asyncio.wait_for(
                    self.hass.services.async_call(
                        domain, service, {"entity_id": entity, **data}, blocking=True,
                    ), timeout=CONFIRM_SECONDS,
                )
                command["transport"] = "accepted"
            # Service completion records an accepted setting. Ordinary readings
            # and explicit device workflows assess the physical response.
        except (Exception, asyncio.CancelledError) as err:
            command["error"] = str(err) or type(err).__name__
            raise
        finally:
            command["completed_at"] = datetime.now(timezone.utc).isoformat()

    def matches(self, entity, value):
        state = (self.shadow.get(entity) if self.verifying else None) or self.observed_state(entity)
        if state is None or state.state in ("unknown", "unavailable"):
            return False
        if isinstance(value, (int, float)):
            measured = state.attributes["temperature"] if entity.startswith("climate.") else state.state
            try:
                return abs(finite(measured) - value) < 1e-6
            except (TypeError, ValueError):
                return False
        return state.state == value

    async def confirm(self, predicate, error):
        if self.verifying:
            return
        deadline = asyncio.get_running_loop().time() + CONFIRM_SECONDS
        while True:
            self.observation_changed.clear()
            self.check_authority()
            if predicate():
                return
            try:
                await asyncio.wait_for(self.observation_changed.wait(),
                                       timeout=max(0, deadline - asyncio.get_running_loop().time()))
            except TimeoutError:
                self.check_authority()
                if predicate():
                    return
                raise ValueError(error) from None

    def device_deadline(self, name, when):
        if self.scheduler is not None and not self.verifying:
            self.scheduler.device_deadline(self.device, name, when)

    def measurement_after_commands(self, entity, *actuators):
        state = self.state(entity, max_age=BATTERY_MAX_AGE_SECONDS)
        reported = getattr(state, "last_reported", state.last_updated)
        return all(reported >= self.command_times[actuator]
                   for actuator in actuators if actuator in self.command_times)

    async def save(self):
        if self.verifying:
            return
        await self.store.async_save({"records": self.records, "overrides": self.overrides,
                                     "retired_pool_temperature_settings": self.retired_pool_temperature_settings})

    async def capture(self, device, options, entities):
        if device in self.records:
            return self.records[device]
        originals = {entity: (self.state(entity).attributes["temperature"] if entity.startswith("climate.")
                              else self.state(entity).state) for entity in entities if entity}
        record = {"options": deepcopy(options), "originals": originals}
        self.records[device] = record
        await self.save()
        return record

    async def restore(self, device):
        self.device = device
        record = self.records.get(device)
        if record is None:
            return
        options, original = record["options"], record["originals"]
        record["restoration_pending"] = True
        await self.save()
        self.restoring = True
        try:
            if device.startswith("device:"):
                changed = set(record.get("externally_changed", []))
                for entity, expected in record.get("last_commands", {}).items():
                    if self.verifying:
                        continue
                    # Unavailable is unknown, never changed: the handover waits for it.
                    self.actuator_state(entity)
                    if not self.matches(entity, expected) and not self.matches(entity, original[entity]):
                        changed.add(entity)
                if changed:
                    record["externally_changed"] = sorted(changed)
                    self.overrides[device] = EXTERNAL_CHANGE
                    await self.save()
                mapping = options["device_control_mappings"][device.removeprefix("device:")]
                if mapping["control_type"] == "switch_schedule":
                    now = datetime.now(timezone.utc)
                    for entity, value in original.items():
                        at = record.get("transition_times", {}).get(entity)
                        last = record.get("last_commands", {}).get(entity)
                        if at and last != value and entity not in record.get("externally_changed", []):
                            minimum = mapping.get("minimum_on_seconds" if last == "on" else "minimum_off_seconds")
                            if minimum is not None and (now - datetime.fromisoformat(at)).total_seconds() < minimum:
                                self.device_deadline("minimum_run:" + entity,
                                                     datetime.fromisoformat(at) + timedelta(seconds=minimum))
                                raise ControlDeadlineError("restoration waiting for minimum relay run time")
                for entity, value in original.items():
                    if entity not in record.get("externally_changed", []):
                        await self.command(entity, value)
            elif device == "battery":
                charge = options.get("battery_charge_limit_entity")
                discharge = options.get("battery_discharge_limit_entity")
                normal_w = {direction: resolve_quantity(
                    options[f"battery_{direction}_max_w"], self.battery_quantity,
                    unit="W", minimum=0, maximum=100000, label=f"rated battery {direction} power",
                ) for direction in ("charge", "discharge")}
                targets = [(entity, self.battery_limit(entity, normal_w[direction]))
                           for direction, entity in (("charge", charge), ("discharge", discharge))]
                await self.command(charge, 0)
                await self.command(discharge, 0)
                await self.command(options["battery_mode_entity"], options["battery_mode_baseline"])
                for entity, value in targets:
                    await self.command(entity, value)
                await self.confirm(
                    lambda: self.battery_within_ceilings(options, normal_w["charge"], normal_w["discharge"],
                        options["battery_mode_entity"], charge, discharge),
                    "no fresh battery response within normal ceilings after baseline restoration",
                )
                measured = self.battery_measurement(options)
                self.report(device, "baseline", measured_power_w=measured,
                            reason="baseline mode and normal limits confirmed; power is measured")
            elif device == "pool":
                # Never write temperature registers, even from a pre-upgrade journal.
                if any(entity.split(".")[0] not in ("switch", "input_boolean") for entity in original):
                    self.retired_pool_temperature_settings = {entity: value for entity, value in original.items()
                        if entity.split(".")[0] not in ("switch", "input_boolean")}
                    self.report("pool", "legacy_control_retired",
                        reason="Old temperature control has been removed. Check the heater's own temperature settings; SHS will only use its switch.",
                        retired_temperature_settings={entity: value for entity, value in original.items()
                            if entity.split(".")[0] not in ("switch", "input_boolean")})
                for entity, value in original.items():
                    if entity.split(".")[0] in ("switch", "input_boolean"):
                        await self.command(entity, value)
            else:
                switch = options["ev_charge_switch_entity"]
                await self.command(switch, "off")
                for entity, value in original.items():
                    if entity != switch:
                        await self.command(entity, finite(value))
                await self.command(switch, original[switch])
            del self.records[device]
            await self.save()
        except Exception as err:
            # A failed service may recover without any entity event. Retain the
            # previous retry interval only while real handover remains pending.
            if not isinstance(err, (ControlObservationError, ControlDeadlineError)):
                self.device_deadline("restoration_retry", datetime.now(timezone.utc) + timedelta(seconds=5))
            else:
                self.device_deadline("restoration_retry", None)
            raise
        finally:
            self.restoring = False

    def ev_mapping(self, options):
        plan = self.coordinator.optimisation_plan or {}
        keys = set((self.active_slot or {}).get("device_loads_w", {}))
        models = plan.get("device_models", [])
        # The plan's reviewed device inventory owns category routing.
        routed = {m["key"] for m in models
                  if planning_path(m.get("control_type"), m.get("category")) == "ev"}
        mappings = options.get("device_control_mappings", {})
        candidates = [mappings[key] for key in routed & keys if key in mappings]
        targets = {(m.get("control_entity_id"), m.get("minimum_value"),
                    m.get("maximum_value")) for m in candidates}
        if len(targets) != 1:
            raise ValueError("EV requires one planned, reviewed current control")
        entity, low, high = targets.pop()
        if self.actuator_state(entity).attributes.get("unit_of_measurement") != "A":
            raise ValueError("EV current control must use amperes")
        return entity, finite(low), finite(high)

    async def execute_ev(self, options, slot):
        entity, low, high = self.ev_mapping(options)
        switch = options.get("ev_charge_switch_entity")
        self.actuator_state(switch)
        connected = self.state(options.get("ev_connected_entity"), max_age=EV_MAX_AGE_SECONDS).state
        if connected not in ("on", "off", "true", "false", "connected", "disconnected"):
            raise ValueError("EV connection state is not a supported boolean")
        soc = self.fraction(options.get("ev_soc_entity"), max_age=EV_MAX_AGE_SECONDS)
        target_soc = self.fraction(options.get("ev_target_soc_entity"))
        current = finite(slot["ev_target_current_a"])
        if current and not low <= current <= high:
            raise ValueError("planned EV current exceeds reviewed bounds")
        if not finite(slot["ev_min_current_a"]) <= current <= finite(slot["ev_max_current_a"]):
            raise ValueError("planned EV current exceeds its slot envelope")
        self.check_targets("ev", [entity, switch])
        await self.capture("ev", options, [entity, switch])
        if current == 0 or connected in ("off", "false", "disconnected") or soc >= target_soc:
            reason = ("plan requests charging off" if current == 0 else
                      "vehicle disconnected" if connected in ("off", "false", "disconnected") else
                      "charge target reached")
            await self.command(switch, "off")
            return {"state": "stopped", "requested_current_a": current,
                    "reason": "off slot, disconnected, or charge target reached",
                    "decision": {"kind": "ev", "charging": False, "current_a": 0, "reason": reason}}
        await self.command(entity, current)
        await self.command(switch, "on")
        return {"state": "commanded", "requested_current_a": current,
                "reason": "current and charge switch accepted; delivered power not inferred",
                "decision": {"kind": "ev", "charging": True, "current_a": current}}

    def pool_temperature(self, entity, *, read_state=None):
        """Use the filtered temperature, but require reports from its raw source."""
        reader = read_state or self.state
        selected = entity
        visited = set()
        sources = {}
        temperature = None
        while True:
            if entity in visited:
                raise ControlObservationError(
                    f"{selected}: filter source cycle at {entity}", entity,
                    "Correct the Filter sensor's source so it does not refer back to itself.",
                )
            visited.add(entity)
            state = reader(entity)
            if state.attributes.get("unit_of_measurement") != "°C":
                raise ValueError(f"{entity}: pool control requires Celsius")
            value = finite(state.state)
            if entity == selected:
                temperature = value
            entry = self.entity_registry.async_get(entity) if self.entity_registry is not None else None
            if entry is None or entry.platform != "filter":
                raw = reader(entity, max_age=POOL_MAX_AGE_SECONDS)
                sources[entity] = None
                return temperature, sources, raw.last_reported + timedelta(seconds=POOL_MAX_AGE_SECONDS)
            source = state.attributes.get("entity_id")
            if not isinstance(source, str) or not source.startswith("sensor."):
                raise ControlObservationError(
                    f"{entity}: Filter sensor has no valid temperature source", entity,
                    "Check the source entity configured in this Filter sensor.",
                )
            sources[entity] = source
            entity = source

    def pool_request(self, options, slot, *, read_state=None, plan=None):
        reader = read_state or self.state
        if plan is None:
            plan, _ = self.coordinator.binding_plan_for("pool", options)
        key, mapping = pool_control_mapping(options, plan.get("device_models", []))
        fields = {}
        errors = pool_control_errors(options, mapping, field_errors=fields)
        if errors:
            error = ControlObservationError("; ".join(errors), None, "Complete the highlighted pool settings.")
            error.details["fix"] = {"kind": "fields", "fields": [
                {"key": field, "message": "; ".join(messages),
                 **({"scope": "mapping", "device_key": key} if field == "actuator_entity_ids" else {})}
                for field, messages in fields.items()]}
            raise error
        entity = mapping["actuator_entity_ids"][0]
        if (read_state or self.actuator_state)(entity).state not in ("on", "off"):
            raise ValueError(f"{entity}: pool control must report on or off")
        target = (plan.get("pool") or {}).get("stop_temperature_c")
        if target is None:
            error = ControlObservationError(
                "The plan is missing the pool's Stop at temperature.", None,
                "Refresh the plan after updating the planner. If this continues, check the website's pool preference and download diagnostics.")
            error.details["fix"] = {"kind": "diagnostics"}
            raise error
        target = finite(target)
        water, sources, fresh_until = self.pool_temperature(options.get("pool_water_temperature_entity"), read_state=reader)
        heating = finite(slot["pool_w"]) > 0
        on = heating and water < target
        reason = ("The pool heater is allowed to run as planned." if on else
                  "The water has reached your Stop at temperature; the pool heater is off." if heating else
                  "The plan is pausing pool heating; the pool heater is off.")
        return {"device_key": key, "control_entity": entity, "requested_switch_state": "on" if on else "off",
                "water_temperature_c": water, "stop_temperature_c": target,
                "requested_power_w": slot["pool_w"], "reason": reason,
                "decision": {"kind": "pool", "heating": on, "water_temperature_c": water,
                             "stop_temperature_c": target}}, sources, fresh_until

    async def execute_pool(self, options, slot):
        request, _, _ = self.pool_request(options, slot)
        entity = request["control_entity"]
        # Refuse shared targets before either controller can acquire ownership.
        for key, mapping in options.get("device_control_mappings", {}).items():
            if key == request["device_key"] or key in options.get("excluded_device_readings", []):
                continue
            if device_mode(options, "device:" + key) in EXECUTING_MODES and entity in actuator_targets(mapping):
                raise ValueError("another enabled device shares the pool actuator")
        self.check_targets("pool", [entity])
        await self.capture("pool", options, [entity])
        await self.command(entity, request["requested_switch_state"])
        return {"state": "scheduled", **request}

    def report_gap(self, device, error, purpose=None, slot=None, plan=None):
        """Hold through a restart, reconnect or reading gap; only a long one needs attention.

        Nothing is written or handed back meanwhile, so the device keeps the last
        setting SHS sent. The entity's own state change starts the next
        evaluation, and SHS resumes as soon as it reports; the deadline only
        escalates an entity that stays away.
        """
        now = datetime.now(timezone.utc)
        # Consecutive reports of this gap share its start; any other outcome ends it.
        previous = self.status.get(device, {}).get("unavailable_since")
        since = datetime.fromisoformat(previous) if previous else now
        deadline = since + timedelta(seconds=OBSERVATION_GRACE_SECONDS)
        if purpose is None:
            purpose = "hand it back" if self.records.get(device, {}).get("restoration_pending") else "resume the plan"
        hold = "SHS holds the last setting it sent and will" if device in self.records else "SHS will"
        details = {"retry_automatically": True, "unavailable_since": since.isoformat(),
                   **({"plan_id": plan.get("plan_id"), "slot_start": slot["start"]} if slot and plan else {})}
        if now < deadline:
            if self.scheduler is not None:
                self.scheduler.device_deadline(device, "observation_gap", deadline)
            self.report(device, "pending", reason=f"{error}. {hold} {purpose} as soon as it reports again.",
                        **details, **correction_details(error))
        else:
            self.report(device, "fault", reason=f"{error.entity or 'A required reading'} has not been usable for more than "
                        f"{OBSERVATION_GRACE_SECONDS // 60} minutes ({error}). {hold} {purpose} as soon as it reports again.",
                        **details, **correction_details(error))

    def end_gap(self, device):
        if self.scheduler is not None:
            self.scheduler.device_deadline(device, "observation_gap", None)

    def check_targets(self, device, targets):
        """A new control entity is adopted through the select, never by a silent release."""
        record = self.records.get(device)
        if record is None:
            return
        # A pre-upgrade pool journal also named temperature settings, which are never written.
        owned = {entity for entity in record["originals"]
                 if device != "pool" or entity.split(".")[0] in ("switch", "input_boolean")}
        if owned != set(targets):
            raise ValueError("The control entity changed while this device was Controlling; set it to "
                             "Verification and back to Controlling to apply the change")

    def inhibit_limit(self, record, mapping, now):
        """Once a paused device reaches its reviewed maximum pause, it may run for a quarter.

        SHS enforces this itself and keeps control. It used to be a fault whose
        handover to the device's own settings lasted until the plan changed.
        """
        forced = record.get("permit_forced_until")
        if forced and now < datetime.fromisoformat(forced):
            return datetime.fromisoformat(forced)
        record.pop("permit_forced_until", None)
        since = record.get("inhibited_since")
        if not since or (now - datetime.fromisoformat(since)).total_seconds() < mapping["max_inhibit_slots"] * 900:
            return None
        until = now + timedelta(seconds=900)
        record["permit_forced_until"] = until.isoformat()
        return until

    async def hold_inhibit_limit(self, device, options):
        """Without a plan every setting is held, but a paused device still gets its permitted run."""
        record = self.records.get(device)
        mapping = options.get("device_control_mappings", {}).get(device.removeprefix("device:"), {})
        if not device.startswith("device:") or not record or mapping.get("control_type") != "permit_inhibit":
            return False
        until = self.inhibit_limit(record, mapping, datetime.now(timezone.utc))
        if until is None:
            return False
        for entity in actuator_targets(mapping):
            await self.command(entity, "on")
        record.pop("inhibited_since", None)
        await self.save()
        self.device_deadline("permitted_run", until)
        self.report(device, "limited", reason=INHIBIT_LIMIT.format(mapping["max_inhibit_slots"]), retry_automatically=True)
        return True

    def battery_measurement(self, options):
        """Direction comes from explicit observations, magnitude from either power sign."""
        observed = []
        for key in ("battery_charging_entity", "battery_discharging_entity"):
            entity = options[key]
            if not entity.startswith("binary_sensor."):
                raise ValueError(f"{entity} must be a battery direction binary sensor")
            state = self.state(entity, max_age=BATTERY_MAX_AGE_SECONDS).state
            if state not in ("on", "off"):
                raise ValueError(f"{entity} must report on or off")
            observed.append(state == "on")
        magnitude = abs(self.watts(options["battery_power_measurement_entity"]))
        if all(observed) or (not any(observed) and magnitude > 100):
            raise ValueError("battery direction observations disagree with measured power")
        return magnitude if observed[0] else -magnitude if observed[1] else 0.0

    def battery_within_ceilings(self, options, charge, discharge, *actuators):
        measured = self.battery_measurement(options)
        sources = (options["battery_power_measurement_entity"], options["battery_charging_entity"],
                   options["battery_discharging_entity"])
        return (all(self.measurement_after_commands(source, *actuators) for source in sources)
                and -discharge - 100 <= measured <= charge + 100)

    def battery_quantity(self, entity):
        state = self.state(entity)
        return {"state": state.state, "attributes": state.attributes}

    def battery_ratings(self, options):
        return resolve_battery_quantities(options, self.battery_quantity)

    def battery_limit(self, entity, watts):
        """Validate the outgoing ceiling; the old register value is irrelevant."""
        return battery_limit_value(entity, watts, self.observed_state(entity))

    def preview_state(self, entity, *, max_age=None):
        """Read real HA state without scheduling observations or changing journals."""
        state = self.hass.states.get(entity) if entity else None
        return checked_state(state, entity, max_age)

    def preview_commands(self, slot, *, plan=None, options=None):
        """Describe intended targets without writes, simulation, or authority changes.

        Pool switching depends on current water readings; future execution
        recalculates it. Other adapters publish the same fields/basis/error shape.
        """
        options = self.options() if options is None else options
        previews = {}
        if slot.get("battery_command"):
            try:
                command = validate_battery_command(slot)
                mode = options[battery_mode_key(command)]
                mode_entity = options["battery_mode_entity"]
                if mode not in self.preview_state(mode_entity).attributes.get("options", []):
                    raise ValueError("Choose a supported battery mode in device setup")
                fields = [{"label": "Mode", "value": mode}]
                for direction in ("charge", "discharge"):
                    entity = options[f"battery_{direction}_limit_entity"]
                    state = self.hass.states.get(entity)
                    value = battery_limit_value(entity, command[f"{direction}_limit_w"], state)
                    fields.append({"label": f"{direction.capitalize()} limit", "value": value,
                                   "unit": state.attributes["unit_of_measurement"]})
                previews["battery"] = {"fields": fields}
            except (KeyError, TypeError, ValueError) as err:
                previews["battery"] = {"error": str(err)}
        if slot.get("pool_w") is not None and options.get("pool_enabled"):
            try:
                request, _, _ = self.pool_request(options, slot, read_state=self.preview_state, plan=plan)
                fields = [{"label": "Pool heater", "value": request["requested_switch_state"].capitalize()},
                          {"label": "Water temperature", "value": request["water_temperature_c"], "unit": "°C"},
                          {"label": "Stop at", "value": request["stop_temperature_c"], "unit": "°C"}]
                previews["pool"] = {"fields": fields, "basis": "current_readings"}
            except (KeyError, TypeError, ValueError) as err:
                previews["pool"] = {"error": str(err)}
        return previews

    async def execute_battery(self, options, slot):
        errors = battery_control_errors({**options, "battery_control_enabled": True})
        if errors:
            raise ValueError("; ".join(errors))
        soc = self.fraction(options["battery_soc_entity"], max_age=BATTERY_MAX_AGE_SECONDS)
        limits = self.battery_ratings(options)
        command = validate_battery_command(slot)
        operation = command["operation"]
        charge, discharge = command["charge_limit_w"], command["discharge_limit_w"]
        if charge > limits["battery_charge_max_w"] or discharge > limits["battery_discharge_max_w"]:
            raise ValueError("planned battery ceiling exceeds the current rated power")
        # A permission ceiling is not a request, so a full or empty pack does not
        # contradict it; only a sized purchase or sale has to be deliverable.
        if (operation in ("supply_house", "export") and soc <= limits["battery_min_soc"]) or (operation == "grid_charge" and soc >= 1):
            raise ValueError("battery SOC protection blocks the planned request")
        if operation == "export":
            price = slot.get("export_price_sek_per_kwh")
            if not slot.get("binding") or price is None or finite(price) < finite(options["battery_export_min_price_sek_per_kwh"]):
                raise ValueError("battery export is not permitted at this price")
            reserve = finite(options["battery_export_reserve_soc"])
            end = datetime.fromisoformat(slot["start"].replace("Z", "+00:00")) + timedelta(minutes=15)
            remaining_hours = min(.25, max(0, (end - datetime.now(timezone.utc)).total_seconds() / 3600))
            projected = soc - discharge / 1000 * remaining_hours / finite(options["battery_discharge_efficiency"]) / limits["battery_capacity_kwh"]
            if projected < reserve - 1e-6:
                raise ValueError("battery export would consume reserved charge")
        self.battery_measurement(options)
        charge_entity = options["battery_charge_limit_entity"]
        discharge_entity = options["battery_discharge_limit_entity"]
        targets = [(charge_entity, self.battery_limit(charge_entity, charge)),
                   (discharge_entity, self.battery_limit(discharge_entity, discharge))]
        mode_entity = options["battery_mode_entity"]
        supported = self.state(mode_entity).attributes.get("options", [])
        for key in ("battery_mode_charge", "battery_mode_discharge", "battery_mode_idle", "battery_mode_baseline"):
            if options.get(key) not in supported:
                raise ValueError(f"{key}: choose a supported option from {mode_entity}")
        # Journal the mapping before the first write. Handover uses configured
        # rated sources, never arbitrary or sentinel pre-existing register values.
        await self.capture("battery", options, [])
        mode = options[battery_mode_key(command)]
        # Close both ceilings for a mode transition; do not interrupt an
        # unchanged request on every scheduler tick.
        if self.state(mode_entity).state != mode:
            for entity, _ in targets:
                await self.command(entity, 0)
        await self.command(mode_entity, mode)
        for entity, value in sorted(targets, key=lambda target: target[1]):
            await self.command(entity, value)
        def delivered_within_ceiling():
            measured = self.battery_measurement(options)
            within = self.battery_within_ceilings(options, charge, discharge, mode_entity, charge_entity, discharge_entity)
            responding = (charge <= 100 or measured > 100 if operation == "grid_charge" else
                          discharge <= 100 or measured < -100 if operation == "export" else True)
            return within and responding
        if self.verifying:
            return {"state": "verified", "operation": operation, "physical_confirmation": "not_tested",
                    "decision": {"kind": "battery", "operation": operation,
                                 "charge_limit_w": charge, "discharge_limit_w": discharge}}
        await self.confirm(delivered_within_ceiling, "battery did not confirm the requested operation and power ceilings within 15 seconds; check inverter control availability")
        measured = self.battery_measurement(options)
        requested = finite(slot["battery_charge_w"]) - finite(slot["battery_discharge_w"])
        # Native regulation follows available surplus/demand, not forecast watts.
        limited = operation in ("grid_charge", "export") and abs(measured - requested) > 100
        return {"state": "limited" if limited else "confirmed",
                **({"reason": f"Battery power differs from the plan: requested {requested:g} W, measured {measured:g} W",
                    "next_step": "Inspect the inverter's operating limits and state of charge, and other automations controlling it. "
                    "If those do not explain the difference, download diagnostics for controller/planner review.",
                    "fix": {"kind": "device"}} if limited else {}),
                "operation": operation, "charge_limit_w": charge, "discharge_limit_w": discharge,
                "forecast_power_w": requested, "measured_power_w": measured,
                "decision": {"kind": "battery", "operation": operation,
                             "charge_limit_w": charge, "discharge_limit_w": discharge}}

    async def execute_device(self, device, options, slot):
        key = device.removeprefix("device:")
        mapping = options["device_control_mappings"][key]
        selected, _ = self.coordinator.binding_plan_for(device, options)
        models = selected["device_models"]
        validate_commands(slot["device_commands"], models)
        command = slot["device_commands"][key]
        if command["type"] == "unavailable":
            # A plan that cannot drive the device releases nothing; only the select does.
            return {"state": "unsupported", "reason": self.holding(device, command["reason"])}
        if command["type"] != mapping["control_type"]:
            raise ValueError("the website command does not match the reviewed local method")
        errors = execution_setup_errors(mapping)
        if errors:
            raise ValueError("; ".join(errors))
        targets = actuator_targets(mapping)
        # Every enabled owner reserves its targets, even before it captures a
        # baseline. This refuses both sides of an overlap before either writes.
        for other_key, other in options.get("device_control_mappings", {}).items():
            if other_key != key and device_mode(options, "device:" + other_key) in EXECUTING_MODES and set(targets) & set(actuator_targets(other)):
                raise ValueError("another enabled device shares this actuator")
        system_targets = {options.get(field) for field in (
            "battery_charge_limit_entity", "battery_discharge_limit_entity", "battery_mode_entity",
            "ev_charge_switch_entity")}
        _, pool_mapping = pool_control_mapping(options, models)
        system_targets.update(pool_mapping.get("actuator_entity_ids", []))
        if set(targets) & system_targets:
            raise ValueError("actuator is assigned to a system controller")
        self.check_targets(device, targets)
        record = self.records.get(device)
        if record:
            for entity in record.get("last_commands", {}):
                # Unavailable is unknown, never an external change.
                self.actuator_state(entity)
            changed = [entity for entity, value in record.get("last_commands", {}).items() if not self.matches(entity, value)]
            if changed:
                record["externally_changed"] = changed
                self.overrides[device] = EXTERNAL_CHANGE
                await self.save()
                await self.restore(device)
                return {"state": "overridden", "reason": self.overrides[device]}
        values = {}
        limited = None
        kind = command["type"]
        if kind == "setpoint":
            low = max(mapping["minimum_temperature_c"], command["minimum_c"])
            high = min(mapping["maximum_temperature_c"], command["maximum_c"])
            if not low <= command["target_c"] <= high:
                raise ValueError("planned temperature exceeds reviewed bounds")
            for entity in targets:
                state = self.state(entity)
                if entity.startswith("climate."):
                    if state.state != "heat" or self.hass.config.units.temperature_unit != "°C":
                        raise ValueError("setpoint execution requires an active Celsius heating thermostat")
                    step = finite(state.attributes.get("target_temp_step", 0.1))
                    origin = finite(state.attributes["min_temp"])
                else:
                    if state.attributes.get("unit_of_measurement") != "°C":
                        raise ValueError("temperature control must use Celsius")
                    step, origin = finite(state.attributes.get("step", 1)), finite(state.attributes["min"])
                if step <= 0:
                    raise ValueError("invalid temperature step")
                value = origin + round((command["target_c"] - origin) / step) * step
                if not low <= value <= high:
                    raise ValueError("no hardware temperature step fits the plan envelope")
                values[entity] = value
        else:
            on = command["permitted"] if kind == "permit_inhibit" else command["on_seconds"] == 900
            values = {entity: "on" if on else "off" for entity in targets}
            now = datetime.now(timezone.utc)
            if record and kind == "permit_inhibit" and not on and (limited := self.inhibit_limit(record, mapping, now)):
                on = True
                values = {entity: "on" for entity in targets}
                self.device_deadline("permitted_run", limited)
            if kind == "switch_schedule" and not record:
                for entity, value in values.items():
                    state = self.state(entity)
                    if state.state != value:
                        minimum = mapping.get("minimum_on_seconds" if state.state == "on" else "minimum_off_seconds")
                        if minimum is None or minimum == 0:
                            continue
                        at = getattr(state, "last_changed", None)
                        if at is None:
                            raise ValueError("cannot establish the actuator's current run time")
                        if (now - at).total_seconds() < minimum:
                            self.device_deadline("minimum_run:" + entity, at + timedelta(seconds=minimum))
                            raise ControlDeadlineError("initial switch transition violates the reviewed minimum run time")
            if record and kind == "switch_schedule":
                for entity, value in values.items():
                    previous = record.get("last_commands", {}).get(entity)
                    changed_at = record.get("transition_times", {}).get(entity)
                    if changed_at and previous != value:
                        minimum = mapping.get("minimum_on_seconds" if previous == "on" else "minimum_off_seconds")
                        if minimum is not None and (now - datetime.fromisoformat(changed_at)).total_seconds() < minimum:
                            self.device_deadline("minimum_run:" + entity,
                                                 datetime.fromisoformat(changed_at) + timedelta(seconds=minimum))
                            raise ControlDeadlineError("planned switch transition violates the reviewed minimum run time")
        record = await self.capture(device, options, targets)
        now = datetime.now(timezone.utc).isoformat()
        if kind == "permit_inhibit":
            if on:
                record.pop("inhibited_since", None)
                self.device_deadline("maximum_inhibit", None)
            else:
                record.setdefault("inhibited_since", now)
                self.device_deadline("maximum_inhibit", datetime.fromisoformat(record["inhibited_since"])
                                     + timedelta(seconds=mapping["max_inhibit_slots"] * 900))
        for entity, value in values.items():
            if record.get("last_commands", {}).get(entity) != value:
                record.setdefault("transition_times", {})[entity] = now
            await self.command(entity, value)
        await self.save()
        if kind == "setpoint":
            decision = {"kind": kind, "target_c": sorted(set(values.values()))}
        elif kind == "switch_schedule":
            decision = {"kind": kind, "on": command["on_seconds"] > 0}
        else:
            decision = {"kind": kind, "permitted": on}
        if limited:
            return {"state": "limited", "reason": INHIBIT_LIMIT.format(mapping["max_inhibit_slots"]),
                    "decision": decision, "retry_automatically": True}
        return {"state": "commanded", "reason": "actuator targets acknowledged; delivered heat or power is not inferred",
                "decision": decision}

    async def verify(self, device, options, slot, plan):
        if self.verification is None:
            raise ValueError("verification journal is unavailable")
        kind = device if device in DEVICES else options["device_control_mappings"][device.removeprefix("device:")]["control_type"]
        expected = OPERATIONS.get(kind, ())
        if not expected:
            raise ValueError("no executable operation catalogue for this device")
        attempt = evaluation_record(device, "control_verification", options, slot, plan, INTEGRATION_VERSION, expected)
        owned, overrides = self.records, self.overrides
        self.records, self.overrides = {}, deepcopy(overrides)
        self.verifying, self.verification_commands, self.shadow = True, [], {}
        self.verification_observations = {}
        gap = None
        try:
            result = (await self.execute_device(device, options, slot) if device.startswith("device:")
                      else await getattr(self, f"execute_{device}")(options, slot))
            if result["state"] in ("unsupported", "overridden"):
                raise ValueError(result["reason"])
            operation = operation_name(device, kind, slot, result)
            attempt.update(outcome="verified", operations=[operation], result=result)
            if result["state"] == "limited":
                attempt.update({key: result[key] for key in ("next_step", "fix") if key in result})
            try:
                await self.restore(device)
                attempt["operations"].append("handover")
            except Exception as err:
                attempt["handover_reason"] = str(err)
                attempt["handover_pending"] = True
                attempt.update(next_step="Review the handover commands and original values in the verification file. "
                               "Resolve rejected controls before enabling Controlling.", fix={"kind": "device"})
                attempt.update(correction_details(err))
        except Exception as err:
            attempt["reason"] = str(err)
            attempt.update(correction_details(err))
            if isinstance(err, ControlObservationError) and err.transient:
                gap = err
        finally:
            attempt["commands"] = self.verification_commands
            attempt["observations"] = self.verification_observations
            self.verifying = False
            self.records, self.overrides = owned, overrides
        group_id = await self.verification.append(attempt)
        if self.diagnostic_evaluation is not None:
            self.diagnostic_evaluation["verification_group_id"] = group_id
        if gap is not None:
            self.report_gap(device, gap, "resume verification", slot, plan)
            return
        self.end_gap(device)
        limited = attempt.get("result", {}).get("state") == "limited"
        self.report(device, ("limited" if limited else "verified") if attempt["outcome"] == "verified" else "fault",
                    reason=attempt.get("reason") or attempt.get("handover_reason") or (attempt["result"]["reason"] if limited else "Commands logged; physical response and cross-slot transitions are not tested"),
                    plan_id=plan.get("plan_id"), slot_start=slot["start"], retry_automatically=True,
                    **{key: attempt[key] for key in ("next_step", "fix", "handover_pending") if key in attempt},
                    **{key: value for key, value in attempt.get("result", {}).items()
                       if key in ("control_entity", "requested_switch_state", "water_temperature_c", "stop_temperature_c", "requested_power_w", "decision")},
                    **({"decision_reason": attempt["result"]["reason"]} if device == "pool" and attempt.get("result") else {}))

    def begin_diagnostic_evaluation(self, device, options, slot, plan, trigger):
        if self.verification is None:
            return
        self.diagnostic_evaluation = evaluation_record(
            device, device_mode(options, device), options, slot, plan, INTEGRATION_VERSION)
        self.diagnostic_evaluation.update(trigger=trigger, evidence_kind="runtime",
            ownership_before=deepcopy(self.records.get(device)),
            status_before=deepcopy(self.status.get(device)))

    async def finish_diagnostic_evaluation(self):
        attempt, self.diagnostic_evaluation = self.diagnostic_evaluation, None
        if attempt is None:
            return
        device = attempt["device"]
        attempt.update(completed_at=datetime.now(timezone.utc).isoformat(),
            outcome="evaluated", result=deepcopy(self.status.get(device, {})),
            ownership_after=deepcopy(self.records.get(device)),
            override=self.overrides.get(device), failure_latched=device in self.failed)
        try:
            await self.verification.append(attempt, runtime=True)
            self.diagnostics_error = None
        except Exception as err:
            # Diagnostic storage failure must not trigger actuator restoration.
            self.diagnostics_error = str(err)
            self.diagnostics_failed_evaluations += 1
            _LOGGER.warning("Cannot record controller diagnostics: %s", err)

    async def async_sample_diagnostics(self):
        """Observe every inventory device without running any actuator logic."""
        if self.closed or not self.initialized or self.verification is None:
            return
        if __package__:
            from .controller_observations import diagnostic_inventory
            from .presentation import equipment_present
            from .operating_modes import system_device_keys
        else:
            from controller_observations import diagnostic_inventory
            from presentation import equipment_present
            from operating_modes import system_device_keys
        try:
            async with self.lock:
                if self.closed:
                    return
                choices = await self.coordinator.async_cached_planning_configuration()
                options = self.options()
                devices = deepcopy(choices.get("devices", []))
                # System owners use the same identity selection as the panel.
                for system in DEVICES:
                    if equipment_present(options, system, devices):
                        if system not in system_device_keys(devices, options).values():
                            devices.append({"key": "$" + system, "system": system, "name": system})
                rows, _unassigned = diagnostic_inventory(devices, [], options)
                await self.verification.sample(options, rows, self.hass.states.get,
                    at=datetime.now(timezone.utc), version=INTEGRATION_VERSION,
                    slot=self.coordinator.current_plan_slot,
                    plan_id=(self.coordinator.optimisation_plan or {}).get("plan_id"))
                self.diagnostics_sampling_error = None
        except Exception as err:
            self.diagnostics_sampling_error = str(err)
            self.diagnostics_failed_samples += 1
            _LOGGER.warning("Cannot sample controller observations: %s", err)

    async def async_start(self, *, reason="integration_load"):
        try:
            saved = await self.store.async_load() or {}
        except Exception as err:
            for device in DEVICES:
                self.report(device, "fault", reason=f"cannot load restoration journal: {err}")
            return
        self.retired_pool_temperature_settings = saved.get("retired_pool_temperature_settings", {})
        self.records = saved.get("records", {})
        self.overrides = saved.get("overrides", {})
        journal_error = None
        if self.verification is not None:
            try:
                await self.verification.load()
                await self.verification.lifecycle("start", INTEGRATION_VERSION, reason)
            except Exception as err:
                journal_error = str(err)
                self.diagnostics_error = journal_error
        # A restart hands nothing back. Journalled ownership resumes with its
        # captured baseline, which only the select releases.
        if journal_error is not None:
            for device in DEVICES:
                self.report(device, "fault", reason=f"cannot load verification journal: {journal_error}")
            return
        self.initialized = True
        await self.async_sample_diagnostics()
        await self.async_tick(trigger="startup")
        if self.scheduler is not None:
            self.scheduler.plan_deadlines()
            self.scheduler.sample_deadline()

    async def async_tick(self, _now=None, *, trigger="manual", devices=None):
        skipped = ("inactive" if self.closed or not self.initialized else
                   "busy" if self.lock.locked() else None)
        totals = self.metrics.trigger(trigger, skipped)
        if skipped:
            if skipped == "busy" and self.scheduler is not None:
                self.scheduler.request(trigger, devices)
            return
        started = perf_counter()
        try:
            await self._async_tick(devices, trigger=trigger)
        finally:
            record_time(totals, started)

    async def _async_tick(self, devices=None, *, trigger="manual"):
        async with self.lock:
            options = self.options()
            slot = self.coordinator.current_plan_slot
            plan = self.coordinator.optimisation_plan or {}
            self.active_options, self.active_slot = deepcopy(options), deepcopy(slot)
            self.active_plan_id = plan.get("plan_id")
            mappings = options.get("device_control_mappings", {})
            generic = {"device:" + key for key, mapping in mappings.items() if device_mode(options, "device:" + key) in EXECUTING_MODES}
            generic.update(key for key in self.records if key.startswith("device:"))
            if self.scheduler is not None:
                self.scheduler.retain_devices(set(DEVICES) | generic)
            previous_authority = (self.requested_types, self.requested_systems, self.requested_error)
            if generic or any(device_mode(options, d) in EXECUTING_MODES for d in DEVICES) or self.records:
                self.requested_types = {}
                self.requested_systems = set()
                self.requested_error = None
                try:
                    requested = await self.coordinator.async_cached_device_configuration()
                    self.requested_types = {item["key"]: item.get("control_type") for item in requested}
                    self.requested_systems = {mapped_planning_path(item, mappings.get(item["key"], {}), options.get("pool_water_temperature_entity")) for item in requested if item["key"] not in options.get("excluded_device_readings", [])}
                    home = await self.coordinator.async_cached_home_configuration()
                    if home.get("battery", {}).get("included") is True:
                        self.requested_systems.add("battery")
                except Exception as err:
                    self.requested_types = {}
                    self.requested_systems = set()
                    # Without current website choices every device holds its last setting.
                    self.requested_error = str(err)
                    _LOGGER.error("Cannot read device planning ownership: %s", err)
            if previous_authority != (self.requested_types, self.requested_systems, self.requested_error):
                # A shared ownership change or cache failure affects all owners,
                # even if it was discovered during one device's sensor event.
                devices = None
            for device in tuple(self.overrides):
                # Leaving Controlling on the select clears an external-change latch.
                if device_mode(options, device) != "controlling":
                    del self.overrides[device]
                    await self.save()
            # Hash shared inputs once, excluding unused future-plan slots.
            metrics_context = fingerprint({
                "options": options, "slot": slot,
                "plan": {name: plan.get(name) for name in
                         ("plan_id", "schema_version", "capabilities", "device_models")},
                "requested_types": self.requested_types,
                "requested_systems": sorted(self.requested_systems, key=str),
            }).hex()
            for device in (*DEVICES, *sorted(generic)):
                if device == "battery" and getattr(self,"battery_runtime",None) is not None:
                    self.publish_battery_status()
                    continue
                if devices is not None and device not in devices:
                    continue
                self.device = device
                plan, slot = self.coordinator.binding_plan_for(device, options)
                self.active_options, self.active_slot = deepcopy(options), deepcopy(slot)
                self.active_plan_id = plan.get("plan_id")
                if self.scheduler is not None:
                    self.scheduler.begin_device(device)
                key = repr((options, plan.get("plan_id"), slot))
                self.metrics.begin_device(device, {
                    "mode": device_mode(options, device), "shared": metrics_context,
                    "override": self.overrides.get(device),
                    "ownership": self.records.get(device), "failed": self.failed.get(device),
                })
                self.begin_diagnostic_evaluation(device, options, slot, plan, trigger)
                try:
                    record = self.records.get(device)
                    inactive = self.inactive_status(device, options)
                    if record and (inactive or device_mode(options, device) != "controlling"):
                        # The only handover: the select, or a setting that removes the device from it.
                        await self.restore(device)
                        # The next attempt starts from the handed-back device, not the failed one.
                        self.failed.pop(device, None)
                    elif record and record.get("restoration_pending"):
                        # Returned to Controlling before the handover finished: control continues.
                        del record["restoration_pending"]
                        await self.save()
                    if inactive:
                        self.failed.pop(device, None)
                        self.end_gap(device)
                        self.report(device, **inactive)
                        continue
                    if hold := self.hold_reason(device, options):
                        self.report(device, "idle", reason=self.holding(device, hold))
                        continue
                    supported = (plan.get("schema_version", 0) >= 7 and device.removeprefix("device:") in slot.get("device_commands", {})
                                 if device.startswith("device:") and slot else plan.get("capabilities", {}).get(device))
                    if not slot or not supported:
                        # A missing plan holds every setting, except a pause that reached its limit.
                        if not await self.hold_inhibit_limit(device, options):
                            self.report(device, "idle", reason=self.holding(device, "No current plan for this device"))
                        continue
                    if self.failed.get(device) == key:
                        continue
                    if device_mode(options, device) == "control_verification":
                        # Re-evaluate each scheduler tick; observations can change
                        # the command inside the same quarter.
                        await self.verify(device, options, slot, plan)
                        continue
                    result = (await self.execute_device(device, options, slot) if device.startswith("device:")
                              else await getattr(self, f"execute_{device}")(options, slot))
                    self.failed.pop(device, None)
                    self.end_gap(device)
                    self.report(device, **result, slot_start=slot["start"], plan_id=plan.get("plan_id"))
                except Exception as err:
                    if isinstance(err,ControlDeadlineError):
                        self.failed.pop(device,None)
                        self.report(device,"pending",reason=str(err),retry_automatically=True)
                        continue
                    if (isinstance(err, PlanChangedError)
                            and self.coordinator.current_plan_slot is not None
                            and self.coordinator.operational_status["actionable"]
                            and self.options() == options and self.eligible_now(device, options)):
                        # Replacement is not loss of ownership. Stop the stale
                        # sequence; the next evaluation reads actual settings
                        # and completes the new command from that state.
                        self.failed.pop(device, None)
                        self.report(device, "pending", reason=str(err), retry_automatically=True,
                                    **correction_details(err))
                        if self.scheduler is not None:
                            self.scheduler.request("plan_replaced")
                        continue
                    if isinstance(err, ControlObservationError) and err.transient:
                        # Nothing is written or handed back while an entity is away. A
                        # handover the select already asked for stays queued for its return.
                        self.failed.pop(device, None)
                        self.report_gap(device, err, slot=slot, plan=plan)
                        continue
                    # Faults never release control; the device keeps the last setting SHS sent.
                    # A new observation can repair this fault without a new plan.
                    retry = isinstance(err, ControlObservationError)
                    if retry:
                        self.failed.pop(device, None)
                    else:
                        self.failed[device] = key
                    self.report(device, "fault", reason=self.holding(device, str(err)), retry_automatically=retry,
                                **correction_details(err))
                finally:
                    self.metrics.end_device()
                    if self.scheduler is not None:
                        self.scheduler.end_device(device)
                    await self.finish_diagnostic_evaluation()

    async def async_stop(self, _event=None):
        if self.closed:
            return
        self.closed = True
        self.observation_changed.set()
        if self.scheduler is not None:
            self.scheduler.pause()
        try:
            async with self.lock:
                # Stopping is half of a restart: every device keeps the last setting
                # SHS sent, and its journalled ownership resumes on the next start.
                if self.verification is not None and self.initialized:
                    await self.verification.lifecycle(
                        "stop", INTEGRATION_VERSION,
                        _event.event_type if _event is not None else "integration_unload_or_setup_stop",
                    )
        finally:
            if self.scheduler is not None:
                self.scheduler.close()
