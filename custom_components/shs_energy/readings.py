"""Daily per-category readings, assembled from recorder changes.

The recorder answers with one change per meter per period. Turning those into
the category totals the portal stores involves two judgements — which changes
are usable energy, and when a category has enough of its meters to mean
anything — so they live here rather than in the coordinator.
"""

from __future__ import annotations

from typing import Any

try:  # pragma: no cover - exercised by both import paths
    from .const import MAX_KWH_PER_READING, MAX_NEGATIVE_CHANGE_KWH
except ImportError:  # The test suite imports these helpers as flat modules,
    # without Home Assistant installed, so the package parent does not exist.
    from const import (  # type: ignore[no-redef]
        MAX_KWH_PER_READING,
        MAX_NEGATIVE_CHANGE_KWH,
    )


def usable_change(change: float) -> float | None:
    """Return the energy a recorder change represents, or None when unknown.

    A negative change is never consumption. A tiny one is the meter's own
    rounding as it came back from a power cut — the counter resumes a hair
    below where the recorder last saw it — and is worth zero. A larger drop is
    a counter reset, and the energy behind it was genuinely never measured.
    """
    value = float(change)
    if value >= 0:
        return value
    return 0.0 if value >= -MAX_NEGATIVE_CHANGE_KWH else None


def _incomplete_summary(
    days_by_gap: dict[tuple[str, tuple[str, ...]], list[str]],
) -> list[str]:
    """One line per category and set of silent meters, worst-lasting first.

    Deliberately not one line per day: a meter that stays unavailable costs
    every day of the backfill window, and thirty near-identical lines bury the
    one fact worth acting on — which sensor stopped reporting.
    """
    summaries = []
    for (category, missing), days in days_by_gap.items():
        count = len(days)
        summaries.append((
            count,
            f"{category}: {count} day{'' if count == 1 else 's'} without "
            f"{', '.join(missing)} (latest {max(days)})",
        ))
    return [summary for _, summary in sorted(summaries, reverse=True)]


def daily_category_readings(
    changes_by_day: dict[str, dict[str, float]],
    entities_by_category: dict[str, list[str]],
) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    """Sum each category's meters into one reading per day.

    Returns the readings alongside the two ways a day can fail to produce one:
    ``skipped`` for an implausible total, ``incomplete`` for a category whose
    meters did not all report. A category made from several meters is only
    meaningful when every mapped meter covers the day — missing is unknown,
    never a smaller-looking partial total — but that has to be said out loud,
    or one dead sensor erases a whole category and nothing anywhere shows it.
    """
    readings: list[dict[str, Any]] = []
    skipped: list[str] = []
    days_by_gap: dict[tuple[str, tuple[str, ...]], list[str]] = {}
    for day, entity_changes in sorted(changes_by_day.items()):
        for category, entity_ids in entities_by_category.items():
            if not entity_ids:
                continue
            missing = tuple(
                entity for entity in entity_ids if entity not in entity_changes
            )
            if missing:
                days_by_gap.setdefault((category, missing), []).append(day)
                continue
            kwh = round(sum(entity_changes[entity] for entity in entity_ids), 3)
            if kwh > MAX_KWH_PER_READING:
                skipped.append(f"{category} {day} ({kwh} kWh)")
                continue
            readings.append({"date": day, "category": category, "kwh": kwh})
    return readings, skipped, _incomplete_summary(days_by_gap)
