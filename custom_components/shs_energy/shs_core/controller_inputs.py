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


def configured_entity_ids(options):
    """All configured HA sources, including quantity references and mapping keys.

    Configuration includes top-level controls, lists of cumulative meters,
    forecasts and nested device mappings. Registry-area migration only considers
    a subset of these and must not define the runtime observation boundary.
    """
    import re
    result = set()
    def visit(value):
        if isinstance(value, str):
            if re.fullmatch(r'[a-z_][a-z0-9_]*\.[a-z0-9_]+', value):
                result.add(value)
        elif isinstance(value, dict):
            for key, item in value.items():
                visit(key)
                visit(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                visit(item)
    visit(options)
    return result
