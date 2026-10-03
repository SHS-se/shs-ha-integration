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
from types import SimpleNamespace
from time import perf_counter
from uuid import uuid4

from .command_journal import Command
from .native_commands import finite, native_command
from .device_ownership import DeviceOwnership, decode_ownership
from .api_contract import INTEGRATION_VERSION
from .operating_modes import device_mode, EXECUTING_MODES
from .minimum_run import RunStateUnavailable, minimum_run_errors
from .verification import OPERATIONS, operation_name, evaluation_record, observation
from .controller_metrics import ControllerMetrics, fingerprint, record_time
from .battery_commands import validate_battery_command, battery_mode_key
from .configuration_values import resolve_battery_quantities, resolve_quantity
from .device_commands import actuator_targets, execution_setup_errors, validate_commands
from .device_controls import battery_control_errors, pool_control_errors, pool_control_mapping, mapped_planning_path, planning_path

_LOGGER = logging.getLogger(__name__)
from .device_operations import (DeviceOperations, ControlObservationError, ActuatorUnavailableError,
    PlanChangedError, ControlDeadlineError, correction_details, checked_state, battery_limit_value,
    DEVICES, CONFIRM_SECONDS, BATTERY_MAX_AGE_SECONDS, POOL_MAX_AGE_SECONDS, EV_MAX_AGE_SECONDS,
    OBSERVATION_GRACE_SECONDS, EXTERNAL_CHANGE, INHIBIT_LIMIT)


class ScheduledController(DeviceOperations):
    """HA adapter supplied by the caller, so execution is behaviour-testable."""

    def __init__(self, inputs, coordinator, store, options, verification=None, *, native_executor, devices=None):
        self.devices = devices
        self.native_executor = native_executor
        self.verification = verification
        self.verifying = False
        self.verification_commands = []
        self.shadow = {}
        self.verification_observations = {}
        self.inputs = inputs
        self.coordinator = coordinator
        self.ownership = DeviceOwnership(store)
        self.options = options
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
        self.requested_types = {}
        self.requested_systems = set()
        self.requested_error = None
        self.metrics = ControllerMetrics(INTEGRATION_VERSION)
        self.scheduler = None
        self.observation_changed = asyncio.Event()
        self.diagnostic_evaluation = None
        self.diagnostics_error = None
        self.diagnostics_failed_evaluations = 0
        self.diagnostics_sampling_error = None
        self.diagnostics_failed_samples = 0

    def adopt_ownership(self, value):
        (self.ownership.records, self.ownership.overrides,
         self.ownership.retired_pool_temperature_settings, self.ownership.runs) = decode_ownership(value)

    async def _physical(self, device, operation, options, slot=None):
        plan, _ = self.coordinator.binding_plan_for(device, options)
        response = await self.devices.perform(device, operation, plan, slot)
        self.adopt_ownership(response['ownership'])
        for deadline in response['deadlines']:
            if self.scheduler:
                self.scheduler.device_deadline(deadline['device'], deadline['name'],
                    datetime.fromisoformat(deadline['at']) if deadline['at'] else None)
        for entity in response['reads']:
            self.observed_state(entity)
        if self.diagnostic_evaluation is not None:
            self.diagnostic_evaluation['commands'].extend(response['diagnostics']['commands'])
            self.diagnostic_evaluation['observations'].update(response['diagnostics']['observations'])
        error = response['error']
        if error:
            if error['kind'] == 'deadline':
                raise ControlDeadlineError(error['message'])
            if error['kind'] == 'observation':
                exception = ControlObservationError(error['message'], error['entity'],
                    error['details'].get('next_step', ''), unavailable=error['unavailable'], stale=error['stale'])
                exception.details = error['details']
                raise exception
            raise ValueError(error['message'])
        return response['result']

    async def execute_pool(self, options, slot):
        if self.devices is not None and not self.verifying:
            return await self._physical('pool', 'apply', options, slot)
        return await super().execute_pool(options, slot)

    async def execute_ev(self, options, slot):
        if self.devices is not None and not self.verifying:
            return await self._physical('ev', 'apply', options, slot)
        return await super().execute_ev(options, slot)

    async def execute_device(self, device, options, slot):
        if self.devices is not None and not self.verifying:
            return await self._physical(device, 'apply', options, slot)
        return await super().execute_device(device, options, slot)

    async def restore(self, device):
        if self.devices is not None and not self.verifying:
            return await self._physical(device, 'release', self.options())
        return await super().restore(device)

    async def hold_inhibit_limit(self, device, options):
        if self.devices is not None and not self.verifying:
            return await self._physical(device, 'inhibit', options)
        return await super().hold_inhibit_limit(device, options)

    async def minimum_run_snapshot(self, options, models):
        if self.devices is not None:
            return await self.devices.minimum_run_snapshot(options, models)
        return await super().minimum_run_snapshot(options, models)

    async def save(self):
        if self.devices is not None and not self.verifying:
            raise RuntimeError('Physical ownership is written only by the HA gateway')
        await super().save()

    def command_identity(self):
        return "controller:" + uuid4().hex

    def execution_plan(self):
        return self.coordinator.optimisation_plan

    def binding_execution_plan(self, device, options):
        return self.coordinator.binding_plan_for(device, options)

    def before_external_command(self, device):
        runtime = getattr(self, "battery_runtime", None)
        return runtime is None or runtime.before_external_command(device)

    def add_listener(self, listener, reports=None):
        """`reports` selects the status keys a listener shows; None receives all."""
        subscription = (listener, reports)
        self.listeners.add(subscription)
        return lambda: self.listeners.discard(subscription)

    def report(self, device, state, **details):
        if self.verifying:
            return
        if device == "pool" and self.ownership.retired_pool_temperature_settings:
            details["retired_temperature_settings"] = deepcopy(self.ownership.retired_pool_temperature_settings)
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
        if device in self.ownership.overrides:
            return {"state": "overridden", "reason": self.ownership.overrides[device]}
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
        return f"{reason}; holding the last setting SHS sent" if device in self.ownership.records else reason

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
            purpose = "hand it back" if self.ownership.records.get(device, {}).get("restoration_pending") else "resume the plan"
        hold = "SHS holds the last setting it sent and will" if device in self.ownership.records else "SHS will"
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





    async def verify(self, device, options, slot, plan):
        if self.verification is None:
            raise ValueError("verification journal is unavailable")
        kind = device if device in DEVICES else options["device_control_mappings"][device.removeprefix("device:")]["control_type"]
        expected = OPERATIONS.get(kind, ())
        if not expected:
            raise ValueError("no executable operation catalogue for this device")
        attempt = evaluation_record(device, "control_verification", options, slot, plan, INTEGRATION_VERSION, expected)
        owned, overrides = self.ownership.records, self.ownership.overrides
        self.ownership.records, self.ownership.overrides = {}, deepcopy(overrides)
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
        except ControlDeadlineError as err:
            attempt.update(outcome="verified", reason=str(err),
                           result={"state": "limited", "reason": str(err)})
        except Exception as err:
            attempt["reason"] = str(err)
            attempt.update(correction_details(err))
            if isinstance(err, ControlObservationError) and err.transient:
                gap = err
        finally:
            attempt["commands"] = self.verification_commands
            attempt["observations"] = self.verification_observations
            self.verifying = False
            self.ownership.records, self.ownership.overrides = owned, overrides
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
            ownership_before=deepcopy(self.ownership.records.get(device)),
            status_before=deepcopy(self.status.get(device)))

    async def finish_diagnostic_evaluation(self):
        attempt, self.diagnostic_evaluation = self.diagnostic_evaluation, None
        if attempt is None:
            return
        device = attempt["device"]
        attempt.update(completed_at=datetime.now(timezone.utc).isoformat(),
            outcome="evaluated", result=deepcopy(self.status.get(device, {})),
            ownership_after=deepcopy(self.ownership.records.get(device)),
            override=self.ownership.overrides.get(device), failure_latched=device in self.failed)
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
        from .controller_observations import diagnostic_inventory
        from .presentation import equipment_present
        from .operating_modes import system_device_keys
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
                await self.verification.sample(options, rows, self.inputs.read,
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
            if self.devices is not None:
                self.adopt_ownership(await self.devices.ownership())
            else:
                await self.ownership.load()
        except Exception as err:
            for device in DEVICES:
                self.report(device, "fault", reason=f"cannot load restoration journal: {err}")
            return
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
            generic.update(key for key in self.ownership.records if key.startswith("device:"))
            if self.scheduler is not None:
                self.scheduler.retain_devices(set(DEVICES) | generic)
            requested = []
            previous_authority = (self.requested_types, self.requested_systems, self.requested_error)
            if generic or any(device_mode(options, d) in EXECUTING_MODES for d in DEVICES) or self.ownership.records:
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
            if self.devices is not None:
                self.adopt_ownership(await self.devices.synchronize(requested))
            else:
                for device in tuple(self.ownership.overrides):
                    # Leaving Controlling on the select clears an external-change latch.
                    if device_mode(options, device) != "controlling":
                        del self.ownership.overrides[device]
                        await self.save()
                self.ownership.runs.configure(options, requested, datetime.now(timezone.utc))
                self.ownership.runs.observe(self.inputs.read, datetime.now(timezone.utc))
                if self.ownership.runs.dirty:
                    await self.save()
            if self.scheduler is not None:
                self.scheduler.watch_runs()
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
                    "override": self.ownership.overrides.get(device),
                    "ownership": self.ownership.records.get(device), "failed": self.failed.get(device),
                })
                self.begin_diagnostic_evaluation(device, options, slot, plan, trigger)
                try:
                    record = self.ownership.records.get(device)
                    inactive = self.inactive_status(device, options)
                    if record and (inactive or device_mode(options, device) != "controlling"):
                        # The only handover: the select (Verification included), or a
                        # setting that removes the device from it. Nothing else hands a
                        # device back, and Verification must not stop doing so; the
                        # history of both mistakes is in docs/control-continuity.md.
                        await self.restore(device)
                        # The next attempt starts from the handed-back device, not the failed one.
                        self.failed.pop(device, None)
                    elif self.devices is None and record and record.get("restoration_pending"):
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
                if self.devices is None and self.ownership.runs.dirty:
                    await self.save()
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
