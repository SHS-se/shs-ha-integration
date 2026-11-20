"""Shared physical execution and verification of admitted device intentions."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timezone, timedelta
import logging
from math import floor
from types import SimpleNamespace
from time import perf_counter
from uuid import uuid4

from .command_journal import Command
from .native_commands import finite, native_command
from .native_numbers import floor_native
from .grid_capacity import EV_WATTS_PER_AMP, GridCapacityConfigurationError
from .device_ownership import DeviceOwnership
from .api_contract import INTEGRATION_VERSION
from .operating_modes import device_mode, EXECUTING_MODES
from .verification import OPERATIONS, operation_name, evaluation_record, observation
from .controller_metrics import ControllerMetrics, fingerprint, record_time
from .battery_commands import validate_battery_command, battery_mode_key
from .device_commands import actuator_targets, execution_setup_errors, slot_command
from .hot_water import is_hot_water, heating_permission, hot_water_setup_errors, HotWaterConfigurationError
from .device_controls import device_planning_path, pool_control_errors, pool_control_mapping, planning_path
from .device_controls import pool_requested_w, pool_permission
from .heating_overrides import heating_binding, relay_heat, room_temperature
from .ev_charge_override import (URGENT_SLACK_H, capacity_kwh, charge_need_kwh, efficiency,
                                 override_current, override_slack_h)

_LOGGER = logging.getLogger(__name__)
DEVICES = ("battery", "ev", "pool")
CONFIRM_SECONDS = 15
# A battery-powered Zigbee thermostat can take this long to act on a lower
# setting. Until then a pause is not lowered a further step.
PAUSE_RESPONSE_SECONDS = 60
# A car cannot be unplugged while charging (user requirement, 20 November 2026).
OUTSIDE_STOP_PAUSE_SECONDS = 60
# Tessie showed a new current 116 s after the write (20 November 2026). Within a
# minute the same setting is not sent again; the car may also have dropped a
# setting sent before it started, which this corrects after a minute.
EV_RESEND_SECONDS = 60
# A charging switch that turns off this long after SHS switched it on was on
# in between: Tessie reports it only when it changes, never re-reports it.
OUTSIDE_STOP_SETTLE_SECONDS = 30
# A car that drew power during an override and has drawn none for a minute,
# with SHS's charger switch still on, was stopped in the car (20 November 2026).
CAR_DRAWING_W = 1000
CAR_IDLE_W = 100
CAR_STOP_SECONDS = 60
# A Tesla often stops one percentage point short of its charge limit.
CAR_STOP_SHORT = 0.01
# Restarts and reconnects hide an entity for about a minute; longer needs attention.
OBSERVATION_GRACE_SECONDS = 5 * 60


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


class PowerHistoryError(ControlObservationError):
    """Missing runtime evidence is retryable; it is never a configuration fault."""
    def __init__(self, key, mapping):
        sensor = mapping.get('power')
        super().__init__(f'{key}: no positive recorded power is available for {sensor}; waiting for power history before increasing demand',
            sensor if isinstance(sensor, str) else None,
            'Inspect the mapped power sensor and its Recorder history.')


class ActuatorUnavailableError(ControlObservationError):
    """The entity SHS writes cannot be reached, so nothing can be sent or handed back yet."""


class PlanChangedError(ValueError):
    """A superseded execution snapshot, with evidence for the exact stop reason."""
    def __init__(self, code, message, context):
        super().__init__(f"plan changed or expired during execution: {message}")
        self.message = message
        self.details = {"blocked_reason": code, "blocked_context": context}


class ControlDeadlineError(ValueError):
    """The next eligible transition already has an explicit wake-up deadline."""


class HeatingDeferred(ControlDeadlineError):
    """A user heating request is being arbitrated or has been superseded."""


class ControlPlanError(ValueError):
    """An explicit invalid plan command needs a changed plan, not a software retry."""


class ControlCommandError(ValueError):
    """An attempted command did not receive its required physical confirmation."""


def rounded_limit(target, attributes):
    """The car's charge limit setting that covers the override target."""
    step = finite(attributes.get("step", 1)) or 1
    low, high = finite(attributes.get("min", 0)), finite(attributes.get("max", 100))
    value = min(high, max(low, -(-target // step) * step))
    return int(value) if float(value).is_integer() else value


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


def rounded_setpoint(target, origin, step):
    if step <= 0:
        raise ValueError("invalid temperature step")
    # The nearest native step, halves up, including above the plan's range
    # when the thermostat cannot represent the exact target.
    return origin + floor((target - origin) / step + 0.5 + 1e-9) * step


class DeviceOperations:
    """Unscheduled physical transactions; the host supplies decision authority.

    Owns baseline capture, native setting order, restoration and run obligations.
    It never fetches a plan, schedules a policy loop, or loads battery accounting.
    execution_plan() supplies an already admitted device request; check_authority()
    validates its host/session before each physical effect.
    """
    # The HA gateway's manual EV charge override store (ev_charge_override.py).
    ev_override = None

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
        if entity.split('.')[0] in ('switch', 'input_boolean', 'climate') and value == 'off':
            return False
        if self.device == 'ev':
            record = self.ownership.records.get('ev')
            options = record['options'] if self.restoring and record else self.options()
            if entity == options.get('ev_target_soc_entity'):
                return False  # The car's charge limit draws nothing.
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
        in_flight = getattr(self.native_executor, 'command_in_flight', None)
        if not equal and not self.verifying and in_flight is not None and in_flight(entity, value):
            # The control still reports its value from before this write
            # (Tessie lags about 40 s). Re-sending it is not a correction.
            equal = True
        if (not equal and not self.verifying and getattr(self, "device", None) == "ev"
                and domain == "number" and self.recently_sent(entity, value)):
            # Tessie echoes a sent setting at once and then polls the car's
            # older value for up to a minute. Within that minute the same
            # setting is not sent again (20 November 2026: rewritten every
            # ten seconds because the echo had settled the change).
            equal = True
        _, service, service_data = native.action.service_call()
        data = {key: item for key, item in service_data.items() if key != "entity_id"}
        now = datetime.now(timezone.utc)
        command = {"at": now.isoformat(), "phase": "handover" if self.restoring else "plan",
                   "domain": domain, "service": service, "data": {"entity_id": entity, **data},
                   "value": value, "observed_value": state.state}
        if self.verifying:
            self.verification_commands.append({**command,
                "trigger": "if a baseline handover is requested" if self.restoring else "current binding slot",
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
                    if getattr(self, "device", None) == "ev":
                        self.__dict__.setdefault("ev_sends", {})[entity] = (value, self.command_times[entity])
                    command.update(called=True, transport="ambiguous")

                command["command_id"] = self.command_identity()
                request = self.make_command(command["command_id"], command["phase"], native.action)
                await self.native_executor.execute(request, authorize=authorize,
                    timeout=CONFIRM_SECONDS, on_sent=sent)
                command["transport"] = "accepted"
                # Reports up to now may be the integration echoing this request.
                self.__dict__.setdefault("returned_at", {})[entity] = datetime.now(timezone.utc)
            if record is not None and not self.restoring and service != 'set_hvac_mode':
                record.setdefault("last_commands", {})[entity] = value
                await self.save()
            # Service completion records an accepted setting. Ordinary readings
            # and explicit device workflows assess the physical response.
        except (Exception, asyncio.CancelledError) as err:
            command["error"] = str(err) or type(err).__name__
            raise
        finally:
            command["completed_at"] = datetime.now(timezone.utc).isoformat()

    def ev_raise_held(self, entity, admitted, power_entity=None):
        """Raise the car's current only once the last one is confirmed; reductions apply at once.

        Confirmed means the car's meter, read after the send, shows it drawing
        the sent current within one ampere. Tessie's readback cannot confirm:
        it echoes a sent value at once and reports the car's older value for
        up to two minutes. Without confirmation the raise waits a minute (a car
        can stay below its setting). 20 November 2026: as the car ramped up,
        SHS sent 7, 11, 12 and 11 A within 14 seconds.
        """
        sent = self.__dict__.get("ev_sends", {}).get(entity)
        if (self.verifying or sent is None or admitted <= finite(sent[0])
                or datetime.now(timezone.utc) - sent[1] >= timedelta(seconds=EV_RESEND_SECONDS)
                or self.ev_current_confirmed(power_entity, finite(sent[0]), sent[1])):
            return admitted
        return finite(sent[0])

    def ev_current_confirmed(self, power_entity, current, sent_at):
        reading = self.state(power_entity) if power_entity else None
        if reading is None or reading.state in ("unknown", "unavailable"):
            return False
        reported = getattr(reading, "last_reported", None) or getattr(reading, "last_updated", None)
        if reported is None or reported <= sent_at:
            return False
        try:
            watts = finite(reading.state) * (1000 if reading.attributes.get("unit_of_measurement") == "kW" else 1)
        except (TypeError, ValueError):
            return False
        return abs(watts - current * EV_WATTS_PER_AMP) <= EV_WATTS_PER_AMP

    def recently_sent(self, entity, value):
        """The same car setting was sent within the equipment response window."""
        sent = self.__dict__.get("ev_sends", {}).get(entity)
        if sent is None or datetime.now(timezone.utc) - sent[1] >= timedelta(seconds=EV_RESEND_SECONDS):
            return False
        try:
            return abs(float(sent[0]) - float(value)) < 1e-6
        except (TypeError, ValueError):
            return sent[0] == value

    def reported_since_return(self, entity, state):
        """The control reported after SHS's last call to it returned, not only echoed it."""
        returned = self.__dict__.get("returned_at", {}).get(entity)
        reported = getattr(state, "last_reported", None) or getattr(state, "last_updated", None)
        return returned is None or reported is None or reported > returned

    def switched_off_after_landing(self, switch, reading):
        """The switch changed to off well after SHS's switch-on returned, so it was on in between."""
        returned = self.__dict__.get("returned_at", {}).get(switch)
        changed = getattr(reading, "last_changed", None) or getattr(reading, "last_updated", None)
        return (returned is not None and changed is not None
                and changed - returned >= timedelta(seconds=OUTSIDE_STOP_SETTLE_SECONDS))

    def matches(self, entity, value):
        state = (self.shadow.get(entity) if self.verifying else None) or self.observed_state(entity)
        if state is None or state.state in ("unknown", "unavailable"):
            return False
        if isinstance(value, (int, float)):
            measured = state.attributes.get("temperature") if entity.startswith("climate.") else state.state
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
                raise ControlCommandError(error) from None

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
        if not self.verifying and device_mode(self.options(), device) == 'control_verification':
            await self.ownership.abandon({device})
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
                    state = self.actuator_state(entity)
                    if record.get('heating_suspended') == entity and state.state == 'off':
                        continue
                    if not self.matches(entity, expected) and not self.matches(entity, original[entity]):
                        changed.add(entity)
                if changed:
                    record["externally_changed"] = sorted(changed)
                    await self.save()
                for entity, value in original.items():
                    if record.get('heating_suspended') == entity and self.actuator_state(entity).state == 'off':
                        await self.command(entity, 'heat')
                        await self.confirm(lambda: self.matches(entity, 'heat'), 'Thermostat has not returned to heating mode')
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

    def ev_candidates(self, options):
        plan = self.execution_plan() or {}
        keys = set((self.active_slot or {}).get("device_loads_w", {}))
        models = plan.get("device_models", [])
        # The plan's reviewed device inventory owns category routing.
        routed = {m["key"] for m in models
                  if planning_path(m.get("control_type"), m.get("category")) == "ev"}
        mappings = options.get("device_control_mappings", {})
        return [mappings[key] for key in sorted(routed & keys) if key in mappings]

    def ev_power_entity(self, options, switch):
        """The car's metered charging power; the gateway uses its admitted source."""
        powers = sorted({m["power"] for m in self.ev_candidates(options) if isinstance(m.get("power"), str)})
        return powers[0] if powers else None

    def ev_mapping(self, options):
        candidates = self.ev_candidates(options)
        targets = {(m.get("control_entity_id"), m.get("minimum_value"),
                    m.get("maximum_value")) for m in candidates}
        if len(targets) != 1:
            raise ValueError("EV requires one planned, reviewed current control")
        entity, low, high = targets.pop()
        if self.actuator_state(entity).attributes.get("unit_of_measurement") != "A":
            raise ValueError("EV current control must use amperes")
        return entity, finite(low), finite(high)

    async def ev_import_current(self, entity, switch, current, minimum, *, maximum, spare_house_w,
                                hold_spare_increase=False):
        return current

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
            raise ControlPlanError("planned EV current exceeds reviewed bounds")
        slot_minimum = finite(slot["ev_min_current_a"])
        slot_maximum = finite(slot["ev_max_current_a"])
        if not slot_minimum <= current <= slot_maximum:
            raise ControlPlanError("planned EV current exceeds its slot envelope")
        self.check_targets("ev", [entity, switch])
        await self.capture("ev", options, [entity, switch])
        disconnected = connected in ("off", "false", "disconnected")
        forced = await self.ev_charge_override(options, soc, disconnected, low, high)
        if forced is not None:
            # The override is a floor on the plan, within the charger's reviewed range.
            current = max(current, forced["current_a"])
            slot_minimum, slot_maximum = low, high
            # Live priority sizes the car from this, also in other owners' decisions.
            self.ev_override.live = dict(current_a=current, slack_h=forced["slack_h"],
                                         urgent=forced["slack_h"] <= URGENT_SLACK_H,
                                         started_at=forced["started_at"])
            target_soc = forced["target_soc"]
            slot = {**slot, "spare_house_w": None}
        if current == 0 or disconnected or soc >= target_soc:
            await self.clear_outside_stop("ev")
            reason = ("plan requests charging off" if current == 0 else
                      "vehicle disconnected" if connected in ("off", "false", "disconnected") else
                      "charge target reached")
            await self.command(switch, "off")
            return {"state": "stopped", "requested_current_a": current,
                    "execution_condition": ({"status": "temporarily_absent", "cause": "vehicle_disconnected"}
                        if connected in ("off", "false", "disconnected") and soc < target_soc else None),
                    "reason": "off slot, disconnected, or charge target reached",
                    "decision": {"kind": "ev", "charging": False, "current_a": 0, "reason": reason}}
        if forced is not None and (stopped := self.outside_stop_started("ev")) and forced["started_at"] > stopped:
            # Setting an override is an explicit request to charge now.
            await self.clear_outside_stop("ev")
        if (forced is not None and soc >= forced["target_soc"] - CAR_STOP_SHORT
                and self.actuator_state(switch).state == "off"
                and self.ownership.records["ev"].get("last_commands", {}).get(switch) == "on"):
            # The car stopped itself at the limit SHS raised, often one point
            # short of it: the target is reached, not stopped outside SHS.
            await self.end_ev_charge_override(options)
            await self.command(switch, "off")
            await self.clear_outside_stop("ev")
            return {"state": "stopped", "requested_current_a": 0,
                    "reason": "The charge override reached its target; the car follows the plan again.",
                    "decision": {"kind": "ev", "charging": False, "current_a": 0,
                                 "reason": "charge override target reached"}}
        if forced is not None and self.car_stopped_itself("ev", options, switch):
            # SHS's own switch is still on: a stop in the car's app ends the
            # override, and the car stays stopped so it can be unplugged.
            await self.end_ev_charge_override(options)
            return {"state": "stopped", "requested_current_a": 0,
                    "reason": "The car stopped charging outside SHS, so the charge override has ended.",
                    "decision": {"kind": "ev", "charging": False, "current_a": 0,
                                 "reason": "charging stopped outside SHS"}}
        resumes = await self.outside_stop_pause("ev", switch)
        if resumes is not None:
            if forced is not None:
                await self.end_ev_charge_override(options)
            return {"state": "stopped", "requested_current_a": current,
                    "reason": ("Charging was stopped outside SHS. Planned charging resumes "
                               "after a minute unless the car is unplugged."),
                    "resumes_at": resumes.isoformat(),
                    "decision": {"kind": "ev", "charging": False, "current_a": 0,
                                 "reason": "charging stopped outside SHS"}}
        native = self.actuator_state(entity).attributes
        available = current
        if current > finite(native["max"]):
            # Some chargers expose their full range only once charging starts.
            # Apply a supported setting before enabling, then follow the
            # reported range on the next evaluation.
            ceiling = floor_native(current, native)
            if ceiling is None or ceiling < max(low, slot_minimum) or ceiling <= 0:
                raise ControlObservationError(f"{entity}: the car's available current is below the planned minimum",
                    entity, "Inspect the available current reported by the car.")
            available = float(ceiling)
        admitted = await self.ev_import_current(entity, switch, available, max(low, slot_minimum),
            maximum=min(high, slot_maximum), spare_house_w=slot.get('spare_house_w'),
            hold_spare_increase=slot.get('hold_spare_increase', False))
        admitted = self.ev_raise_held(entity, admitted, self.ev_power_entity(options, switch))
        if admitted == 0:
            # Zero is a charging pause, not necessarily a supported number
            # setting. Keep the scheduled target so meter events can resume it.
            await self.command(switch, 'off')
            return {'state': 'limited', 'requested_current_a': 0,
                    'reason': 'House import capacity is below the minimum charging current.',
                    'decision': {'kind': 'ev', 'charging': False, 'current_a': 0}}
        await self.command(entity, admitted)
        if forced is not None:
            await self.command(switch, "on")
            result = {"state": "commanded" if admitted >= current else "limited",
                      "requested_current_a": admitted, "reason": forced["reason"],
                      "charge_override": forced["row"],
                      "decision": {"kind": "ev", "charging": True, "current_a": admitted}}
            if admitted < current:
                result["reason"] += (f" House import capacity allows {admitted:g} A for now." if admitted < available
                                     else f" The car reports a limit of {available:g} A for now.")
            return result
        if admitted > current:
            return {"state": "commanded", "requested_current_a": admitted,
                    "reason": f"Using spare house capacity at {admitted:g} A. The plan requests {current:g} A.",
                    "decision": {"kind": "ev", "charging": True, "current_a": admitted}}
        await self.command(switch, "on")
        if admitted < current:
            reason = (f"House import capacity allows {admitted:g} A. The plan requests {current:g} A."
                      if admitted < available else
                      f"The car reports a limit of {available:g} A. The plan requests {current:g} A.")
            result = {"state": "limited", "requested_current_a": admitted, "reason": reason,
                      "decision": {"kind": "ev", "charging": True, "current_a": admitted}}
            if admitted == available:
                result["fix"] = {"kind": "entity", "entity_id": entity}
            return result
        return {"state": "commanded", "requested_current_a": current,
                "reason": "current and charge switch accepted; delivered power not inferred",
                "decision": {"kind": "ev", "charging": True, "current_a": current}}

    async def outside_stop_pause(self, device, switch):
        """Pause planned charging for a minute after a stop made outside SHS.

        A car cannot be unplugged while it is charging (user requirement,
        20 November 2026), so switching such a stop straight back would trap
        the cable. The pause is journalled; it is not a release. A switch that
        has not reported charging since SHS switched it on is SHS's own command
        still landing, never an outside stop.
        """
        if self.verifying:
            return None
        record = self.ownership.records[device]
        reading = self.actuator_state(switch)
        state = reading.state
        commanded = record.get("last_commands", {}).get(switch)
        now = datetime.now(timezone.utc)
        until = record.get("outside_stop_until")
        before = (until, record.get("charging_seen"))
        if state == "on":
            record.pop("outside_stop_until", None)
            until = None
            if commanded == "on" and self.reported_since_return(switch, reading):
                # Tessie's echo of SHS's own switch-on is not the car charging.
                record["charging_seen"] = True
        elif until is not None and now < datetime.fromisoformat(until):
            pass
        elif until is not None:
            record.pop("outside_stop_until")
            until = None
        elif commanded == "on" and (record.get("charging_seen") or self.switched_off_after_landing(switch, reading)):
            until = (now + timedelta(seconds=OUTSIDE_STOP_PAUSE_SECONDS)).isoformat()
            record["outside_stop_until"] = until
            # The switch is already off; SHS starts it again only after the pause.
            record["last_commands"][switch] = "off"
            record.pop("charging_seen", None)
            _LOGGER.warning("%s was switched off outside SHS; planned charging pauses until %s", switch, until)
        else:
            record.pop("charging_seen", None)
        if (until, record.get("charging_seen")) != before:
            await self.save()
        if until is None:
            self.device_deadline("outside_stop", None)
            return None
        resumes = datetime.fromisoformat(until)
        self.device_deadline("outside_stop", resumes)
        return resumes

    def outside_stop_started(self, device):
        record = self.ownership.records.get(device) or {}
        until = record.get("outside_stop_until")
        if until is None:
            return ""
        return (datetime.fromisoformat(until) - timedelta(seconds=OUTSIDE_STOP_PAUSE_SECONDS)).isoformat()

    async def ev_charge_override(self, options, soc, disconnected, low, high):
        """The manual override's current, or None once it has ended or is absent.

        See docs/ev-charge-override.md. The override only adds charging: it
        never writes a setting while the device is not Controlling, and the
        charge limit it raised is the only setting it hands back.
        """
        store = getattr(self, "ev_override", None)
        if store is None or self.verifying or store.active is None:
            return None
        row = store.active
        target = row["target_soc"] / 100
        if row.get("ending") or disconnected or soc >= target:
            await self.end_ev_charge_override(options)
            return None
        limit_entity = options.get("ev_target_soc_entity")
        if (limit_entity and limit_entity.startswith("number.") and row.get("raised_limit") is None
                and self.fraction(limit_entity) < target):
            limit = self.actuator_state(limit_entity)
            saved = finite(limit.state)
            saved = int(saved) if saved.is_integer() else saved
            raised = rounded_limit(row["target_soc"], limit.attributes)
            await self.command(limit_entity, raised)
            await store.record_limit(saved, raised)
        remaining = options.get("ev_energy_remaining_entity")
        size = capacity_kwh(soc, finite(self.state(remaining).state) if remaining else None)
        now = datetime.now(timezone.utc)
        deadline = store.deadline()
        if size is None:
            current, need = high, None
        else:
            need = charge_need_kwh(soc, target, size, efficiency(options))
            current = override_current(need, deadline, now, low, high)
        slack = override_slack_h(need, deadline, now, high)
        self.device_deadline("charge_override", now + timedelta(minutes=5))
        if deadline is None:
            by = " as fast as possible"
        else:
            minutes = max(0, int((deadline - now).total_seconds() // 60))
            by = f" within {minutes // 60} h {minutes % 60} min"
        return {"current_a": current, "target_soc": target, "started_at": row["started_at"],
                "slack_h": slack, "row": {**row, "need_kwh": need},
                "reason": f"Charge override to {row['target_soc']:g}%{by}."}

    async def end_ev_charge_override(self, options):
        """Hand back the charge limit only if the car still reports what SHS set."""
        store = self.ev_override
        row = store.active
        limit_entity = options.get("ev_target_soc_entity")
        if row and row.get("raised_limit") is not None and limit_entity:
            state = self.state(limit_entity)
            try:
                unchanged = abs(finite(state.state) - row["raised_limit"]) < 1e-6
            except (TypeError, ValueError):
                unchanged = False
            if unchanged:
                await self.command(limit_entity, row["saved_limit"])
        await store.clear()
        (self.ownership.records.get("ev") or {}).pop("override_drawn", None)
        self.device_deadline("charge_override", None)

    def car_stopped_itself(self, device, options, switch):
        """The car drew power during the override and has drawn none for a minute.

        With the charger switch still on, only the car (its app, its screen)
        stops the charge. A car that stopped at its target is handled first.
        """
        record = self.ownership.records[device]
        if self.verifying or self.actuator_state(switch).state != "on" or record.get("last_commands", {}).get(switch) != "on":
            record.pop("override_drawn", None)
            return False
        entity = self.ev_power_entity(options, switch)
        reading = self.state(entity) if entity else None
        if reading is None or reading.state in ("unknown", "unavailable"):
            return False
        try:
            watts = finite(reading.state) * (1000 if reading.attributes.get("unit_of_measurement") == "kW" else 1)
        except (TypeError, ValueError):
            return False
        if watts >= CAR_DRAWING_W:
            record["override_drawn"] = True
            return False
        changed = getattr(reading, "last_changed", None) or getattr(reading, "last_updated", None)
        return bool(record.get("override_drawn") and watts < CAR_IDLE_W and changed is not None
                    and datetime.now(timezone.utc) - changed >= timedelta(seconds=CAR_STOP_SECONDS))

    async def clear_outside_stop(self, device):
        record = self.ownership.records.get(device)
        if self.verifying or record is None:
            return
        if "outside_stop_until" in record or "charging_seen" in record:
            record.pop("outside_stop_until", None)
            record.pop("charging_seen", None)
            await self.save()
        self.device_deadline("outside_stop", None)

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

    def heating_permission_for(self, mapping, commanded, planned, demand):
        """The same active-cycle rule for execution and capacity feasibility."""
        entity = actuator_targets(mapping)[0]
        permission = self.actuator_state(entity).state
        if permission not in ('on','off'):
            raise ValueError('Hot-water permission switch must report on or off')
        # Only heating SHS permitted may finish; an outside ON during an
        # inhibition never acquires protection.
        permitted = permission == 'on' and commanded.get(entity,'on') == 'on'
        return heating_permission(planned,permitted,demand)

    async def pause_thermostat(self, device, options, entity, accepted=None):
        """Stop a thermostat's call for heat while it stays in Heat.

        User requirement, 10 November 2026: a controller-driven pause never
        sends Off, which blanks the display of the laundry, master-bathroom
        and pool-bathroom thermostats. Only the setpoint moves, to the highest
        supported step at or below the thermostat's own temperature, so the
        call for heat ends whatever its dead zone. Scheduled import relief and
        manual heating-override relief share this routine. `accepted` is the
        target to return to; the pause keeps the first one it was given.
        """
        state = self.actuator_state(entity)
        if not entity.startswith('climate.'):
            raise HeatingDeferred('Thermostat cannot pause heating')
        record = self.ownership.records.get(device)
        if state.state == 'off' and record and record.get('heating_suspended') == entity:
            # Paused with Off by an earlier version. It is already not
            # heating; it returns to Heat when the pause clears.
            return {'state': 'manual', 'reason': 'Making room for a heating override'}
        if state.state != 'heat':
            raise HeatingDeferred('Thermostat cannot pause its current mode')
        attrs = state.attributes
        try:
            current, target = finite(attrs['current_temperature']), finite(attrs['temperature'])
            step, low = finite(attrs.get('target_temp_step', 0.1)), finite(attrs['min_temp'])
            if step <= 0:
                raise ValueError('invalid temperature step')
        except (KeyError, TypeError, ValueError) as error:
            raise ControlObservationError(f'{entity}: the thermostat does not report the temperature and setting '
                'a pause needs', entity, 'Check the thermostat’s own temperature and target.') from error
        pause = low + floor((current - low) / step + 1e-9) * step
        paused = (record or {}).get('heating_pause')
        sent = self.command_times.get(entity)
        if (paused and paused.get('entity') == entity and attrs.get('hvac_action') == 'heating' and target <= pause
                and (sent is None or datetime.now(timezone.utc) - sent >= timedelta(seconds=PAUSE_RESPONSE_SECONDS))):
            # The lower setting was acknowledged and the thermostat still
            # calls for heat: go one more supported step down.
            pause = target - step
        pause = max(low, min(pause, target))
        record = await self.capture(device, options, [entity])
        if not paused or paused.get('entity') != entity:
            record['heating_pause'] = {'entity': entity, 'target_c': target if accepted is None else accepted}
            await self.save()
        if pause < target:
            await self.command(entity, pause)
        return {'state': 'manual', 'reason': 'Making room for a heating override'}

    def shed_scheduled_heat(self, entity, state):
        return False

    def paused_room_is_warm(self, entity, state, planned):
        attrs = state.attributes
        try:
            if attrs.get('hvac_action') == 'heating':
                return False
            if attrs.get('hvac_action') not in ('idle', 'off'):
                raise ValueError('The thermostat’s heating state is unavailable')
            temperature = room_temperature(entity, state.state, attrs, self.inputs.temperature_unit())
            # Use the planned target for the same 0.5 °C start margin as the room model.
            return temperature > planned - 0.5
        except (KeyError, TypeError, ValueError) as error:
            raise ControlObservationError(str(error), entity,
                'Check the thermostat’s room temperature and heating state.') from error

    async def execute_device(self, device, options, slot, *, target_c=None, suspend_heat=False):
        key = device.removeprefix("device:")
        mapping = options["device_control_mappings"][key]
        selected, _ = self.binding_execution_plan(device, options)
        models = selected["device_models"]
        command = (slot_command(slot, key) if target_c is None
                   else {"type": mapping["control_type"], "target_c": target_c})
        if command["type"] == "unavailable":
            # A plan that cannot drive the device releases nothing; only the select does.
            # Positive availability observations can recover a known missing
            # entity; reading controls never executes this unavailable command.
            entities = set(actuator_targets(mapping))
            entities.update(value for key, value in mapping.items()
                if key in ("power", "temperature_entity_id") and isinstance(value, str))
            available = []
            for entity in sorted(entities):
                state = self.observed_state(entity)
                if state is not None and state.state not in ("unknown", "unavailable"):
                    available.append(entity)
            return {"state": "unsupported", "reason": self.holding(device, command["reason"]),
                    "execution_available_entities": available}
        kind = mapping["control_type"]
        room_relay = command["type"] == "room_relay"
        if room_relay:
            if (kind != "switch_schedule" or command["room_key"] != mapping.get("room_area_id")
                    or not heating_binding(options, device)
                    or not any(m["key"] == key and m.get("category") == "heating" for m in models)):
                raise ControlPlanError("The planned room no longer matches the heater's reviewed room and sensor")
        elif command["type"] != kind:
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
        if suspend_heat:
            return await self.pause_thermostat(device, options, targets[0])
        model = next(m for m in models if m["key"] == key)
        charge_setback = (target_c is None and kind == 'setpoint' and slot.get('charge_setback')
            and targets[0].startswith('climate.') and model.get('category') == 'heating'
            and device_planning_path(model) == 'room')
        if record and (entity := record.get('heating_suspended')):
            state = self.actuator_state(entity)
            if state.state == 'off':
                if charge_setback:
                    step = finite(state.attributes.get('target_temp_step', 0.1))
                    origin = finite(state.attributes['min_temp'])
                    planned = rounded_setpoint(command['target_c'], origin, step)
                    if self.paused_room_is_warm(entity, state, planned):
                        # A paused thermostat already cannot start. Leave it
                        # paused until the room calls for heat or the slot changes.
                        return {'state': 'commanded', 'reason': 'Idle thermostat remains paused during planned charging',
                                'decision': {'kind': 'setpoint', 'heating': False, 'planned_target_c': planned}}
                await self.command(entity, 'heat')
                if not self.matches(entity, 'heat'):
                    raise HeatingDeferred('Resuming scheduled heating')
        commanded = dict(record.get("last_commands", {})) if record else {}
        for entity in commanded:
            # Unavailable is unknown, never an external change.
            self.actuator_state(entity)
        # SHS owns a Controlling device (user requirement, 18 October 2026): a
        # setting changed outside SHS is switched back by the commands below, in
        # the evaluation the actuator's own state change starts. It never
        # suspends control or hands the device back.
        outside = {entity: self.state(entity).state for entity, value in commanded.items()
                   if not self.matches(entity, value)}
        values = {}
        demand = None
        water = is_hot_water(model)
        if water:
            if hot_water_setup_errors(mapping):
                raise HotWaterConfigurationError(mapping, key)
            demand = self.watts(mapping["power"])
        if kind == "setpoint":
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
                values[entity] = rounded_setpoint(command["target_c"], origin, step)
            if "room_target_c" in command and self.shed_scheduled_heat(targets[0], self.actuator_state(targets[0])):
                await self.pause_thermostat(device, options, targets[0], accepted=values[targets[0]])
                return self.scheduled_heat_limited()
        else:
            if room_relay or (target_c is not None and kind == "switch_schedule"):
                sensor = mapping["temperature_entity_id"]
                measured = self.state(sensor)
                try:
                    temperature = room_temperature(sensor, measured.state, measured.attributes, self.inputs.temperature_unit())
                except (TypeError, ValueError) as error:
                    raise ControlObservationError(str(error), sensor, 'Check the configured room temperature sensor.') from error
                state = self.actuator_state(targets[0])
                target = command["target_c"]
                on = relay_heat(temperature, target, state.state) == 'on'
                if room_relay and self.shed_scheduled_heat(targets[0], state) and on:
                    await self.capture(device, options, targets)
                    await self.command(targets[0], 'off')
                    return self.scheduled_heat_limited()
            else:
                on = command["permitted"] if kind == "permit_inhibit" else command["on_seconds"] == 900
            if water:
                on = self.heating_permission_for(mapping,commanded,command["permitted"],demand)
            values = {entity: "on" if on else "off" for entity in targets}
        reverted = {entity: state for entity, state in outside.items()
                    if target_c is None and entity in values and not self.matches(entity, values[entity])}
        if reverted and not self.verifying:
            _LOGGER.warning("%s: switching back %s, changed outside SHS", device,
                            ", ".join(f"{entity} ({state})" for entity, state in reverted.items()))
        record = await self.capture(device, options, targets)
        for entity, value in values.items():
            await self.command(entity, value)
        record.pop('charge_setback_c', None)
        record.pop('heating_suspended', None)
        record.pop('heating_pause', None)
        await self.save()
        if kind == "setpoint":
            decision = {"kind": kind, "target_c": sorted(set(values.values()))}
        elif kind == "switch_schedule":
            decision = {"kind": kind, "on": on}
        else:
            decision = {"kind": kind, "permitted": on}
            if demand is not None:
                decision.update(heater_power_w=demand, planned_permitted=command["permitted"], finishing_heating=on and not command["permitted"])
        result = {"state": "commanded", "reason": "actuator targets acknowledged; delivered heat or power is not inferred",
                  "decision": decision}
        if reverted:
            result.update(reason="Switched back after a change outside SHS; " + result["reason"],
                          reverted_external_change=reverted)
        return result

    @staticmethod
    def scheduled_heat_limited():
        return {"state": "limited", "reason": "Scheduled heating paused for the house import limit",
                "heating_curtailed": True, "retry_automatically": True}

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
                if 'pool_permitted' in slot and pool_permission(plan, slot) is None:
                    previews["pool"] = {"fields": [{"label": "Pool heater", "value": "Existing settings retained"}]}
                    return previews
                request, _ = self.pool_request(options, slot, read_state=self.preview_state, plan=plan)
                fields = [{"label": "Pool heater", "value": request["requested_switch_state"].capitalize()},
                          {"label": "Water temperature", "value": request["water_temperature_c"], "unit": "°C"},
                          {"label": "Stop at", "value": request["stop_temperature_c"], "unit": "°C"}]
                previews["pool"] = {"fields": fields, "basis": "current_readings"}
            except (KeyError, TypeError, ValueError) as err:
                previews["pool"] = {"error": str(err)}
        return previews
