"""One local command owner for the binding battery, EV and pool schedule.

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
    from .device_controls import battery_control_errors, pool_band_errors, planning_path
except ImportError:  # Pure executor tests, without importing Home Assistant.
    from device_controls import battery_control_errors, pool_band_errors, planning_path

_LOGGER = logging.getLogger(__name__)
DEVICES = ("battery", "ev", "pool")
CONFIRM_SECONDS = 15
SOURCE_MAX_AGE_SECONDS = 120


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
        raise ValueError("installed pool hysteresis does not fit the reviewed range")
    if not minimum <= start < stop <= maximum:
        raise ValueError("installed pool band is outside the reviewed range")
    upper = stop if on else min(stop, water - step)
    upper = min(max(upper, minimum + width), maximum)
    # Round downward to the actuator grid without changing the width.
    lower = minimum + int((upper - width - minimum + 1e-8) / step) * step
    return round(lower, 6), round(lower + width, 6)


class ScheduledController:
    """HA adapter supplied by the caller, so execution is behaviour-testable."""

    def __init__(self, hass, coordinator, store, options):
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
        domain = entity.split(".")[0]
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
            equal = abs(finite(state.state) - value) < 1e-6
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
            return abs(finite(state.state) - value) < 1e-6
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
        await self.store.async_save({"records": self.records})

    async def capture(self, device, options, entities):
        if device in self.records:
            return self.records[device]
        originals = {entity: self.state(entity).state for entity in entities if entity}
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
        record = self.records.get(device)
        if record is None:
            return
        options, original = record["options"], record["originals"]
        record["restoration_pending"] = True
        await self.save()
        self.restoring = True
        try:
            if device == "battery":
                await self.command(options["battery_power_entity"], 0)
                await self.command(options["battery_mode_entity"], options["battery_mode_baseline"])
                await self.confirm(
                    lambda: self.measurement_after_commands(
                        options["battery_power_measurement_entity"],
                        options["battery_mode_entity"], options["battery_power_entity"],
                    ), "no new battery measurement after baseline restoration",
                )
                measured = self.watts(options["battery_power_measurement_entity"])
                self.report(device, "baseline", measured_power_w=measured,
                            reason="baseline mode confirmed; power is measured, not inferred")
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
        water = self.number(water_entity, fresh=True)
        minimum = finite(options["pool_temperature_minimum"])
        maximum = finite(options["pool_temperature_maximum"])
        step = max(finite(self.state(e).attributes.get("step", 0.1)) for e in (start, stop))
        record = await self.capture("pool", options, [start, stop, options.get("pool_permission_entity")])
        original = record["originals"]
        on = finite(slot["pool_w"]) > 0
        band = pool_band((finite(original[start]), finite(original[stop])), water,
                         on, minimum, maximum, step)
        await self.band_commands(start, stop, band)
        if on and (permission := options.get("pool_permission_entity")):
            await self.command(permission, "on")
        limited = not on and band[1] >= water
        return {"state": "limited" if limited else "scheduled",
                "reason": "reviewed lower bound prevents further deferral" if limited else "band accepted; the local thermostat controls heating",
                "start_temperature_c": band[0], "stop_temperature_c": band[1],
                "water_temperature_c": water, "requested_power_w": slot["pool_w"]}

    async def execute_battery(self, options, slot):
        errors = battery_control_errors(options)
        if errors:
            raise ValueError("; ".join(errors))
        soc = self.fraction(options["battery_soc_entity"])
        floor = finite(options["battery_min_soc"])
        if entity := options.get("battery_min_soc_entity"):
            floor = max(floor, self.fraction(entity))
        charge, discharge = finite(slot["battery_charge_w"]), finite(slot["battery_discharge_w"])
        if min(charge, discharge) < 0 or charge and discharge:
            raise ValueError("invalid simultaneous battery charge and discharge")
        if charge > finite(options["battery_charge_max_w"]) or discharge > finite(options["battery_discharge_max_w"]):
            raise ValueError("planned battery power exceeds reviewed rating")
        if (discharge and soc <= floor) or (charge and soc >= finite(options["battery_max_soc"])):
            raise ValueError("battery SOC protection blocks the planned request")
        measured_entity = options["battery_power_measurement_entity"]
        self.watts(measured_entity)
        existing = self.records.get("battery")
        if existing and (confirmation := options.get("battery_authority_confirm_entity")):
            if self.state(confirmation).state != options["battery_authority_confirm_state"]:
                raise ValueError("battery remote authority was lost")
        await self.capture("battery", options, [])
        if claim := options.get("battery_authority_entity"):
            await self.command(claim, "on")
        if entity := options.get("battery_authority_confirm_entity"):
            await self.confirm(lambda: self.state(entity).state == options["battery_authority_confirm_state"], "battery remote authority was not granted")
        mode = options["battery_mode_charge" if charge else "battery_mode_discharge" if discharge else "battery_mode_idle"]
        await self.command(options["battery_mode_entity"], mode)
        power = charge - discharge
        if not options.get("battery_discharge_is_negative", False):
            power = -power
        if options["battery_power_unit"] == "kW":
            power /= 1000
        # Round toward zero, never request more than the planner authorised.
        target = options["battery_power_entity"]
        step = finite(self.state(target).attributes.get("step", 1))
        if step <= 0:
            raise ValueError("battery target has an invalid step")
        power = int(power / step + (1e-8 if power >= 0 else -1e-8)) * step
        await self.command(target, power)
        expected = abs(power) * (1000 if options["battery_power_unit"] == "kW" else 1)
        if discharge:
            expected = -expected
        if not options.get("battery_measurement_charge_positive", True):
            expected = -expected
        await self.confirm(
            lambda: self.measurement_after_commands(measured_entity, target, options["battery_mode_entity"])
            and abs(self.watts(measured_entity) - expected) <= max(100, abs(expected)*0.1),
            "battery did not achieve planned power within 15 seconds",
        )
        return {"state": "confirmed", "requested_power_w": charge-discharge,
                "measured_power_w": self.watts(measured_entity)}

    async def async_start(self):
        try:
            saved = await self.store.async_load() or {}
        except Exception as err:
            for device in DEVICES:
                self.report(device, "fault", reason=f"cannot load restoration journal: {err}")
            return
        self.records = saved.get("records", {})
        options = self.options()
        if options.get("battery_control_enabled") and not battery_control_errors(options):
            # A fresh enable also starts from a known baseline, not from a
            # possibly stranded command belonging to an earlier controller.
            if "battery" not in self.records:
                await self.capture("battery", options, [])
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
            for device in DEVICES:
                self.device = device
                key = repr((options, plan.get("plan_id"), slot))
                try:
                    record = self.records.get(device)
                    if record and (record.get("restoration_pending") or record["options"] != options or not slot or not self.eligible(device, options)):
                        await self.restore(device)
                    if not self.eligible(device, options):
                        self.failed.pop(device, None)
                        self.report(device, "disabled", reason="control disabled or manually overridden")
                        continue
                    if not slot or not plan.get("capabilities", {}).get(device):
                        await self.restore(device)
                        self.report(device, "idle", reason="no binding plan for this device")
                        continue
                    if self.failed.get(device) == key:
                        continue
                    result = await getattr(self, f"execute_{device}")(options, slot)
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
