"""Explicit local observation ports for controller policy and verification."""
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Mapping, Protocol


class ObservedState(Protocol):
    state: str
    attributes: Mapping
    last_updated: datetime
    last_reported: datetime


@dataclass(frozen=True)
class ControllerInputs:
    read: Callable[[str], ObservedState | None]
    temperature_unit: Callable[[], str]
    platform: Callable[[str], str | None]
