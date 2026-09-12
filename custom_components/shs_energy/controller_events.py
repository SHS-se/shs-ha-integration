"""Connect the local scheduler to entity-scoped HA events and one-shot timers."""
from homeassistant.const import EVENT_CORE_CONFIG_UPDATE, EVENT_STATE_REPORTED
from homeassistant.core import callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_track_point_in_utc_time, async_track_state_change_event

from .controller_scheduler import ControllerScheduler


def attach_controller_events(hass, entry, controller):
    def subscribe(entity, notify):
        @callback
        def changed(event):
            notify(entity, event.data["new_state"], "state_change")

        @callback
        def reported(event):
            notify(entity, event.data["new_state"], "state_report")

        @callback
        def registered(event):
            notify(entity, hass.states.get(entity), "registry")

        @callback
        def matches(data):
            return data.get("entity_id") == entity

        remove = (
            async_track_state_change_event(hass, [entity], changed),
            hass.bus.async_listen(EVENT_STATE_REPORTED, reported, event_filter=matches),
            hass.bus.async_listen(er.EVENT_ENTITY_REGISTRY_UPDATED, registered, event_filter=matches),
        )

        def unsubscribe():
            for cancel in remove:
                cancel()
        return unsubscribe

    def at(when, action):
        return async_track_point_in_utc_time(hass, callback(action), when)

    scheduler = ControllerScheduler(
        controller, subscribe, at,
        lambda work: entry.async_create_background_task(hass, work, name="shs_energy_controller_event"),
    )
    entry.async_on_unload(scheduler.close)

    @callback
    def coordinator_updated():
        scheduler.coordinator_updated()

    entry.async_on_unload(controller.coordinator.async_add_listener(coordinator_updated))

    @callback
    def core_configuration_updated(event):
        # Units and home location can change without an equipment state event.
        scheduler.request("configuration_update")

    entry.async_on_unload(hass.bus.async_listen(EVENT_CORE_CONFIG_UPDATE, core_configuration_updated))
    return scheduler
