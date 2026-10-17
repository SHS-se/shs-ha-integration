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
from .grid_capacity import GridCapacityConfigurationError
from .device_ownership import DeviceOwnership
from .api_contract import INTEGRATION_VERSION
from .operating_modes import device_mode, EXECUTING_MODES
from .verification import OPERATIONS, operation_name, evaluation_record, observation
from .controller_metrics import ControllerMetrics, fingerprint, record_time
from .battery_commands import validate_battery_command, battery_mode_key
from .device_commands import actuator_targets, execution_setup_errors, slot_command
from .hot_water import is_hot_water, heating_permission, hot_water_setup_errors, HotWaterConfigurationError
from .device_controls import pool_control_errors, pool_control_mapping, planning_path
from .device_controls import pool_requested_w, pool_permission

_LOGGER = logging.getLogger(__name__)
DEVICES = ("battery", "ev", "pool")
CONFIRM_SECONDS = 15
# Restarts and reconnects hide an entity for about a minute; longer needs attention.
OBSERVATION_GRACE_SECONDS = 5 * 60
EXTERNAL_CHANGE = "Actuator changed outside SHS; set this device to Verification and back to Controlling to resume"


class ControlObservationError(ValueError):
    """An unusable observation with a concrete inspection destination."""
    def __init__(self, message, entity, next_step, *, unavailable=False):
        super().__init__(message)
        self.entity = entity
        self.unavailable = unavailable
        self.details = {"next_step": next_step,
                        "fix": {"kind": "entity", "entity_id": entity} if entity else {"kind": "device"}}

    @property
    def transient(self):
        """A reading interrupted by a restart or reconnect, which returns by itself."""
        return self.unavailable


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
    if isinstance(error, (HotWaterConfigurationError, GridCapacityConfigurationError)):
        return {"fix": error.fix, "next_step": error.next_step}
    return error.details if isinstance(error, (ControlObservationError, PlanChangedError)) else {}


def checked_state(state, entity):
    if state is None or state.state in ("unknown", "unavailable"):
        raise ControlObservationError(
            f"{entity or 'required entity'} is unavailable", entity,
            "Check the source entity and its integration for an unavailable reading. "
            "If the entity was replaced, select its replacement in this device's setup.",
            unavailable=True,
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

    def observed_state(self, entity):
        state = self.inputs.read(entity) if entity else None
        if self.diagnostic_evaluation is not None and not self.verifying and entity:
            value = observation(state)
            self.diagnostic_evaluation["observations"].setdefault(entity, value)
            self.diagnostic_evaluation.setdefault("final_observations", {})[entity] = value
        self.metrics.observe(entity, state)
        if self.scheduler is not None:
            self.scheduler.observe(self.device, entity, state)
        return state

    def state(self, entity):
        state = (self.shadow.get(entity) if self.verifying else None) or self.observed_state(entity)
        if self.verifying and entity not in self.shadow:
            self.verification_observations[entity] = observation(state)
        return checked_state(state, entity)

    def actuator_state(self, entity):
        try:
            return self.state(entity)
        except ControlObservationError as error:
            if not error.unavailable:
                raise
            raise ActuatorUnavailableError(str(error), entity, error.details["next_step"], unavailable=True) from None

    def number(self, entity):
        return finite(self.state(entity).state)

    def watts(self, entity):
        state = self.state(entity)
        unit = state.attributes.get("unit_of_measurement")
        if unit not in ("W", "kW"):
            raise ValueError(f"{entity} must report W or kW")
        return finite(state.state) * (1000 if unit == "kW" else 1)

    def fraction(self, entity):
        state = self.state(entity)
        value = finite(state.state)
        if state.attributes.get("unit_of_measurement") == "%":
            value /= 100
        if not 0 <= value <= 1:
            raise ValueError(f"{entity} must report SOC as % or a fraction")
        return value

    def requires_grid_capacity(self, entity, value):
        """Stopping a switch, or setting EV current while off, adds no demand."""
        if entity.split('.')[0] in ('switch', 'input_boolean') and value == 'off':
            return False
        if self.device == 'ev':
            record = self.ownership.records.get('ev')
            options = record['options'] if self.restoring and record else self.options()
            switch = options.get('ev_charge_switch_entity')
            if entity != switch and self.actuator_state(switch).state == 'off':
                return False
            if not self.restoring and entity != switch and finite(value) <= self.number(entity):
                return False
        return self.device != 'battery'

    async def command(self, entity, value):
        if self.battery_writer_fence is not None and not self.verifying:
            self.battery_writer_fence.check_device_command(entity)
        self.check_authority()
        domain = entity.split(".")[0]
        state = ((self.shadow.get(entity) if self.verifying else None) or self.observed_state(entity)) if getattr(self, "device", None) == "battery" and domain == "number" else self.actuator_state(entity)
        native = native_command(entity, value, state,
                                temperature_unit=self.inputs.temperature_unit() if domain == "climate" else None)
        value, equal = native.value, native.equal
        if equal and not self.verifying and self.native_executor.requires_command(entity, value):
            equal = False
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
                    self.battery_writer_fence.check_device_command(entity)
                if self.requires_grid_capacity(entity, value) and not self.before_external_command(self.device, entity=entity, value=value):
                    if self.scheduler is not None:
                        self.scheduler.device_deadline(self.device,"battery_headroom",datetime.now(timezone.utc)+timedelta(seconds=5))
                    raise ControlDeadlineError("Waiting for battery charging to release grid capacity")
                def authorize():
                    self.check_authority()
                    if self.requires_grid_capacity(entity, value) and not self.before_external_command(self.device, entity=entity, value=value):
                        raise ControlDeadlineError('Waiting for battery charging to release grid capacity')
                    if self.battery_writer_fence is not None:
                        self.battery_writer_fence.check_device_command(entity)

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
                # One-time handover of a persisted pre-native ownership record.
                # This is not a battery policy or a physical-response gate.
                for entity, value in original.items():
                    await self.command(entity, value)
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
        connected = self.state(options.get("ev_connected_entity")).state
        if connected not in ("on", "off", "true", "false", "connected", "disconnected"):
            raise ValueError("EV connection state is not a supported boolean")
        soc = self.fraction(options.get("ev_soc_entity"))
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
        """Use the filtered temperature and validate availability of its raw source."""
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
                sources[entity] = None
                return temperature, sources
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
        water, sources = self.pool_temperature(options.get("pool_water_temperature_entity"), read_state=reader)
        requested = finite(pool_requested_w(plan, slot))
        heating = pool_permission(plan, slot)
        on = heating and water < target
        reason = ("The pool heater is allowed to run as planned." if on else
                  "The water has reached your Stop at temperature; the pool heater is off." if heating else
                  "The plan is pausing pool heating; the pool heater is off.")
        return {"device_key": key, "control_entity": entity, "requested_switch_state": "on" if on else "off",
                "water_temperature_c": water, "stop_temperature_c": target,
                "requested_power_w": requested, "reason": reason,
                "decision": {"kind": "pool", "heating": on, "water_temperature_c": water,
                             "stop_temperature_c": target}}, sources

    async def execute_pool(self, options, slot):
        request, _ = self.pool_request(options, slot)
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

    async def execute_device(self, device, options, slot):
        key = device.removeprefix("device:")
        mapping = options["device_control_mappings"][key]
        selected, _ = self.binding_execution_plan(device, options)
        models = selected["device_models"]
        command = slot_command(slot, key)
        if command["type"] == "unavailable":
            # A plan that cannot drive the device releases nothing; only the select does.
            return {"state": "unsupported", "reason": self.holding(device, command["reason"])}
        kind = command["type"]
        if kind != mapping["control_type"]:
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
        demand = None
        water = is_hot_water(next(m for m in models if m["key"] == key))
        if water:
            if hot_water_setup_errors(mapping):
                raise HotWaterConfigurationError(mapping, key)
            demand = self.watts(mapping["power"])
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
            if water:
                permission = self.actuator_state(targets[0]).state
                if permission not in ("on", "off"):
                    raise ValueError("Hot-water permission switch must report on or off")
                on = heating_permission(command["permitted"], permission == "on", demand)
            values = {entity: "on" if on else "off" for entity in targets}
        record = await self.capture(device, options, targets)
        for entity, value in values.items():
            await self.command(entity, value)
        await self.save()
        if kind == "setpoint":
            decision = {"kind": kind, "target_c": sorted(set(values.values()))}
        elif kind == "switch_schedule":
            decision = {"kind": kind, "on": command["on_seconds"] > 0}
        else:
            decision = {"kind": kind, "permitted": on}
            if demand is not None:
                decision.update(heater_power_w=demand, planned_permitted=command["permitted"], finishing_heating=on and not command["permitted"])
        return {"state": "commanded", "reason": "actuator targets acknowledged; delivered heat or power is not inferred",
                "decision": decision}

    def preview_state(self, entity):
        """Read real HA state without scheduling observations or changing journals."""
        state = self.inputs.read(entity) if entity else None
        return checked_state(state, entity)


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
                request, _ = self.pool_request(options, slot, read_state=self.preview_state, plan=plan)
                fields = [{"label": "Pool heater", "value": request["requested_switch_state"].capitalize()},
                          {"label": "Water temperature", "value": request["water_temperature_c"], "unit": "°C"},
                          {"label": "Stop at", "value": request["stop_temperature_c"], "unit": "°C"}]
                previews["pool"] = {"fields": fields, "basis": "current_readings"}
            except (KeyError, TypeError, ValueError) as err:
                previews["pool"] = {"error": str(err)}
        return previews
