"""Inputs and host effects required by the household planner.

Reads expose domain values, not an application framework. Hosts own task lifetime,
canonical settings and source I/O; constructing a household starts no jobs.
"""
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass
from datetime import datetime, tzinfo
from typing import Any, Protocol

from .controller_inputs import ObservedState


class HouseholdReadError(Exception):
    """A source operation failed without producing usable observations."""


class HouseholdRefreshError(Exception):
    """The first cloud status could not be loaded."""


class ConfigurationChanged(HouseholdReadError):
    """Canonical settings changed while admission was being requested."""


class RecordStore(Protocol):
    async def async_load(self) -> dict | None: ...
    async def async_save(self, value: dict) -> None: ...


@dataclass(frozen=True)
class HomeFacts:
    latitude: float
    longitude: float
    language: str
    timezone: tzinfo


class HistorySource(Protocol):
    async def statistics(self, start: datetime, end: datetime, entities: set[str],
                         period: str, units: dict | None, kinds: set[str]) -> dict: ...

    async def states(self, start: datetime, end: datetime, entities: list[str], *,
                     with_attributes: bool) -> dict[str, list[tuple[datetime, Any, dict | None]]]: ...

    async def hourly_forecast(self, entity: str) -> list[dict]: ...


@dataclass(frozen=True)
class HouseholdPorts:
    home: Callable[[], HomeFacts]
    options: Callable[[], dict]
    admit: Callable[[dict, dict], Awaitable[None]]
    read_state: Callable[[str], ObservedState | None]
    entity_ids: Callable[[], set[str]]
    entity_names: Callable[[], dict[str, str]]
    area_names: Callable[[], dict[str, str]]
    entity_areas: Callable[[], dict[str, str]]
    inventory: Callable[[], Awaitable[list[dict]]]
    battery_report: Callable[[str], dict | None]
    history: HistorySource
    utcnow: Callable[[], datetime]
    recovering: Callable[[], bool]
    repair: Callable[[str, str | None, dict[str, str]], None]
    publish: Callable[[], None]
    spawn: Callable[[Coroutine, str], Any]
