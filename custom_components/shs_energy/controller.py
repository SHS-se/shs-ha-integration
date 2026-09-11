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
import hashlib
import json
from math import isfinite
from typing import Any
from types import SimpleNamespace

try:
    from .api_contract import INTEGRATION_VERSION
    from .operating_modes import device_mode, EXECUTING_MODES, ownership_configuration
    from .verification import OPERATIONS, operation_name
    from .battery_commands import validate_battery_command
    from .configuration_values import resolve_battery_quantities, resolve_quantity
    from .device_commands import actuator_targets, execution_setup_errors, validate_commands
    from .device_controls import battery_control_errors, pool_band_errors, mapped_planning_path, planning_path
except ImportError:  # Pure executor tests, without importing Home Assistant.
    from api_contract import INTEGRATION_VERSION
    from operating_modes import device_mode, EXECUTING_MODES, ownership_configuration
    from verification import OPERATIONS, operation_name
    from battery_commands import validate_battery_command
    from configuration_values import resolve_battery_quantities, resolve_quantity
    from device_commands import actuator_targets, execution_setup_errors, validate_commands
    from device_controls import battery_control_errors, pool_band_errors, mapped_planning_path, planning_path

_LOGGER = logging.getLogger(__name__)
DEVICES = ("battery", "ev", "pool")
CONFIRM_SECONDS = 15
# Battery feedback gates power commands; pool and EV telemetry may report more slowly.
BATTERY_MAX_AGE_SECONDS = 120
POOL_MAX_AGE_SECONDS = 15 * 60
EV_MAX_AGE_SECONDS = 15 * 60


class ControlObservationError(ValueError):
    """An unusable observation with a concrete inspection destination."""
    def __init__(self, message, entity, next_step):
        super().__init__(message)
        self.details = {"next_step": next_step,
                        "fix": {"kind": "entity", "entity_id": entity} if entity else {"kind": "device"}}


def correction_details(error):
    return error.details if isinstance(error, ControlObservationError) else {}


def finite(value: Any) -> float:
    if isinstance(value, bool):
        raise ValueError("boolean is not a numeric command")
    result = float(value)
    if not isfinite(result):
        raise ValueError("non-finite value")
    return result


def pool_band(heat: tuple[float, float], water: float, on: bool,
              minimum: float, maximum: float, step: float) -> tuple[float, float]:
    """Move the installed hysteresis intact; never invent a warmer target."""
    start, stop = heat
    width = stop - start
    if step <= 0 or width <= 0 or width > maximum - minimum:
        raise ValueError("installed pool hysteresis does not fit the control limits")
    if not minimum <= start < stop <= maximum:
        raise ValueError("installed pool band is outside the control limits")
    upper = stop if on else min(stop, water - step)
    upper = min(max(upper, minimum + width), maximum)
    # Round downward to the actuator grid without changing the width.
    lower = minimum + int((upper - width - minimum + 1e-8) / step) * step
    return round(lower, 6), round(lower + width, 6)


def pool_hardware_band(heat, water, on, start_attributes, stop_attributes):
    """Use both registers' advertised bounds and steps, preserving hysteresis."""
    width = finite(heat[1]) - finite(heat[0])
    limits = [(finite(attrs["min"]), finite(attrs["max"]), finite(attrs["step"]))
              for attrs in (start_attributes, stop_attributes)]
    if any(low >= high or step <= 0 for low, high, step in limits):
        raise ValueError("invalid pool temperature control bounds or step")
    minimum = max(limits[0][0], limits[1][0] - width)
    maximum = min(limits[1][1], limits[0][1] + width)
    band = pool_band(heat, water, on, minimum, maximum, max(row[2] for row in limits))
    # Validate both writes before sending either half of a temperature band.
    for value, (low, high, step) in zip(band, limits):
        if not low <= value <= high or abs((value - low) / step - round((value - low) / step)) > 1e-5:
            raise ValueError("pool temperature band does not fit both controls' bounds and steps")
    return band


class ScheduledController:
    """HA adapter supplied by the caller, so execution is behaviour-testable."""

    def __init__(self, hass, coordinator, store, options, verification=None):
        self.verification = verification
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
        if self.verifying:
            return
        value = {"state": state, **details}
        if self.status.get(device) == value:
            return
        self.status[device] = value
        _LOGGER.info("Scheduled %s: %s", device, value)
        for listener in tuple(self.listeners):
            listener()

    def state(self, entity, *, max_age=None):
        state = (self.shadow.get(entity) if self.verifying else None) or (self.hass.states.get(entity) if entity else None)
        if self.verifying and entity not in self.shadow:
            self.verification_observations[entity] = {
                "state": state.state if state else None,
                "attributes": dict(state.attributes) if state else {},
                "last_reported": str(getattr(state, "last_reported", None)),
                "last_updated": str(getattr(state, "last_updated", None)),
            }
        if state is None or state.state in ("unknown", "unavailable"):
            raise ControlObservationError(
                f"{entity or 'required entity'} is unavailable", entity,
                "Check the source entity and its integration for an unavailable reading. "
                "If the entity was replaced, select its replacement in this device's setup.",
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
                )
        return state

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

    def eligible(self, device, options):
        if device.startswith("device:"):
            key = device.removeprefix("device:")
            if key in options.get("excluded_device_readings", []):
                return False
            mapping = options.get("device_control_mappings", {}).get(key, {})
            if self.requested_types.get(key) != mapping.get("control_type") or device_mode(options, device) not in EXECUTING_MODES or device in self.overrides:
                return False
            override = mapping.get("control_override_entity")
            return not override or self.state(override).state == "off"
        if device not in self.requested_systems:
            return False
        if device_mode(options, device) not in EXECUTING_MODES:
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
        domain = entity.split(".")[0]
        state = ((self.shadow.get(entity) if self.verifying else None) or self.hass.states.get(entity)) if getattr(self, "device", None) == "battery" and domain == "number" else self.state(entity)
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
        if self.verifying:
            self.verification_commands.append({
                "at": datetime.now(timezone.utc).isoformat(), "phase": "handover" if self.restoring else "plan",
                "trigger": "if control stopped now" if self.restoring else "current binding slot",
                "domain": domain, "service": service, "data": {"entity_id": entity, **data},
                "value": value, "observed_value": state.state,
                "would_call": not equal,
            })
            attributes = dict(state.attributes)
            if domain == "climate":
                attributes["temperature"] = value
            self.shadow[entity] = SimpleNamespace(state=state.state if domain == "climate" else str(value), attributes=attributes)
            record = self.records.get(self.device)
            if record is not None and not self.restoring:
                record.setdefault("last_commands", {})[entity] = value
            return
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
        state = (self.shadow.get(entity) if self.verifying else None) or self.hass.states.get(entity)
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
            self.check_authority()
            if predicate():
                return
            if asyncio.get_running_loop().time() >= deadline:
                raise ValueError(error)
            await asyncio.sleep(0.5)

    def measurement_after_commands(self, entity, *actuators):
        state = self.state(entity, max_age=BATTERY_MAX_AGE_SECONDS)
        reported = getattr(state, "last_reported", state.last_updated)
        return all(reported >= self.command_times[actuator]
                   for actuator in actuators if actuator in self.command_times)

    async def save(self):
        if self.verifying:
            return
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

    async def band_commands(self, start_entity, stop_entity, band):
        start, stop = band
        # Raising stop first / lowering start first never transiently inverts a band.
        if start >= self.number(stop_entity):
            await self.command(stop_entity, stop)
            await self.command(start_entity, start)
        else:
            await self.command(start_entity, start)
            await self.command(stop_entity, stop)

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
                    if not self.verifying and not self.matches(entity, expected) and not self.matches(entity, original[entity]):
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
                start = options["pool_start_temperature_entity"]
                stop = options["pool_stop_temperature_entity"]
                await self.band_commands(start, stop, (finite(original[start]), finite(original[stop])))
                if permission := options.get("pool_permission_entity"):
                    await self.command(permission, original[permission])
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
        await self.capture("ev", options, [entity, switch])
        if current == 0 or connected in ("off", "false", "disconnected") or soc >= target_soc:
            await self.command(switch, "off")
            return {"state": "stopped", "requested_current_a": current,
                    "reason": "off slot, disconnected, or charge target reached"}
        await self.command(entity, current)
        await self.command(switch, "on")
        return {"state": "commanded", "requested_current_a": current,
                "reason": "current and charge switch accepted; delivered power not inferred"}

    async def execute_pool(self, options, slot):
        errors = pool_band_errors(options)
        if errors:
            raise ValueError("; ".join(errors))
        start = options.get("pool_start_temperature_entity")
        stop = options.get("pool_stop_temperature_entity")
        water_entity = options.get("pool_water_temperature_entity")
        for entity in (start, stop, water_entity):
            if self.state(entity).attributes.get("unit_of_measurement") != "°C":
                raise ValueError(f"{entity}: pool control requires Celsius")
        water = self.number(water_entity, max_age=POOL_MAX_AGE_SECONDS)
        record = self.records.get("pool")
        heat = ((finite(record["originals"][start]), finite(record["originals"][stop]))
                if record else (self.number(start), self.number(stop)))
        on = finite(slot["pool_w"]) > 0
        band = pool_hardware_band(heat, water, on, self.state(start).attributes, self.state(stop).attributes)
        await self.capture("pool", options, [start, stop, options.get("pool_permission_entity")])
        await self.band_commands(start, stop, band)
        if on and (permission := options.get("pool_permission_entity")):
            await self.command(permission, "on")
        limited = not on and band[1] >= water
        return {"state": "limited" if limited else "scheduled",
                "reason": "temperature control lower limit prevents further deferral" if limited else "band accepted; the local thermostat controls heating",
                **({"next_step": "Check the Nibe start/stop limits against the current water temperature. "
                     "The plan requests deferral that these controls cannot enforce; download the evidence for controller/planner review.",
                    "fix": {"kind": "device"}} if limited else {}),
                "start_temperature_c": band[0], "stop_temperature_c": band[1],
                "water_temperature_c": water, "requested_power_w": slot["pool_w"]}

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
        state = self.hass.states.get(entity)
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
        if operation != "self_consumption" and ((discharge and soc <= limits["battery_min_soc"]) or (charge and soc >= 1)):
            raise ValueError("battery SOC protection blocks the planned request")
        if operation == "export":
            price = slot.get("export_price_sek_per_kwh")
            if not options.get("battery_export_enabled") or not slot.get("binding") or price is None or finite(price) < finite(options["battery_export_min_price_sek_per_kwh"]):
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
        mode_key = {"self_consumption": "battery_mode_baseline", "solar_charge": "battery_mode_baseline",
                    "supply_house": "battery_mode_baseline", "grid_charge": "battery_mode_charge",
                    "export": "battery_mode_discharge", "hold": "battery_mode_idle"}[operation]
        mode = options[mode_key]
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
            return {"state": "verified", "operation": operation, "physical_confirmation": "not_tested"}
        await self.confirm(delivered_within_ceiling, "battery did not confirm the requested operation and power ceilings within 15 seconds; check inverter control availability")
        measured = self.battery_measurement(options)
        requested = finite(slot["battery_charge_w"]) - finite(slot["battery_discharge_w"])
        return {"state": "limited" if abs(measured - requested) > 100 else "confirmed",
                **({"reason": f"Battery power differs from the plan: requested {requested:g} W, measured {measured:g} W",
                    "next_step": "Inspect the inverter's operating limits and state of charge, and other automations controlling it. "
                    "If those do not explain the difference, download diagnostics for controller/planner review.",
                    "fix": {"kind": "device"}} if abs(measured - requested) > 100 else {}),
                "operation": operation, "charge_limit_w": charge, "discharge_limit_w": discharge,
                "requested_power_w": requested, "measured_power_w": measured}

    async def execute_device(self, device, options, slot):
        key = device.removeprefix("device:")
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
            if other_key != key and device_mode(options, "device:" + other_key) in EXECUTING_MODES and set(targets) & set(actuator_targets(other)):
                raise ValueError("another enabled device shares this actuator")
        system_targets = {options.get(field) for field in (
            "battery_charge_limit_entity", "battery_discharge_limit_entity", "battery_mode_entity",
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

    async def verify(self, device, options, slot, plan):
        if self.verification is None:
            raise ValueError("verification journal is unavailable")
        kind = device if device in DEVICES else options["device_control_mappings"][device.removeprefix("device:")]["control_type"]
        expected = OPERATIONS.get(kind, ())
        if not expected:
            raise ValueError("no executable operation catalogue for this device")
        configuration = json.dumps({"version": INTEGRATION_VERSION, "options": options}, sort_keys=True, default=str)
        scope = device + ":" + hashlib.sha256(configuration.encode()).hexdigest()[:16]
        attempt = {"device": device, "scope": scope, "at": datetime.now(timezone.utc).isoformat(),
                   "mode": "control_verification", "integration_version": INTEGRATION_VERSION, "plan_id": plan.get("plan_id"),
                   "slot_start": slot["start"], "slot": deepcopy(slot),
                   "plan_schema_version": plan.get("schema_version"), "plan_issued_at": plan.get("issued_at"),
                   "configuration": deepcopy(options), "expected_operations": list(expected),
                   "operations": [], "outcome": "blocked"}
        owned, overrides = self.records, self.overrides
        self.records, self.overrides = {}, deepcopy(overrides)
        self.verifying, self.verification_commands, self.shadow = True, [], {}
        self.verification_observations = {}
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
        finally:
            attempt["commands"] = self.verification_commands
            attempt["observations"] = self.verification_observations
            self.verifying = False
            self.records, self.overrides = owned, overrides
        await self.verification.append(attempt)
        limited = attempt.get("result", {}).get("state") == "limited"
        self.report(device, ("limited" if limited else "verified") if attempt["outcome"] == "verified" else "fault",
                    reason=attempt.get("reason") or attempt.get("handover_reason") or (attempt["result"]["reason"] if limited else "Commands logged; physical response and cross-slot transitions are not tested"),
                    plan_id=plan.get("plan_id"), slot_start=slot["start"], retry_automatically=True,
                    **{key: attempt[key] for key in ("next_step", "fix", "handover_pending") if key in attempt})

    async def async_start(self):
        try:
            saved = await self.store.async_load() or {}
        except Exception as err:
            for device in DEVICES:
                self.report(device, "fault", reason=f"cannot load restoration journal: {err}")
            return
        self.records = saved.get("records", {})
        self.overrides = saved.get("overrides", {})
        async with self.lock:
            for device in tuple(self.records):
                try:
                    await self.restore(device)
                except Exception as err:
                    self.report(device, "fault", reason=f"startup restoration: {err}")
        if self.verification is not None:
            try:
                await self.verification.load()
            except Exception as err:
                for device in DEVICES:
                    self.report(device, "fault", reason=f"cannot load verification journal: {err}")
                return
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
            generic = {"device:" + key for key, mapping in mappings.items() if device_mode(options, "device:" + key) in EXECUTING_MODES}
            generic.update(key for key in self.records if key.startswith("device:"))
            if generic or any(device_mode(options, d) in EXECUTING_MODES for d in DEVICES) or self.records:
                self.requested_types = {}
                self.requested_systems = set()
                try:
                    requested = await self.coordinator.async_cached_device_configuration()
                    self.requested_types = {item["key"]: item.get("control_type") for item in requested}
                    self.requested_systems = {mapped_planning_path(item, mappings.get(item["key"], {}), options.get("pool_water_temperature_entity")) for item in requested if item["key"] not in options.get("excluded_device_readings", [])}
                    choices = await self.coordinator.async_cached_planning_configuration()
                    if choices.get("home", {}).get("battery", {}).get("included") is True:
                        self.requested_systems.add("battery")
                except Exception as err:
                    self.requested_types = {}
                    self.requested_systems = set()
                    # Without current planning ownership, hand back all targets.
                    _LOGGER.error("Cannot read device planning ownership: %s", err)
            for device in tuple(self.overrides):
                if device_mode(options, device) not in EXECUTING_MODES:
                    del self.overrides[device]
                    await self.save()
            for device in (*DEVICES, *sorted(generic)):
                self.device = device
                key = repr((options, plan.get("plan_id"), slot))
                try:
                    record = self.records.get(device)
                    if record and (record.get("restoration_pending") or ownership_configuration(record["options"], device) != ownership_configuration(options, device) or not slot or not self.eligible(device, options)):
                        await self.restore(device)
                    if not self.eligible(device, options):
                        self.failed.pop(device, None)
                        self.report(device, "overridden" if device in self.overrides else "disabled", reason=self.overrides.get(device, "control disabled, excluded from the plan, or manually overridden"))
                        continue
                    supported = (plan.get("schema_version", 0) >= 7 and device.removeprefix("device:") in slot.get("device_commands", {})
                                 if device.startswith("device:") and slot else plan.get("capabilities", {}).get(device))
                    if not slot or not supported:
                        await self.restore(device)
                        self.report(device, "idle", reason="no binding plan for this device")
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
                    self.report(device, **result, slot_start=slot["start"], plan_id=plan.get("plan_id"))
                except Exception as err:
                    # A new observation can repair this fault without a new plan.
                    retry = isinstance(err, ControlObservationError)
                    if retry:
                        self.failed.pop(device, None)
                    else:
                        self.failed[device] = key
                    reason = str(err)
                    try:
                        await self.restore(device)
                    except Exception as restore_error:
                        reason += f"; restoration pending: {restore_error}"
                    self.report(device, "fault", reason=reason, retry_automatically=retry, **correction_details(err))

    async def async_stop(self, _event=None):
        self.closed = True
        async with self.lock:
            for device in tuple(self.records):
                try:
                    await self.restore(device)
                except Exception as err:
                    self.report(device, "fault", reason=f"restoration pending: {err}")
            if self.verification is not None:
                await self.verification.flush()
