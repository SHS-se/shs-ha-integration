"""Subscription, tariff, push, and calculated grid-cost sensors."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_CUSTOMER_NAME, CONF_DEVICE_TOKEN_ID, DOMAIN
from .coordinator import ShsStatusCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: Any,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: ShsStatusCoordinator = entry.runtime_data
    async_add_entities(
        [
            ShsSubscriptionSensor(coordinator),
            ShsTariffStatusSensor(coordinator),
            ShsCurrentGridCostSensor(coordinator),
            ShsLastPushSensor(coordinator),
        ]
    )
    added_component_keys: set[str] = set()

    def add_component_entities() -> None:
        new_keys = sorted(set(coordinator.tariff_components) - added_component_keys)
        if not new_keys:
            return
        async_add_entities([
            ShsTariffComponentSensor(
                coordinator,
                key,
                coordinator.tariff_components[key],
            )
            for key in new_keys
        ])
        added_component_keys.update(new_keys)

    add_component_entities()
    entry.async_on_unload(coordinator.async_add_listener(add_component_entities))


class ShsBaseSensor(CoordinatorEntity[ShsStatusCoordinator], SensorEntity):
    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: ShsStatusCoordinator) -> None:
        super().__init__(coordinator)
        entry = coordinator.entry
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.data[CONF_DEVICE_TOKEN_ID])},
            name=entry.data.get(CONF_CUSTOMER_NAME) or "Smart Home Solutions",
            manufacturer="Smart Home Solutions",
            entry_type=DeviceEntryType.SERVICE,
        )


class ShsSubscriptionSensor(ShsBaseSensor):
    _attr_translation_key = "subscription"

    def __init__(self, coordinator: ShsStatusCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.entry.entry_id}_subscription"

    @property
    def native_value(self) -> str:
        data = self.coordinator.data or {}
        return "active" if data.get("subscription_active") else "inactive"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data = self.coordinator.data or {}
        return {
            "expires_at": data.get("subscription_expires_at"),
            "customer": data.get("customer_name"),
        }


class ShsLastPushSensor(ShsBaseSensor):
    _attr_translation_key = "last_push"

    def __init__(self, coordinator: ShsStatusCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.entry.entry_id}_last_push"

    @property
    def native_value(self) -> str | None:
        return self.coordinator.last_push_date

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "last_error": self.coordinator.last_push_error,
            "last_calculation_error": self.coordinator.last_calculation_error,
        }


class ShsTariffStatusSensor(ShsBaseSensor):
    _attr_translation_key = "tariff_status"

    def __init__(self, coordinator: ShsStatusCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.entry.entry_id}_tariff_status"

    @property
    def native_value(self) -> str:
        return self.coordinator.tariff_status

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        catalog = self.coordinator.tariff_catalog or {}
        revisions = [
            version.get("revision")
            for profile in catalog.get("profiles", [])
            for version in profile.get("versions", [])
            if version.get("revision")
        ]
        return {
            "configuration_ready": catalog.get("configuration") is not None,
            "missing_inputs": catalog.get("missing_inputs", []),
            "missing_questions": self.coordinator.missing_questions,
            "revisions": revisions,
            "last_error": self.coordinator.last_tariff_error,
            "last_calculation_error": self.coordinator.last_calculation_error,
        }


def _sum_field(components: list[dict[str, Any]], field: str) -> float | None:
    values = [
        value.get(field)
        for value in components
        if isinstance(value.get(field), (int, float))
    ]
    return round(sum(values), 2) if values else None


def _number(value: Any) -> str:
    """Trim trailing zeros so rates read like the invoice does."""
    text = f"{float(value):.5f}".rstrip("0").rstrip(".")
    return text or "0"


def _explain(component: dict[str, Any]) -> str:
    """One plain sentence showing how this amount was reached."""
    quantity = component.get("quantity")
    unit = component.get("unit")
    gross_rate = component.get("unit_price_sek")
    ex_vat_rate = component.get("unit_price_sek_ex_vat")
    vat_rate = component.get("vat_rate") or 0
    amount = component.get("amount_sek")

    if isinstance(quantity, (int, float)) and isinstance(gross_rate, (int, float)):
        head = f"{_number(quantity)} {unit or ''}".strip()
        head = f"{head} × {_number(gross_rate)} kr/{unit or 'unit'}"
    else:
        head = f"{_number(amount)} kr" if isinstance(amount, (int, float)) else "—"

    if vat_rate:
        basis = (
            f"{_number(ex_vat_rate)} kr ex moms + {_number(vat_rate * 100)}% moms"
            if isinstance(ex_vat_rate, (int, float))
            else f"incl. {_number(vat_rate * 100)}% moms"
        )
        head = f"{head} incl. moms ({basis})"
    else:
        head = f"{head} (no moms — micro-production is not VAT-able)"

    if isinstance(amount, (int, float)):
        head = f"{head} = {_number(round(amount, 2))} kr"
    period = component.get("period_start"), component.get("period_end")
    if all(period):
        head = f"{head}, {period[0]} → {period[1]}"
    return head


class ShsTariffComponentSensor(ShsBaseSensor):
    """Monthly amount for one stable Ellevio tariff component."""

    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_native_unit_of_measurement = "SEK"
    _attr_state_class = SensorStateClass.TOTAL
    _attr_entity_category = None
    _attr_suggested_display_precision = 2

    def __init__(
        self,
        coordinator: ShsStatusCoordinator,
        component_key: str,
        definition: dict[str, str],
    ) -> None:
        super().__init__(coordinator)
        self.component_key = component_key
        self.definition = definition
        self._attr_name = definition["label"]
        self._attr_unique_id = (
            f"{coordinator.entry.entry_id}_tariff_component_{component_key}"
        )

    def _components(self) -> list[dict[str, Any]]:
        return [
            component
            for component in self.coordinator.latest_display_components
            if component.get("component_key") == self.component_key
        ]

    @property
    def native_value(self) -> float | None:
        calculation = self.coordinator.latest_calculation
        if calculation is None:
            return None
        return round(sum(float(value["amount_sek"]) for value in self._components()), 2)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        calculation = self.coordinator.latest_calculation or {}
        components = self._components()
        total_ex_vat = _sum_field(components, "amount_sek_ex_vat")
        total_vat = _sum_field(components, "vat_amount_sek")
        return {
            "component_key": self.component_key,
            "category": self.definition["category"],
            "active": bool(components),
            "billing_month": calculation.get("billing_month"),
            "coverage_start": calculation.get("coverage_start"),
            "coverage_end": calculation.get("coverage_end"),
            "is_complete": calculation.get("is_complete"),
            "tariff_revisions": calculation.get("tariff_revisions"),
            "amount_sek_ex_vat": total_ex_vat,
            "vat_amount_sek": total_vat,
            "how_this_is_calculated": [_explain(value) for value in components],
            "details": components,
        }


class ShsCurrentGridCostSensor(ShsBaseSensor):
    _attr_translation_key = "current_grid_cost"
    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_native_unit_of_measurement = "SEK"
    _attr_state_class = SensorStateClass.TOTAL
    _attr_entity_category = None
    _attr_suggested_display_precision = 2

    def __init__(self, coordinator: ShsStatusCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.entry.entry_id}_current_grid_cost"

    @property
    def native_value(self) -> float | None:
        calculation = self.coordinator.latest_calculation
        return None if calculation is None else calculation.get("total_amount_sek")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        calculation = self.coordinator.latest_calculation or {}
        components = self.coordinator.latest_display_components
        return {
            "billing_month": calculation.get("billing_month"),
            "coverage_start": calculation.get("coverage_start"),
            "coverage_end": calculation.get("coverage_end"),
            "is_complete": calculation.get("is_complete"),
            "grid_import_kwh": calculation.get("grid_import_kwh"),
            "grid_export_kwh": calculation.get("grid_export_kwh"),
            "peak_demand_kw": calculation.get("peak_demand_kw"),
            "tariff_revisions": calculation.get("tariff_revisions"),
            "amount_sek_ex_vat": _sum_field(components, "amount_sek_ex_vat"),
            "vat_amount_sek": _sum_field(components, "vat_amount_sek"),
            "how_this_is_calculated": [_explain(value) for value in components],
            "components": components,
        }
