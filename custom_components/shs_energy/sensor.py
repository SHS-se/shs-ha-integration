"""Subscription, tariff, push, and calculated grid-cost sensors."""

from __future__ import annotations

from datetime import datetime, timezone
from math import isfinite
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    CONF_CUSTOMER_NAME,
    CONF_DEVICE_TOKEN_ID,
    CONF_HOME_ID,
    DOMAIN,
    OPT_CONFIGURATION_REVIEWED_AT,
    OPT_EV_CHARGE_CURRENT_ENTITY,
    OPT_EV_CURRENT_STEP_A,
    OPT_EV_MAX_CURRENT_A,
    OPT_EV_MIN_CURRENT_A,
    OPT_GRID_EXPORT_POWER_ENTITY,
    OPT_SUPPLIER_EXPORT_PRICE,
    OPT_SUPPLIER_IMPORT_PRICE,
    OPT_PLANNING_MODE,
    PLANNING_MODE_DISABLED,
)
from .configuration import resolved_options
from .optimisation import OptimisationInputError, validate_plan_contract
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
            ShsGridOperatorSensor(coordinator),
            ShsCurrentGridCostSensor(coordinator),
            ShsGridPriceSensor(coordinator, "import"),
            ShsGridPriceSensor(coordinator, "export"),
            ShsTotalPriceSensor(coordinator, "import"),
            ShsTotalPriceSensor(coordinator, "export"),
            ShsLastPushSensor(coordinator),
            ShsOptimisationStatusSensor(coordinator),
            ShsReactiveSurplusSensor(coordinator),
            ShsPlanRequestSensor(coordinator, "boiler"),
            ShsPlanRequestSensor(coordinator, "pool"),
            ShsPlanRequestSensor(coordinator, "ev"),
            ShsEvPlanCurrentSensor(coordinator),
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
            "home_id": data.get("home_id") or self.coordinator.entry.data.get(
                CONF_HOME_ID
            ),
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
            "skipped_readings": self.coordinator.skipped_readings,
            "supplier_cost_days": self.coordinator.supplier_cost_days,
        }


class ShsOptimisationStatusSensor(ShsBaseSensor):
    """Whether a verified, unexpired priority plan may be consumed locally."""

    _attr_translation_key = "optimisation_status"

    def __init__(self, coordinator: ShsStatusCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.entry.entry_id}_optimisation_status"

    @property
    def native_value(self) -> str:
        mode = resolved_options(
            self.hass, dict(self.coordinator.entry.options)
        )[OPT_PLANNING_MODE]
        if mode == PLANNING_MODE_DISABLED:
            return "disabled"
        plan = self.coordinator.optimisation_plan
        if self.coordinator.optimisation_missing_inputs:
            return "not_configured"
        if not plan:
            return "unavailable"
        try:
            validate_plan_contract(
                plan, datetime.now(timezone.utc), require_recent_issue=False
            )
        except OptimisationInputError:
            return "invalid"
        if self.coordinator.current_plan_slot is None:
            if plan.get("status") != "ready":
                return str(plan.get("status", "invalid"))
            try:
                now = datetime.now(timezone.utc)
                if now >= datetime.fromisoformat(plan["valid_until"]):
                    return "expired"
                if now >= datetime.fromisoformat(plan["binding_until"]):
                    return "advisory_only"
                return "waiting"
            except (KeyError, TypeError, ValueError):
                return "invalid"
        return "ready"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        plan = self.coordinator.optimisation_plan or {}
        return {
            "mode": resolved_options(
                self.hass, dict(self.coordinator.entry.options)
            )[OPT_PLANNING_MODE],
            "plan_id": plan.get("plan_id"),
            "model_version": plan.get("model_version"),
            "issued_at": plan.get("issued_at"),
            "valid_until": plan.get("valid_until"),
            "binding_until": plan.get("binding_until"),
            "last_push": self.coordinator.last_optimisation_push,
            "last_error": self.coordinator.last_optimisation_error,
            "home_id": self.coordinator.entry.data.get(CONF_HOME_ID),
            "actual_slots_accepted": self.coordinator.last_actual_slots_accepted,
            "actuals_accepted_until": self.coordinator.actuals_accepted_until,
            "configuration_reviewed_at": self.coordinator.entry.options.get(
                OPT_CONFIGURATION_REVIEWED_AT
            ),
            "capabilities": plan.get("capabilities", {}),
            "missing_inputs": self.coordinator.optimisation_missing_inputs,
            "validation_errors": plan.get("validation_errors", []),
        }


class ShsReactiveSurplusSensor(ShsBaseSensor):
    """Measured export available to the one local reactive allocator."""

    _attr_translation_key = "reactive_surplus"
    _attr_device_class = SensorDeviceClass.POWER
    _attr_native_unit_of_measurement = "W"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = None

    def __init__(self, coordinator: ShsStatusCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.entry.entry_id}_reactive_surplus"

    @property
    def native_value(self) -> float | None:
        return self.coordinator.reactive_surplus_w

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        entity_id = self.coordinator.entry.options.get(
            OPT_GRID_EXPORT_POWER_ENTITY
        )
        if entity_id:
            self.async_on_remove(
                async_track_state_change_event(
                    self.hass, [entity_id], self._source_changed
                )
            )

    @callback
    def _source_changed(self, _event: Any) -> None:
        self.async_write_ha_state()


class ShsPlanRequestSensor(ShsBaseSensor):
    """Current bounded request; an executor still owns the physical device."""

    _attr_device_class = SensorDeviceClass.POWER
    _attr_native_unit_of_measurement = "W"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = None

    def __init__(self, coordinator: ShsStatusCoordinator, device: str) -> None:
        super().__init__(coordinator)
        self.device = device
        self._attr_name = f"{device.title()} planned request"
        self._attr_unique_id = (
            f"{coordinator.entry.entry_id}_{device}_planned_request"
        )

    @property
    def native_value(self) -> float | None:
        slot = self.coordinator.current_plan_slot
        # Unavailable means "the planner has no authority"; zero is reserved
        # for a valid binding slot that explicitly requests the device off.
        return None if slot is None else float(slot.get(f"{self.device}_w", 0))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        slot = self.coordinator.current_plan_slot or {}
        attributes = {
            "slot_start": slot.get("start"),
            "binding": slot.get("binding"),
            "plan_status": (
                self.coordinator.optimisation_plan or {}
            ).get("status"),
            "advisory_only": True,
        }
        if self.device == "ev":
            attributes.update({
                "target_current_a": slot.get("ev_target_current_a"),
                "minimum_current_a": slot.get("ev_min_current_a"),
                "maximum_current_a": slot.get("ev_max_current_a"),
            })
        return attributes


class ShsEvPlanCurrentSensor(ShsBaseSensor):
    """Quarter-hour current target and reactive envelope for a capable EV."""

    _attr_device_class = SensorDeviceClass.CURRENT
    _attr_native_unit_of_measurement = "A"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = None

    def __init__(self, coordinator: ShsStatusCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_name = "EV planned current"
        self._attr_unique_id = f"{coordinator.entry.entry_id}_ev_planned_current"

    def _control(self) -> dict[str, Any] | None:
        plan = self.coordinator.optimisation_plan or {}
        for service in plan.get("services", []):
            control = service.get("control", {})
            if (
                service.get("device") == "ev"
                and control.get("type") == "discrete_current"
            ):
                return control
        return None

    @property
    def native_value(self) -> float | None:
        slot = self.coordinator.current_plan_slot
        plan = self.coordinator.optimisation_plan or {}
        if slot is None or not plan.get("capabilities", {}).get("ev"):
            return None
        return float(slot["ev_target_current_a"])

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        slot = self.coordinator.current_plan_slot or {}
        control = self._control() or {}
        current_entity = self.coordinator.entry.options.get(
            OPT_EV_CHARGE_CURRENT_ENTITY
        )
        current_state = (
            self.hass.states.get(current_entity) if current_entity else None
        )
        current_attributes = current_state.attributes if current_state else {}
        return {
            "slot_start": slot.get("start"),
            "binding": slot.get("binding"),
            "minimum_current_a": slot.get("ev_min_current_a"),
            "maximum_current_a": slot.get("ev_max_current_a"),
            "charger_minimum_current_a": control.get(
                "min_current_a",
                self.coordinator.entry.options.get(
                    OPT_EV_MIN_CURRENT_A, current_attributes.get("min")
                ),
            ),
            "charger_maximum_current_a": control.get(
                "max_current_a",
                self.coordinator.entry.options.get(
                    OPT_EV_MAX_CURRENT_A, current_attributes.get("max")
                ),
            ),
            "current_step_a": control.get(
                "current_step_a",
                self.coordinator.entry.options.get(
                    OPT_EV_CURRENT_STEP_A, current_attributes.get("step")
                ),
            ),
            "current_entity": current_entity,
            "advisory_only": True,
        }


class ShsGridOperatorSensor(ShsBaseSensor):
    """Which network operator's tariff this home is billed on.

    Read-only on purpose: the website owns who the customer's operator is,
    because that is what the invoices and the tariff timeline are keyed to.
    """

    _attr_translation_key = "grid_operator"

    def __init__(self, coordinator: ShsStatusCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.entry.entry_id}_grid_operator"

    @property
    def native_value(self) -> str | None:
        operator = self.coordinator.grid_operator
        return None if operator is None else operator["name"]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        operator = self.coordinator.grid_operator or {}
        return {
            "provider_key": operator.get("provider_key"),
            "tariff_key": operator.get("tariff_key"),
            "currency": operator.get("currency"),
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


def price_from_state(raw: Any) -> float | None:
    """Read a per-kWh price, or None when the source has nothing usable."""
    if raw is None or raw in ("unknown", "unavailable", ""):
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if isfinite(value) else None


class ShsTotalPriceSensor(ShsBaseSensor):
    """Grid share plus what the electricity supplier charges or pays."""

    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "SEK/kWh"
    _attr_entity_category = None
    _attr_suggested_display_precision = 3

    def __init__(self, coordinator: ShsStatusCoordinator, direction: str) -> None:
        super().__init__(coordinator)
        self.direction = direction
        self.option_key = (
            OPT_SUPPLIER_IMPORT_PRICE
            if direction == "import"
            else OPT_SUPPLIER_EXPORT_PRICE
        )
        self._attr_translation_key = f"total_{direction}_price"
        self._attr_unique_id = f"{coordinator.entry.entry_id}_total_{direction}_price"

    @property
    def _supplier_entity_id(self) -> str | None:
        return self.coordinator.entry.options.get(self.option_key) or None

    def _supplier_price(self) -> float | None:
        entity_id = self._supplier_entity_id
        if not entity_id:
            return None
        state = self.hass.states.get(entity_id)
        return price_from_state(None if state is None else state.state)

    @property
    def native_value(self) -> float | None:
        prices = self.coordinator.grid_prices
        supplier = self._supplier_price()
        if prices is None or supplier is None:
            # Reporting the grid share alone would read as an all-in price.
            return None
        grid = prices.get(f"{self.direction}_price_sek_per_kwh")
        if grid is None:
            return None
        return round(grid + supplier, 5)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        prices = self.coordinator.grid_prices or {}
        return {
            "grid_price_sek_per_kwh": prices.get(
                f"{self.direction}_price_sek_per_kwh"
            ),
            "supplier_price_sek_per_kwh": self._supplier_price(),
            "supplier_entity_id": self._supplier_entity_id,
            "load_period": prices.get("load_period"),
            "tariff_revision": prices.get("tariff_revision"),
        }

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        entity_id = self._supplier_entity_id
        if entity_id:
            # The supplier price moves on its own schedule, so follow it
            # instead of waiting for the next coordinator poll.
            self.async_on_remove(
                async_track_state_change_event(
                    self.hass, [entity_id], self._supplier_changed
                )
            )

    @callback
    def _supplier_changed(self, _event: Any) -> None:
        self.async_write_ha_state()


class ShsGridPriceSensor(ShsBaseSensor):
    """What one more kWh through the meter costs, or earns, right now."""

    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "SEK/kWh"
    _attr_entity_category = None
    _attr_suggested_display_precision = 3

    def __init__(self, coordinator: ShsStatusCoordinator, direction: str) -> None:
        super().__init__(coordinator)
        self.direction = direction
        self._attr_translation_key = f"grid_{direction}_price"
        self._attr_unique_id = f"{coordinator.entry.entry_id}_grid_{direction}_price"

    @property
    def native_value(self) -> float | None:
        prices = self.coordinator.grid_prices
        if prices is None:
            return None
        return prices.get(f"{self.direction}_price_sek_per_kwh")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        prices = self.coordinator.grid_prices or {}
        key = f"{self.direction}_price_sek_per_kwh"
        shared = {
            "load_period": prices.get("load_period"),
            "vat_rate": prices.get("vat_rate"),
            "tariff_revision": prices.get("tariff_revision"),
            "excludes": "electricity supplier energy price",
            # Published ahead of time, so this is exact rather than predicted.
            # Shaped for an optimiser: one entry per slot, UTC start, price.
            "forecast": [
                {"start": slot["start"], "price_sek_per_kwh": slot[key]}
                for slot in self.coordinator.grid_price_forecast
            ],
        }
        if self.direction == "import":
            demand = self.coordinator.demand_charge
            calculation = self.coordinator.latest_calculation or {}
            return {
                **shared,
                "transfer_sek_per_kwh": prices.get("import_transfer_sek_per_kwh"),
                "energy_tax_sek_per_kwh": prices.get("import_energy_tax_sek_per_kwh"),
                "price_sek_per_kwh_ex_vat": prices.get(
                    "import_price_sek_per_kwh_ex_vat"
                ),
                # An optimiser charging a peak needs the rate and the peak the
                # month has already incurred, or it will re-buy a peak it is
                # being billed for anyway.
                "capacity_cost_per_kw": None if demand is None else demand[
                    "rate_sek_per_kw"
                ],
                "demand_charge": demand,
                "billing_period_peak_kw": calculation.get("peak_demand_kw"),
            }
        return {
            **shared,
            "price_sek_per_kwh_ex_vat": prices.get("export_price_sek_per_kwh_ex_vat"),
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
            "missing_days": calculation.get("missing_days"),
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
            "missing_days": calculation.get("missing_days"),
            "grid_import_kwh": calculation.get("grid_import_kwh"),
            "grid_export_kwh": calculation.get("grid_export_kwh"),
            "peak_demand_kw": calculation.get("peak_demand_kw"),
            "tariff_revisions": calculation.get("tariff_revisions"),
            "amount_sek_ex_vat": _sum_field(components, "amount_sek_ex_vat"),
            "vat_amount_sek": _sum_field(components, "vat_amount_sek"),
            "how_this_is_calculated": [_explain(value) for value in components],
            "components": components,
        }
