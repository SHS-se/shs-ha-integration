"""Unscheduled physical device transactions with one local ownership writer.

The app selects a semantic intention. This owner resolves canonical bindings,
rechecks permission at each native dispatch and retains physical obligations.
No cloud client, household coordinator, accounting archive or policy loop lives here.
"""
import asyncio
import sqlite3
from copy import deepcopy
from dataclasses import dataclass
from uuid import uuid4

from .command_journal import ObligationCommand
from datetime import datetime, timedelta, timezone

from .api_contract import INTEGRATION_VERSION
from .controller_metrics import ControllerMetrics
from .device_operations import (DeviceOperations, ControlDeadlineError, ControlObservationError, ControlPlanError, ControlCommandError, PowerHistoryError,
                                correction_details, HeatingDeferred)
from .heating_overrides import active_heating
from .device_ownership import DeviceOwnership
from .operating_modes import device_mode
from .hot_water import HotWaterConfigurationError, is_hot_water
from .device_commands import actuator_targets
from .device_controls import pool_control_mapping
from .grid_capacity import EV_WATTS_PER_AMP
from .gateway_journal import AdmissionPending, GatewayConflict, GatewayRejected, JournalStorageError
from .command_transport import CommandUncertain


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
                result.operation not in ('apply', 'release', 'heat', 'curtail') or
                type(result.parameters) is not dict or type(result.headroom_reserved) is not bool or
                any(type(v) is not int or v < 0 for v in (result.configuration_revision, result.policy_revision))):
            raise ValueError('Invalid device intention')
        return result


class DeviceGateway(DeviceOperations):
    def __init__(self, inputs, options, ownership_store, native_executor, *, authorize, before_external, operations, capacity=None, heating=None):
        self.inputs, self.options = inputs, options
        self.operations = operations
        self.capacity = capacity
        self.heating = heating
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
        self.heating_limit = None
        self.heating_needs_capacity = False

    def before_external_command(self, device, *, entity, value):
        if not self.local_obligation and self.intent.operation == 'heat':
            try:
                if self.external_ready(device, entity=entity, value=value, priority=True):
                    return True
            except ControlDeadlineError:
                pass
            self.heating_needs_capacity = True
            self.device_deadline('heating_capacity', datetime.now(timezone.utc) + timedelta(seconds=5))
            raise HeatingDeferred('Making room for the heating override')
        if not self.local_obligation and not self.intent.headroom_reserved:
            if self.heating is not None and self.capacity.heating_effects():
                self.capacity.admit_import(self.capacity.describe_import(device, entity, value))
            self.reservation_required = True
            self.device_deadline("battery_headroom", datetime.now(timezone.utc)+timedelta(seconds=5))
            return False
        try:
            return self.external_ready(device, entity=entity, value=value)
        except (ControlDeadlineError, PowerHistoryError):
            self.device_deadline("battery_headroom", datetime.now(timezone.utc)+timedelta(seconds=5))
            raise

    async def load(self):
        await self.ownership.load()

    def execution_plan(self):
        return self.plan

    def binding_execution_plan(self, device, options):
        if device != self.device or options != self.active_options:
            raise GatewayConflict('Device operation changed')
        return self.plan, self.slot

    def observed_state(self, entity):
        if entity:
            self.reads.add(entity)
        return super().observed_state(entity)

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
            if not self.restoring:
                raise GatewayConflict('Only restoration and maximum-inhibit obligations run locally')
            return
        self.authorize(self.intent)
        if self.closed or self.options() != self.active_options:
            raise AdmissionPending('Canonical device configuration changed')
        mode = device_mode(self.options(), self.device)
        # Verification writes no schedule, but leaving Controlling for it is the
        # release and its handover is a real write (docs/control-continuity.md).
        if not self.restoring and mode != 'controlling':
            raise GatewayConflict('Local device control permission is absent')
        if self.restoring:
            return
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
        if self.heating is not None:
            active = active_heating(self.heating.value, options, self.device, self.heating.now())
            if self.intent.operation == 'heat':
                p = self.intent.parameters
                if not active or (active['revision'], active['target_c']) != (p['revision'], p['target_c']):
                    raise HeatingDeferred('Heating target changed')
            elif self.intent.operation in ('apply', 'curtail'):
                if active:
                    raise HeatingDeferred('Following the heating override')
                if (self.intent.operation == 'curtail' and self.heating_limit is None
                        or self.heating_limit and not self.capacity.heating_cap_current(self.heating_limit)):
                    raise HeatingDeferred('Heating priority changed')
        if self.intent.operation == 'apply':
            start = datetime.fromisoformat(self.slot['start'].replace('Z', '+00:00'))
            if not start <= datetime.now(timezone.utc) < start+timedelta(minutes=15):
                raise GatewayConflict('Binding device slot has ended or not started')

    def _decode(self, intent):
        """Convert the finite semantic payload to the shared operation inputs."""
        p = intent.parameters
        if intent.operation == 'heat':
            if (set(p) != {'revision', 'target_c', 'models'} or not intent.device.startswith('device:')
                    or type(p['revision']) is not int or type(p['target_c']) not in (int, float)
                    or not any(model['key'] == intent.device.removeprefix('device:')
                               and model.get('category') == 'heating' and model.get('control_type') == 'setpoint'
                               for model in p['models'])):
                raise ValueError('Invalid room heating intention')
            return {'device_models': p['models']}, None
        if intent.operation == 'curtail':
            if set(p) != {'models'} or type(p['models']) is not list:
                raise ValueError('Invalid heating relief intention')
            return {'device_models': p['models']}, None
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
            if set(p) != {'start', 'heating_w', 'pool_permitted', 'stop_temperature_c', 'models'}:
                raise ValueError('Invalid pool intention')
            if type(p['pool_permitted']) is not bool:
                raise ValueError('pool permission must be a boolean')
            return {'device_models':p['models'], 'pool':{'stop_temperature_c':p['stop_temperature_c']}}, dict(start=p['start'], pool_w=p['heating_w'], pool_permitted=p['pool_permitted'])
        if intent.device.startswith('device:'):
            key = intent.device.removeprefix('device:')
            if set(p) != {'start', 'commands', 'models'} or key not in p['commands']:
                raise ValueError('Invalid mapped device intention')
            return {'device_models':p['models']}, dict(start=p['start'], device_commands=p['commands'])
        raise ValueError('Battery control requires an admitted native transition')

    def capacity_reduction_available(self, owner, value):
        """Expose existing physical run protections without selecting a schedule."""
        if owner in ('ev','pool'):
            return True
        if value is None:
            return False
        key = owner.removeprefix('device:')
        model = next((m for m in value['parameters'].get('models',[]) if m['key']==key),None)
        record = self.ownership.records.get(owner)
        if model is None or record is None:
            return False
        if not is_hot_water(model):
            return True
        mapping = self.options().get('device_control_mappings',{}).get(key,{})
        try:
            return not self.heating_permission_for(mapping,record.get('last_commands',{}),False,self.watts(mapping['power']))
        except (ValueError,KeyError,TypeError,IndexError):
            return False  # Unknown physical evidence cannot promise relief.

    async def synchronize(self, models):
        """Canonical mode transitions and run clocks, without executing a plan."""
        async with self.lock:
            options = self.options()
            changed = False
            for device, record in tuple(self.ownership.records.items()):
                if device == 'battery' and device_mode(options, device) == 'control_verification':
                    # A pre-runtime battery record holds no settings to return:
                    # the admitted battery adapter performs that handover.
                    del self.ownership.records[device]
                    changed = True
                elif device_mode(options, device) == 'controlling' and record.pop('restoration_pending', None) is not None:
                    changed = True
            recoveries = await self.capacity.capacity_recoveries() if self.capacity is not None else []
            return {**self.ownership.snapshot(), 'capacity_recoveries':recoveries}

    def make_command(self, identity, phase, action):
        if self.local_obligation:
            return ObligationCommand(identity, 'controller', self.device, phase, action)
        return super().make_command(identity, phase, action)

    async def maintain_obligations(self):
        """Finish explicitly requested restoration offline.

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
                if not release:
                    continue
                self.intent = DeviceIntent(uuid4().hex,device,'release',0,0,{},True)
                self.device = device
                self.sequence = 0
                self.local_obligation = True
                try:
                    if self.heating is not None:
                        await self.heating.retire(self.active_options, released=device)
                    await self.restore(device)
                except Exception as error:
                    self.report(device,'pending',reason=str(error),**correction_details(error))
                finally:
                    self.local_obligation = False
                    self.restoring = False
    async def curtail(self):
        owner, cap, options = self.device, self.heating_limit, self.active_options
        record = self.ownership.records[owner]
        if owner == 'ev':
            switch = options['ev_charge_switch_entity']
            entity, _ = self.capacity.flexible_ev(options, record)
            self.check_targets(owner, [switch, entity])
            amps = cap['maximum_w'] / EV_WATTS_PER_AMP
            if amps == 0:
                await self.command(switch, 'off')
            elif self.actuator_state(switch).state == 'on' and self.number(entity) > amps:
                await self.command(entity, amps)
        elif owner == 'pool':
            _, mapping = pool_control_mapping(options, self.plan['device_models'])
            targets = actuator_targets(mapping)
            self.check_targets(owner, targets)
            await self.command(targets[0], 'off')
        else:
            key = owner.removeprefix('device:')
            mapping = options['device_control_mappings'][key]
            targets = actuator_targets(mapping)
            self.check_targets(owner, targets)
            if cap.get('thermostat'):
                return await self.suspend_thermostat(owner, options, targets[0])
            if mapping['control_type'] not in ('switch_schedule', 'permit_inhibit'):
                raise HeatingDeferred('Device no longer offers heating capacity')
            model = next((item for item in self.plan['device_models'] if item['key'] == key), {})
            if is_hot_water(model) and self.heating_permission_for(
                    mapping, record.get('last_commands', {}), False, self.watts(mapping['power'])):
                raise HeatingDeferred('Hot water is completing its active cycle')
            await self.command(targets[0], 'off')
        return {'state': 'manual', 'reason': 'Making room for a heating override'}

    async def perform(self, value):
        intent = DeviceIntent.parse(value)
        response = await self._perform(intent, value)
        if not response.get('heating_needs_capacity'):
            return response

        self.authorize(intent)
        if await self.capacity.make_heating_room(value):
            return {**response, 'reservation_required': True}
        # A reducer may report immediately. Recheck the native guard in this
        # event, and rely on the observation wake if the watts are still pending.
        retry = {**value, 'request_id': uuid4().hex}
        return await self._perform(DeviceIntent.parse(retry), retry)

    async def _perform(self, intent, value):
        async with self.lock:
            self.intent, self.device = intent, intent.device
            self.plan, self.slot = self._decode(intent)
            self.active_slot = self.slot
            self.active_options = deepcopy(self.options())
            self.authorize(intent)
            effective = await self.capacity.capacity_intention(value) if self.capacity is not None else value
            self.heating_limit = (self.capacity.heating_cap(intent.device)
                                  if self.heating is not None and intent.operation in ('apply', 'curtail') else None)
            self.plan, self.slot = self._decode(DeviceIntent.parse(effective))
            self.active_slot = self.slot
            prior = await self.operations.begin(value)
            if prior['state'] == 'completed':
                return {**prior['result'], 'ownership':self.ownership.snapshot()}
            if prior['state'] != 'new':
                raise GatewayConflict('Interrupted device operation; reconcile physical observations before a new intention')
            self.sequence = 0
            self.reservation_required = False
            self.heating_needs_capacity = False
            self.reads = set()
            self.reports = {}
            self.deadlines = {}
            self.diagnostic_evaluation = {'commands': [], 'observations': {}}
            error, result = None, None
            try:
                if intent.operation == 'release':
                    self.restoring = True
                    self.check_authority()
                    if self.heating is not None:
                        await self.heating.retire(self.active_options, released=intent.device)
                    await self.restore(intent.device)
                else:
                    self.check_authority()
                    if intent.operation == 'curtail':
                        result = await self.curtail()
                    elif intent.device == 'pool':
                        result = await self.execute_pool(self.active_options, self.slot)
                    elif intent.device == 'ev':
                        result = await self.execute_ev(self.active_options, self.slot)
                    elif intent.operation == 'heat':
                        result = await self.execute_device(intent.device, self.active_options, None,
                                                           target_c=intent.parameters['target_c'])
                        result.update(state='manual', reason='Following the heating override')
                    else:
                        cap = self.heating_limit
                        result = await self.execute_device(intent.device, self.active_options, self.slot,
                                                           suspend_heat=bool(cap and cap.get('thermostat')))
            except Exception as exception:
                # In this owner transaction a permission change is a refused
                # command, not a lost app session. Durable corruption still faults it.
                if isinstance(exception, (JournalStorageError, sqlite3.DatabaseError)):
                    raise
                error = {'message':str(exception), 'kind':'heating' if isinstance(exception, HeatingDeferred) else 'plan' if isinstance(exception, ControlPlanError) else 'deadline' if isinstance(exception, ControlDeadlineError) else 'observation' if isinstance(exception, ControlObservationError) else 'configuration' if isinstance(exception, HotWaterConfigurationError) else 'admission' if isinstance(exception, AdmissionPending) else 'rejected' if isinstance(exception, (GatewayConflict, GatewayRejected, ControlCommandError, CommandUncertain)) else 'internal',
                         'details':correction_details(exception)}
                if isinstance(exception, ControlObservationError):
                    error.update(entity=exception.entity, unavailable=exception.unavailable)
            finally:
                self.restoring = False
            if result is not None and self.capacity is not None:
                if self.heating_limit:
                    result.update(heating_curtailed=True, reason='Making room for a heating override')
                condition = self.capacity.capacity_condition(value, effective)
                if condition is not None and result.get('execution_condition') is None:
                    result['capacity_curtailed'] = True
                    result['execution_condition'] = condition
            response = dict(result=result, error=error, reservation_required=self.reservation_required, ownership=self.ownership.snapshot(),
                        reports=self.reports, reads=sorted(self.reads),
                        deadlines=[{'device':d,'name':n,'at':at} for (d,n),at in self.deadlines.items()],
                        diagnostics=deepcopy(self.diagnostic_evaluation))
            if self.heating_needs_capacity:
                response['heating_needs_capacity'] = True
            await self.operations.finish(value, response)
            if intent.device == 'pool':
                self.pool_pause = (dict(configuration_revision=intent.configuration_revision,
                    policy_revision=intent.policy_revision, start=self.slot['start'],
                    entity=result['control_entity'])
                    if error is None and result and intent.operation == 'apply'
                    and result['requested_power_w'] == 0 and result['requested_switch_state'] == 'off'
                    else None)
            return response
