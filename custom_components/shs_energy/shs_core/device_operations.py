"""Shared physical execution and verification of admitted device intentions."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timezone, timedelta
import logging
from types import SimpleNamespace
from time import perf_counter
from uuid import uuid4

from .command_journal import Command
from .native_commands import finite, native_command
from .device_ownership import DeviceOwnership
from .api_contract import INTEGRATION_VERSION
from .operating_modes import device_mode, EXECUTING_MODES
from .verification import OPERATIONS, operation_name, evaluation_record, observation
from .controller_metrics import ControllerMetrics, fingerprint, record_time
from .battery_commands import validate_battery_command, battery_mode_key
from .configuration_values import resolve_battery_quantities, resolve_quantity
from .device_commands import actuator_targets, execution_setup_errors, validate_commands
from .device_controls import battery_control_errors, pool_control_errors, pool_control_mapping, planning_path
from .device_controls import pool_requested_w

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


class DeviceOperations:
    """Unscheduled physical transactions; the host supplies decision authority.

    Owns baseline capture, native setting order, restoration and run obligations.
    It never fetches a plan, schedules a policy loop, or loads battery accounting.
    execution_plan() supplies an already admitted device request; check_authority()
    validates its host/session before each physical effect.
    """

    def make_command(self, identity, phase, action):
        return Command(identity, 'controller', self.device, phase, action)

    def observed_state(self, entity, *, max_age=None):
        state = self.inputs.read(entity) if entity else None
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

    async def command(self, entity, value):
        if self.battery_writer_fence is not None and not self.verifying:
            self.battery_writer_fence.check_legacy(entity)
        self.check_authority()
        domain = entity.split(".")[0]
        state = ((self.shadow.get(entity) if self.verifying else None) or self.observed_state(entity)) if getattr(self, "device", None) == "battery" and domain == "number" else self.actuator_state(entity)
        native = native_command(entity, value, state,
                                temperature_unit=self.inputs.temperature_unit() if domain == "climate" else None)
        value, equal = native.value, native.equal
        _, service, service_data = native.action.service_call()
        data = {key: item for key, item in service_data.items() if key != "entity_id"}
        now = datetime.now(timezone.utc)
        command = {"at": now.isoformat(), "phase": "handover" if self.restoring else "plan",
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
            record = self.ownership.records.get(self.device)
            if record is not None and not self.restoring:
                record.setdefault("last_commands", {})[entity] = value
            return
        command.update(called=False, transport="not_sent", settings_confirmation="not_checked")
        if self.diagnostic_evaluation is not None:
            self.diagnostic_evaluation["commands"].append(command)
        try:
            record = self.ownership.records.get(getattr(self, "device", ""))
            if record is not None and not self.restoring:
                record.setdefault("last_commands", {})[entity] = value
                await self.save()
            self.check_authority()
            if not equal:
                if self.battery_writer_fence is not None:
                    self.battery_writer_fence.check_legacy(entity)
                if self.device!="battery" and not self.before_external_command(self.device):
                    if self.scheduler is not None:
                        self.scheduler.device_deadline(self.device,"battery_headroom",datetime.now(timezone.utc)+timedelta(seconds=5))
                    raise ControlDeadlineError("Waiting for battery charging to release grid capacity")
                def authorize():
                    self.check_authority()
                    if self.device != 'battery' and not self.before_external_command(self.device):
                        raise ControlDeadlineError('Waiting for battery charging to release grid capacity')
                    if self.battery_writer_fence is not None:
                        self.battery_writer_fence.check_legacy(entity)

                def sent():
                    self.command_times[entity] = datetime.now(timezone.utc)
                    command.update(called=True, transport="ambiguous")

                command["command_id"] = self.command_identity()
                request = self.make_command(command["command_id"], command["phase"], native.action)
                await self.native_executor.execute(request, authorize=authorize,
                    timeout=CONFIRM_SECONDS, on_sent=sent)
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
        await self.ownership.save()


    async def capture(self, device, options, entities):
        if device in self.ownership.records:
            return self.ownership.records[device]
        originals = {entity: (self.state(entity).attributes["temperature"] if entity.startswith("climate.")
                              else self.state(entity).state) for entity in entities if entity}
        if self.verifying:
            # Verification owns only a hypothetical record, never a disk write.
            record = {"options": deepcopy(options), "originals": originals}
            self.ownership.records[device] = record
            return record
        return await self.ownership.capture(device, options, originals)

    async def restore(self, device):
        self.device = device
        record = self.ownership.records.get(device)
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
                    self.ownership.overrides[device] = EXTERNAL_CHANGE
                    await self.save()
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
                    self.ownership.retired_pool_temperature_settings = {entity: value for entity, value in original.items()
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
            del self.ownership.records[device]
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
        plan = self.execution_plan() or {}
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
            if self.inputs.platform(entity) != "filter":
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
            plan, _ = self.binding_execution_plan("pool", options)
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
        requested = finite(pool_requested_w(plan, slot))
        heating = requested > 0
        on = heating and water < target
        reason = ("The pool heater is allowed to run as planned." if on else
                  "The water has reached your Stop at temperature; the pool heater is off." if heating else
                  "The plan is pausing pool heating; the pool heater is off.")
        return {"device_key": key, "control_entity": entity, "requested_switch_state": "on" if on else "off",
                "water_temperature_c": water, "stop_temperature_c": target,
                "requested_power_w": requested, "reason": reason,
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

    def check_targets(self, device, targets):
        """A new control entity is adopted through the select, never by a silent release."""
        record = self.ownership.records.get(device)
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
        record = self.ownership.records.get(device)
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
        selected, _ = self.binding_execution_plan(device, options)
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
        record = self.ownership.records.get(device)
        if record:
            for entity in record.get("last_commands", {}):
                # Unavailable is unknown, never an external change.
                self.actuator_state(entity)
            changed = [entity for entity, value in record.get("last_commands", {}).items() if not self.matches(entity, value)]
            if changed:
                record["externally_changed"] = changed
                self.ownership.overrides[device] = EXTERNAL_CHANGE
                await self.save()
                await self.restore(device)
                return {"state": "overridden", "reason": self.ownership.overrides[device]}
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
                    if state.state != "heat" or self.inputs.temperature_unit() != "°C":
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

    def preview_state(self, entity, *, max_age=None):
        """Read real HA state without scheduling observations or changing journals."""
        state = self.inputs.read(entity) if entity else None
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
                    state = self.inputs.read(entity)
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
