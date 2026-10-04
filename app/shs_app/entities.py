"""Produce native HA entity descriptors and compact, already calculated values."""
from types import SimpleNamespace
from shs_core import entity_views as views
from shs_core.presentation import controller_explanation
from shs_core.operating_modes import device_mode

METADATA = ('name','translation_key','device_class','state_class','native_unit_of_measurement','entity_category','suggested_display_precision')

class EntityContext:
    # Several sensors consume the same derived prices and invoice rows. Share
    # these calculations within this synchronous projection; the next pass
    # creates a fresh context and reads current household inputs again.
    _shared_display_values = frozenset(('grid_prices','total_price_forecast','latest_display_components'))
    def __init__(self,engine):
        self.household=engine.household
        self.entry=SimpleNamespace(entry_id=engine.identity['entry_id'],data=engine.configuration.credentials())
        self.mirror=engine.mirror
    def __getattr__(self,name):
        value=getattr(self.household,name)
        if name in self._shared_display_values:
            setattr(self,name,value)
        return value
    def read_state(self,entity):
        row=self.mirror.report(entity)
        return SimpleNamespace(**row) if row else None


def project_entities(engine,devices):
    context=EntityContext(engine)
    sensors=[cls(context) for cls in (views.ShsSubscriptionSensor,views.ShsTariffStatusSensor,views.ShsGridOperatorSensor,
        views.ShsCurrentGridCostSensor,views.ShsLastPushSensor,views.ShsOptimisationStatusSensor,views.ShsReactiveSurplusSensor)]
    sensors += [cls(context,direction) for cls in (views.ShsGridPriceSensor,views.ShsTotalPriceSensor) for direction in ('import','export')]
    sensors += [views.ShsPlanRequestSensor(context,device) for device in ('boiler','pool','ev')]
    sensors += [views.ShsEvPlanCurrentSensor(context)]
    sensors += [views.ShsControllerSensor(context,device) for device in ('battery','ev','pool','devices')]
    sensors += [views.ShsTariffComponentSensor(context,key,definition) for key,definition in context.tariff_components.items()]
    catalogue=[];values={}
    for sensor in sensors:
        key=sensor._attr_unique_id
        catalogue.append(dict(unique_id=key,**{name:getattr(sensor,'_attr_'+name,None) for name in METADATA}))
        attributes=getattr(sensor,'extra_state_attributes',{})
        # Controller histories and complete runtime documents belong to app downloads.
        attributes={k:v for k,v in attributes.items() if k not in ('battery_runtime','traces','accounting_journal','fault_history')}
        values[key]=dict(value=sensor.native_value,attributes=attributes,available=sensor.available)
    return dict(schema=1,sensors=catalogue,values=values,modes=project_modes(context,devices))


def project_modes(context,devices):
    modes={}
    for device in devices:
        owner=device['permission']['controller_id'];status=context.controller.status.get(owner,{})
        if owner=='battery':status={**status,'battery_runtime':context.battery_runtime.snapshot()}
        _,slot=context.binding_plan_for(owner,context.resolved_options())
        mode=device_mode(context.resolved_options(),owner)
        modes[device['key']]=dict(mode=mode,attributes={'device_key':device['key'],
            'controlling_blocked_reason':device['permission']['reason'],**controller_explanation(owner,mode,status,slot)})
    return modes
