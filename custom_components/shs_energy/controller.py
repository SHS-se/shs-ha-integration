"""One local command owner for binding system and per-device schedules.

The journal is saved *before* taking ownership. Restarts and mapping edits restore
using the old mapping, never through newly selected entities. Service completion
and physical confirmation are deliberately separate statuses.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import logging
from math import isfinite
from typing import Any

try:
    from .legacy_pool import assert_pool_handover_complete
    from .control_capabilities import build_command
    from .device_commands import actuator_targets, execution_setup_errors, validate_commands
    from .device_controls import planning_path
except ImportError:  # Pure executor tests, without importing Home Assistant.
    from legacy_pool import assert_pool_handover_complete
    from control_capabilities import build_command
    from device_commands import actuator_targets, execution_setup_errors, validate_commands
    from device_controls import planning_path

_LOGGER = logging.getLogger(__name__)
DEVICES = ("ev",)
CONFIRM_SECONDS = 15
SOURCE_MAX_AGE_SECONDS = 120


def finite(value: Any) -> float:
    if isinstance(value, bool):
        raise ValueError("boolean is not a numeric command")
    result = float(value)
    if not isfinite(result):
        raise ValueError("non-finite value")
    return result


class ScheduledController:
    """HA adapter supplied by the caller, so execution is behaviour-testable."""

    def __init__(self, hass, coordinator, store, options):
        self.hass = hass
        self.coordinator = coordinator
        self.store = store
        self.options = options
        self.records: dict[str, dict] = {}
        self.status = {device: {"state": "disabled"} for device in (*DEVICES, "battery", "pool")}
        self.listeners = set()
        self.lock = asyncio.Lock()
        self.closed = False
        self.restoring = False
        self.active_options = None
        self.active_slot = None
        self.failed = {}
        self.initialized = False
        self.command_times = {}
        self.overrides = {}
        self.requested_types = {}
        self.requested_systems = set()

    def add_listener(self, listener):
        self.listeners.add(listener)
        return lambda: self.listeners.discard(listener)

    def report(self, device, state, **details):
        value = {"state": state, **details}
        if self.status.get(device) == value:
            return
        self.status[device] = value
        _LOGGER.info("Scheduled %s: %s", device, value)
        for listener in tuple(self.listeners):
            listener()

    def state(self, entity, *, fresh=False):
        state = self.hass.states.get(entity) if entity else None
        if state is None or state.state in ("unknown", "unavailable"):
            raise ValueError(f"{entity or 'required entity'} is unavailable")
        if fresh:
            reported = getattr(state, "last_reported", state.last_updated)
            age = (datetime.now(timezone.utc) - reported).total_seconds()
            if not 0 <= age <= SOURCE_MAX_AGE_SECONDS:
                raise ValueError(f"{entity} is stale")
        return state

    def number(self, entity, *, fresh=False):
        return finite(self.state(entity, fresh=fresh).state)

    def watts(self, entity):
        state = self.state(entity, fresh=True)
        unit = state.attributes.get("unit_of_measurement")
        if unit not in ("W", "kW"):
            raise ValueError(f"{entity} must report W or kW")
        return finite(state.state) * (1000 if unit == "kW" else 1)

    def fraction(self, entity):
        state = self.state(entity, fresh=True)
        value = finite(state.state)
        if state.attributes.get("unit_of_measurement") == "%":
            value /= 100
        if not 0 <= value <= 1:
            raise ValueError(f"{entity} must report SOC as % or a fraction")
        return value

    def eligible(self, device, options):
        if device.startswith("device:"):
            key = device.removeprefix("device:")
            if key in options.get("excluded_device_readings", []):
                return False
            mapping = options.get("device_control_mappings", {}).get(key, {})
            if self.requested_types.get(key) != mapping.get("control_type") or not mapping.get("control_enabled", False) or device in self.overrides:
                return False
            override = mapping.get("control_override_entity")
            return not override or self.state(override).state == "off"
        if device not in self.requested_systems:
            return False
        if not options.get(f"{device}_control_enabled", False):
            return False
        if not options.get(f"{device}_enabled", True):
            return False
        override = options.get(f"{device}_control_override_entity")
        if override and self.state(override).state != "off":
            return False
        return True

    def check_authority(self):
        if self.restoring:
            return
        options = self.options()
        if options != self.active_options or self.closed:
            raise ValueError("configuration changed during execution")
        slot = self.coordinator.current_plan_slot
        if slot != self.active_slot:
            raise ValueError("plan changed or expired during execution")
        if not self.eligible(self.device, options):
            raise ValueError("control disabled or manually overridden")

    async def command(self, entity, value):
        self.check_authority()
        state = self.state(entity)
        temperature_unit = self.hass.config.units.temperature_unit if entity.startswith("climate.") else "°C"
        domain, service, data, value, equal = build_command(
            entity, state.state, state.attributes, value, temperature_unit=temperature_unit,
        )
        record = self.records.get(getattr(self, "device", ""))
        if record is not None and not self.restoring:
            record.setdefault("last_commands", {})[entity] = value
            await self.save()
        self.check_authority()
        if not equal:
            self.command_times[entity] = datetime.now(timezone.utc)
            await asyncio.wait_for(
                self.hass.services.async_call(
                    domain, service, {"entity_id": entity, **data}, blocking=True,
                ), timeout=CONFIRM_SECONDS,
            )
        await self.confirm(lambda: self.matches(entity, value), f"{entity} did not accept {value}")

    def matches(self, entity, value):
        state = self.state(entity)
        if isinstance(value, (int, float)):
            measured = state.attributes["temperature"] if entity.startswith("climate.") else state.state
            return abs(finite(measured) - value) < 1e-6
        return state.state == value

    async def confirm(self, predicate, error):
        deadline = asyncio.get_running_loop().time() + CONFIRM_SECONDS
        while True:
            self.check_authority()
            if predicate():
                return
            if asyncio.get_running_loop().time() >= deadline:
                raise ValueError(error)
            await asyncio.sleep(0.5)

    def measurement_after_commands(self, entity, *actuators):
        state = self.state(entity, fresh=True)
        reported = getattr(state, "last_reported", state.last_updated)
        return all(reported >= self.command_times[actuator]
                   for actuator in actuators if actuator in self.command_times)

    async def save(self):
        await self.store.async_save({"records": self.records, "overrides": self.overrides})

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
        record = self.records.get(device)
        if record is None:
            return
        options, original = record["options"], record["originals"]
        record["restoration_pending"] = True
        await self.save()
        assert_pool_handover_complete({'records': {device: record}}, options)
        self.restoring = True
        try:
            if device.startswith("device:"):
                changed = set(record.get("externally_changed", []))
                for entity, expected in record.get("last_commands", {}).items():
                    if not self.matches(entity, expected) and not self.matches(entity, original[entity]):
                        changed.add(entity)
                if changed:
                    record["externally_changed"] = sorted(changed)
                    self.overrides[device] = "Actuator changed externally; switch local control off and on to resume"
                    await self.save()
                mapping = options["device_control_mappings"][device.removeprefix("device:")]
                if mapping["control_type"] == "switch_schedule":
                    now = datetime.now(timezone.utc)
                    for entity, value in original.items():
                        at = record.get("transition_times", {}).get(entity)
                        last = record.get("last_commands", {}).get(entity)
                        if at and last != value and entity not in record.get("externally_changed", []):
                            minimum = mapping["minimum_on_seconds" if last == "on" else "minimum_off_seconds"]
                            if (now - datetime.fromisoformat(at)).total_seconds() < minimum:
                                raise ValueError("restoration waiting for minimum relay run time")
                for entity, value in original.items():
                    if entity not in record.get("externally_changed", []):
                        await self.command(entity, value)
            elif device == "battery":
                raise ValueError("legacy_handover_required: signed battery journal is retained; no legacy actuator writes are permitted")
            elif device == "pool":
                raise ValueError("legacy_handover_required: direct pool execution is retired")
            else:
                switch = options["ev_charge_switch_entity"]
                await self.command(switch, "off")
                for entity, value in original.items():
                    if entity != switch:
                        await self.command(entity, finite(value))
                await self.command(switch, original[switch])
            del self.records[device]
            await self.save()
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
        if self.state(entity).attributes.get("unit_of_measurement") != "A":
            raise ValueError("EV current control must use amperes")
        return entity, finite(low), finite(high)

    async def execute_ev(self, options, slot):
        entity, low, high = self.ev_mapping(options)
        switch = options.get("ev_charge_switch_entity")
        self.state(switch)
        connected = self.state(options.get("ev_connected_entity"), fresh=True).state
        if connected not in ("on", "off", "true", "false", "connected", "disconnected"):
            raise ValueError("EV connection state is not a supported boolean")
        soc = self.fraction(options.get("ev_soc_entity"))
        target_soc = self.fraction(options.get("ev_target_soc_entity"))
        current = finite(slot["ev_target_current_a"])
        if current and not low <= current <= high:
            raise ValueError("planned EV current exceeds reviewed bounds")
        if not finite(slot["ev_min_current_a"]) <= current <= finite(slot["ev_max_current_a"]):
            raise ValueError("planned EV current exceeds its slot envelope")
        await self.capture("ev", options, [entity, switch])
        if current == 0 or connected in ("off", "false", "disconnected") or soc >= target_soc:
            await self.command(switch, "off")
            return {"state": "stopped", "requested_current_a": current,
                    "reason": "off slot, disconnected, or charge target reached"}
        await self.command(entity, current)
        await self.command(switch, "on")
        return {"state": "commanded", "requested_current_a": current,
                "reason": "current and charge switch accepted; delivered power not inferred"}


    async def execute_device(self, device, options, slot):
        key = device.removeprefix("device:")
        if any(item.get('key') == key and planning_path(item.get('control_type'), item.get('category')) == 'pool'
               for item in await self.coordinator.async_cached_device_configuration()):
            raise ValueError('Pool scheduling requires the customer request interface; direct mappings are retired')
        mapping = options["device_control_mappings"][key]
        models = self.coordinator.optimisation_plan["device_models"]
        validate_commands(slot["device_commands"], models)
        command = slot["device_commands"][key]
        if command["type"] == "unavailable":
            await self.restore(device)
            return {"state": "unsupported", "reason": command["reason"]}
        if command["type"] != mapping["control_type"]:
            raise ValueError("the website command does not match the reviewed local method")
        errors = execution_setup_errors(mapping)
        if errors:
            raise ValueError("; ".join(errors))
        targets = actuator_targets(mapping)
        # Every enabled owner reserves its targets, even before it captures a
        # baseline. This refuses both sides of an overlap before either writes.
        for other_key, other in options.get("device_control_mappings", {}).items():
            if other_key != key and other.get("control_enabled") and set(targets) & set(actuator_targets(other)):
                raise ValueError("another enabled device shares this actuator")
        system_targets = {options.get(field) for field in (
            "battery_power_entity", "battery_mode_entity", "battery_authority_entity",
            "ev_charge_switch_entity", "pool_start_temperature_entity", "pool_stop_temperature_entity", "pool_permission_entity")}
        if set(targets) & system_targets:
            raise ValueError("actuator is assigned to a system controller")
        record = self.records.get(device)
        if record:
            changed = [entity for entity, value in record.get("last_commands", {}).items() if not self.matches(entity, value)]
            if changed:
                record["externally_changed"] = changed
                self.overrides[device] = "Actuator changed externally; switch local control off and on to resume"
                await self.save()
                await self.restore(device)
                return {"state": "overridden", "reason": self.overrides[device]}
        values = {}
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
            if record and kind == "permit_inhibit" and not on:
                since = record.get("inhibited_since")
                if since and (now - datetime.fromisoformat(since)).total_seconds() >= mapping["max_inhibit_slots"] * 900:
                    raise ValueError("maximum continuous inhibit reached")
            if kind == "switch_schedule" and not record:
                for entity, value in values.items():
                    state = self.state(entity)
                    if state.state != value:
                        at = getattr(state, "last_changed", None)
                        if at is None:
                            raise ValueError("cannot establish the actuator's current run time")
                        minimum = mapping["minimum_on_seconds" if state.state == "on" else "minimum_off_seconds"]
                        if (now - at).total_seconds() < minimum:
                            raise ValueError("initial switch transition violates the reviewed minimum run time")
            if record and kind == "switch_schedule":
                for entity, value in values.items():
                    previous = record.get("last_commands", {}).get(entity)
                    changed_at = record.get("transition_times", {}).get(entity)
                    if changed_at and previous != value:
                        minimum = mapping["minimum_on_seconds" if previous == "on" else "minimum_off_seconds"]
                        if (now - datetime.fromisoformat(changed_at)).total_seconds() < minimum:
                            raise ValueError("planned switch transition violates the reviewed minimum run time")
        record = await self.capture(device, options, targets)
        now = datetime.now(timezone.utc).isoformat()
        if kind == "permit_inhibit":
            if command["permitted"]:
                record.pop("inhibited_since", None)
            else:
                record.setdefault("inhibited_since", now)
        for entity, value in values.items():
            if record.get("last_commands", {}).get(entity) != value:
                record.setdefault("transition_times", {})[entity] = now
            await self.command(entity, value)
        await self.save()
        return {"state": "commanded", "reason": "actuator targets acknowledged; delivered heat or power is not inferred"}

    async def async_start(self):
        try:
            saved = await self.store.async_load() or {}
        except Exception as err:
            for device in (*DEVICES, "battery", "pool"):
                self.report(device, "fault", reason=f"cannot load restoration journal: {err}")
            return
        self.records = saved.get("records", {})
        self.overrides = saved.get("overrides", {})
        options = self.options()
        async with self.lock:
            for device in tuple(self.records):
                try:
                    await self.restore(device)
                except Exception as err:
                    self.report(device, "fault", reason=f"startup restoration: {err}")
        self.initialized = True
        await self.async_tick()

    async def async_tick(self, _now=None):
        if self.closed or not self.initialized or self.lock.locked():
            return
        async with self.lock:
            options = self.options()
            slot = self.coordinator.current_plan_slot
            plan = self.coordinator.optimisation_plan or {}
            self.active_options, self.active_slot = deepcopy(options), deepcopy(slot)
            mappings = options.get("device_control_mappings", {})
            generic = {"device:" + key for key, mapping in mappings.items() if mapping.get("control_enabled")}
            generic.update(key for key in self.records if key.startswith("device:"))
            if generic or any(options.get(d + "_control_enabled") for d in DEVICES) or self.records:
                self.requested_types = {}
                self.requested_systems = set()
                try:
                    requested = await self.coordinator.async_cached_device_configuration()
                    self.requested_types = {item["key"]: item.get("control_type") for item in requested}
                    self.requested_systems = {planning_path(item.get("control_type"), item.get("category")) for item in requested if item["key"] not in options.get("excluded_device_readings", [])}
                    choices = await self.coordinator.async_cached_planning_configuration()
                    if choices.get("home", {}).get("battery", {}).get("included") is True:
                        self.requested_systems.add("battery")
                except Exception as err:
                    self.requested_types = {}
                    self.requested_systems = set()
                    # Without current planning ownership, hand back all targets.
                    _LOGGER.error("Cannot read device planning ownership: %s", err)
            for device in tuple(self.overrides):
                if not mappings.get(device.removeprefix("device:"), {}).get("control_enabled"):
                    del self.overrides[device]
                    await self.save()
            for device in (*DEVICES, *sorted(generic)):
                self.device = device
                key = repr((options, plan.get("plan_id"), slot))
                try:
                    record = self.records.get(device)
                    if record and (record.get("restoration_pending") or record["options"] != options or not slot or not self.eligible(device, options)):
                        await self.restore(device)
                    if not self.eligible(device, options):
                        self.failed.pop(device, None)
                        self.report(device, "overridden" if device in self.overrides else "disabled", reason=self.overrides.get(device, "control disabled, excluded from the plan, or manually overridden"))
                        continue
                    supported = (plan.get("schema_version") == 7 and device.removeprefix("device:") in slot.get("device_commands", {})
                                 if device.startswith("device:") and slot else plan.get("capabilities", {}).get(device))
                    if not slot or not supported:
                        await self.restore(device)
                        self.report(device, "idle", reason="no binding plan for this device")
                        continue
                    if self.failed.get(device) == key:
                        continue
                    result = (await self.execute_device(device, options, slot) if device.startswith("device:")
                              else await getattr(self, f"execute_{device}")(options, slot))
                    self.report(device, **result, slot_start=slot["start"], plan_id=plan.get("plan_id"))
                except Exception as err:
                    self.failed[device] = key
                    reason = str(err)
                    try:
                        await self.restore(device)
                    except Exception as restore_error:
                        reason += f"; restoration pending: {restore_error}"
                    self.report(device, "fault", reason=reason)

    async def async_stop(self, _event=None):
        self.closed = True
        async with self.lock:
            for device in tuple(self.records):
                try:
                    await self.restore(device)
                except Exception as err:
                    self.report(device, "fault", reason=f"restoration pending: {err}")
