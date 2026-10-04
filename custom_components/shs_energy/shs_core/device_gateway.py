"""Unscheduled physical device transactions with one local ownership writer.

The app selects a semantic intention. This owner resolves canonical bindings,
rechecks permission at each native dispatch and retains physical obligations.
No cloud client, household coordinator, accounting archive or policy loop lives here.
"""
import asyncio
from copy import deepcopy
from dataclasses import dataclass
from uuid import uuid4

from .command_journal import ObligationCommand
from datetime import datetime, timedelta, timezone

from .api_contract import INTEGRATION_VERSION
from .controller_metrics import ControllerMetrics
from .device_operations import (DeviceOperations, ControlDeadlineError, ControlObservationError,
                                correction_details, EXTERNAL_CHANGE)
from .device_ownership import DeviceOwnership
from .operating_modes import device_mode
from .gateway_journal import GatewayConflict


@dataclass(frozen=True)
class DeviceIntent:
    """A closed device operation; entity/service names come from HA configuration."""
    request_id: str
    device: str
    operation: str
    configuration_revision: int
    policy_revision: int
    parameters: dict
    headroom_reserved: bool

    @classmethod
    def parse(cls, value):
        if type(value) is not dict or set(value) != set(cls.__dataclass_fields__):
            raise ValueError('Invalid device intention')
        result = cls(**deepcopy(value))
        if (type(result.request_id) is not str or not result.request_id or
                type(result.device) is not str or not result.device or
                result.operation not in ('apply', 'release', 'inhibit') or
                type(result.parameters) is not dict or type(result.headroom_reserved) is not bool or
                any(type(v) is not int or v < 0 for v in (result.configuration_revision, result.policy_revision))):
            raise ValueError('Invalid device intention')
        return result


class DeviceGateway(DeviceOperations):
    def __init__(self, inputs, options, ownership_store, native_executor, *, authorize, before_external, operations):
        self.inputs, self.options = inputs, options
        self.operations = operations
        self.ownership = DeviceOwnership(ownership_store)
        self.native_executor = native_executor
        self.authorize, self.external_ready = authorize, before_external
        self.reservation_required = False
        self.lock = asyncio.Lock()
        self.observation_changed = asyncio.Event()
        self.battery_writer_fence = None
        self.verifying = False
        self.closed = False
        self.restoring = False
        self.shadow = {}
        self.verification_observations = {}
        self.verification_commands = []
        self.command_times = {}
        self.metrics = ControllerMetrics(INTEGRATION_VERSION)
        self.scheduler = None
        self.diagnostic_evaluation = None
        self.intent = None
        self.plan = None
        self.slot = None
        self.active_options = None
        self.deadlines = {}
        self.reports = {}
        self.reads = set()
        self.sequence = 0
        self.local_obligation = False
        self.pool_pause = None

    def before_external_command(self, device):
        if self.local_obligation:
            return self.external_ready(device)
        if not self.intent.headroom_reserved:
            self.reservation_required = True
            self.device_deadline("battery_headroom", datetime.now(timezone.utc)+timedelta(seconds=5))
            return False
        return self.external_ready(device)

    async def load(self):
        await self.ownership.load()

    def execution_plan(self):
        return self.plan

    def binding_execution_plan(self, device, options):
        if device != self.device or options != self.active_options:
            raise GatewayConflict('Device operation changed')
        return self.plan, self.slot

    def observed_state(self, entity, *, max_age=None):
        if entity:
            self.reads.add(entity)
        return super().observed_state(entity, max_age=max_age)

    def report(self, device, state, **details):
        self.reports[device] = {'state': state, **details}

    def holding(self, device, reason):
        return f'{reason}; holding the last setting SHS sent' if device in self.ownership.records else reason

    def device_deadline(self, name, when):
        self.deadlines[(self.device, name)] = when.isoformat() if when else None

    def command_identity(self):
        self.sequence += 1
        return f'device:{self.intent.request_id}:{self.sequence}'

    def check_authority(self):
        if self.local_obligation:
            if self.closed or self.options() != self.active_options:
                raise GatewayConflict('Physical obligation permission changed')
            if not self.restoring and self.intent.operation != 'inhibit':
                raise GatewayConflict('Only restoration and maximum-inhibit obligations run locally')
            return
        self.authorize(self.intent)
        if self.closed or self.options() != self.active_options:
            raise GatewayConflict('Canonical device configuration changed')
        mode = device_mode(self.options(), self.device)
        # Verification writes no schedule, but leaving Controlling for it is the
        # release and its handover is a real write (docs/control-continuity.md).
        if not self.restoring and mode != 'controlling':
            raise GatewayConflict('Local device control permission is absent')
        if self.restoring:
            return
        if self.device in self.ownership.overrides:
            raise GatewayConflict(self.ownership.overrides[self.device])
        options = self.options()
        if self.device.startswith('device:'):
            key = self.device.removeprefix('device:')
            mapping = options.get('device_control_mappings', {}).get(key, {})
            excluded = key in options.get('excluded_device_readings', [])
            override = mapping.get('control_override_entity')
        else:
            excluded = '$'+self.device in options.get('excluded_device_readings', []) or not options.get(self.device+'_enabled', True)
            override = options.get(self.device+'_control_override_entity')
        global_override = options.get('control_override_entity')
        if excluded or (override and self.state(override).state != 'off') or (global_override and self.state(global_override).state != 'off'):
            raise GatewayConflict('Device is excluded, disabled or manually overridden')
        if self.intent.operation == 'apply':
            start = datetime.fromisoformat(self.slot['start'].replace('Z', '+00:00'))
            if not start <= datetime.now(timezone.utc) < start+timedelta(minutes=15):
                raise GatewayConflict('Binding device slot has ended or not started')

    def _decode(self, intent):
        """Convert the finite semantic payload to the shared operation inputs."""
        p = intent.parameters
        if intent.operation != 'apply':
            if p:
                raise ValueError('Release/inhibit do not take native settings')
            return {'device_models': []}, None
        if intent.device == 'ev':
            required = {'start', 'current_a', 'minimum_a', 'maximum_a', 'models'}
            if set(p) != required:
                raise ValueError('Invalid EV intention')
            return {'device_models': p['models']}, dict(start=p['start'], ev_target_current_a=p['current_a'],
                ev_min_current_a=p['minimum_a'], ev_max_current_a=p['maximum_a'], device_loads_w={m['key']:0 for m in p['models']})
        if intent.device == 'pool':
            if set(p) != {'start', 'heating_w', 'stop_temperature_c', 'models'}:
                raise ValueError('Invalid pool intention')
            return {'device_models':p['models'], 'pool':{'stop_temperature_c':p['stop_temperature_c']}}, dict(start=p['start'], pool_w=p['heating_w'])
        if intent.device.startswith('device:'):
            key = intent.device.removeprefix('device:')
            if set(p) != {'start', 'commands', 'models'} or key not in p['commands']:
                raise ValueError('Invalid mapped device intention')
            return {'device_models':p['models']}, dict(start=p['start'], device_commands=p['commands'])
        raise ValueError('Battery control requires an admitted native transition')

    async def synchronize(self, models):
        """Canonical mode transitions and run clocks, without executing a plan."""
        async with self.lock:
            options = self.options()
            changed = False
            for device in tuple(self.ownership.overrides):
                if device_mode(options, device) != 'controlling':
                    del self.ownership.overrides[device]
                    changed = True
            for device, record in tuple(self.ownership.records.items()):
                if device == 'battery' and device_mode(options, device) == 'control_verification':
                    # A pre-runtime battery record holds no settings to return:
                    # the admitted battery adapter performs that handover.
                    del self.ownership.records[device]
                    changed = True
                elif device_mode(options, device) == 'controlling' and record.pop('restoration_pending', None) is not None:
                    changed = True
            return self.ownership.snapshot()

    def make_command(self, identity, phase, action):
        if self.local_obligation:
            return ObligationCommand(identity, 'controller', self.device, phase, action)
        return super().make_command(identity, phase, action)

    async def maintain_obligations(self):
        """Continue captured restoration and maximum-inhibit promises offline.

        No schedule is selected here. A lost app session alone does not restore
        ordinary devices; their captured settings survive a restart as before.
        Leaving Controlling, for Verification too, is a release and is finished
        here when the app is not doing it (docs/control-continuity.md).
        """
        async with self.lock:
            self.active_options = deepcopy(self.options())
            self.plan, self.slot, self.active_slot = {'device_models':[]}, None, None
            for device,record in tuple(self.ownership.records.items()):
                if device == 'battery':
                    continue  # The admitted battery adapter owns its release.
                mode = device_mode(self.active_options,device)
                options = self.active_options
                if device.startswith('device:'):
                    key = device.removeprefix('device:')
                    mapping = options.get('device_control_mappings',{}).get(key,{})
                    excluded = key in options.get('excluded_device_readings',[])
                    override = mapping.get('control_override_entity')
                else:
                    excluded = '$'+device in options.get('excluded_device_readings',[]) or not options.get(device+'_enabled',True)
                    override = options.get(device+'_control_override_entity')
                row = self.inputs.read(override) if override else None
                # Unknown override reports retain the existing setting, matching
                # the controller's source-gap behavior.
                if override and (row is None or row.state in ('unknown','unavailable')):
                    continue
                global_override = options.get('control_override_entity')
                global_row = self.inputs.read(global_override) if global_override else None
                if global_override and (global_row is None or global_row.state in ('unknown','unavailable')):
                    continue
                release = mode != 'controlling' or excluded or bool(override and row.state != 'off') or bool(global_override and global_row.state != 'off')
                if not release and record.pop('restoration_pending', None) is not None:
                    await self.ownership.save()
                self.intent = DeviceIntent(uuid4().hex,device,'release' if release else 'inhibit',0,0,{},True)
                self.device = device
                self.sequence = 0
                self.local_obligation = True
                try:
                    if release:
                        await self.restore(device)
                    else:
                        await self.hold_inhibit_limit(device,options)
                except Exception as error:
                    self.report(device,'pending',reason=str(error),**correction_details(error))
                finally:
                    self.local_obligation = False
                    self.restoring = False
    async def perform(self, value):
        intent = DeviceIntent.parse(value)
        async with self.lock:
            self.intent, self.device = intent, intent.device
            self.plan, self.slot = self._decode(intent)
            self.active_slot = self.slot
            self.active_options = deepcopy(self.options())
            self.authorize(intent)
            prior = await self.operations.begin(value)
            if prior['state'] == 'completed':
                return {**prior['result'], 'ownership':self.ownership.snapshot()}
            if prior['state'] != 'new':
                raise GatewayConflict('Interrupted device operation; reconcile physical observations before a new intention')
            self.sequence = 0
            self.reservation_required = False
            self.reads = set()
            self.reports = {}
            self.deadlines = {}
            self.diagnostic_evaluation = {'commands': [], 'observations': {}}
            error, result = None, None
            try:
                if intent.operation == 'release':
                    self.restoring = True
                    self.check_authority()
                    await self.restore(intent.device)
                else:
                    self.check_authority()
                    if intent.operation == 'inhibit':
                        result = await self.hold_inhibit_limit(intent.device, self.active_options)
                    elif intent.device == 'pool':
                        result = await self.execute_pool(self.active_options, self.slot)
                    elif intent.device == 'ev':
                        result = await self.execute_ev(self.active_options, self.slot)
                    else:
                        result = await self.execute_device(intent.device, self.active_options, self.slot)
            except Exception as exception:
                error = {'message':str(exception), 'kind':'deadline' if isinstance(exception, ControlDeadlineError) else 'observation' if isinstance(exception, ControlObservationError) else 'rejected',
                         'details':correction_details(exception)}
                if isinstance(exception, ControlObservationError):
                    error.update(entity=exception.entity, unavailable=exception.unavailable, stale=exception.stale)
            finally:
                self.restoring = False
            response = dict(result=result, error=error, reservation_required=self.reservation_required, ownership=self.ownership.snapshot(),
                        reports=self.reports, reads=sorted(self.reads),
                        deadlines=[{'device':d,'name':n,'at':at} for (d,n),at in self.deadlines.items()],
                        diagnostics=deepcopy(self.diagnostic_evaluation))
            await self.operations.finish(value, response)
            if intent.device == 'pool':
                self.pool_pause = (dict(configuration_revision=intent.configuration_revision,
                    policy_revision=intent.policy_revision, start=self.slot['start'],
                    entity=result['control_entity'])
                    if error is None and result and intent.operation == 'apply'
                    and result['requested_power_w'] == 0 and result['requested_switch_state'] == 'off'
                    else None)
            return response
