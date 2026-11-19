"""One counter settlement path for current capture and dated history replay."""
from datetime import datetime, timedelta, timezone
import logging
from math import isfinite
from .counter_changes import CounterIntervals, CounterRow, judge_counter_rows, retract_catch_ups, settled_energy
from .readings import usable_change
from .household_ports import HouseholdReadError

_LOGGER = logging.getLogger(__name__)


class CounterReader:
    def __init__(self, history, bounds_w):
        self.history = history
        self.bounds_w = dict(bounds_w)
        self.open_catch_up = None

    async def changes(
        self,
        entity_ids: list[str],
        start: datetime,
        end: datetime,
        period: str,
    ) -> CounterIntervals:
        """Read the energy each counter really recorded, with UTC-aware timestamps.

        A change Recorder synthesised from a counter that went backwards, or
        one that spans a period it skipped, is not that period's energy. Those
        samples are settled from the counter's own readings or left out; a
        sample left out is unknown, never zero. See ``counter_changes``.
        """
        if not entity_ids:
            return CounterIntervals()
        start_utc, end_utc = start.astimezone(timezone.utc), end.astimezone(timezone.utc)
        # The period before the window says whether the window's first change
        # is its own. Calendar periods have no fixed length to step back by.
        lookback = {"5minute": timedelta(minutes=5), "hour": timedelta(hours=1)}.get(period)
        stats = await self.history.statistics(
            start_utc - lookback if lookback else start_utc,
            end_utc,
            set(entity_ids),
            period,
            {"energy": "kWh"},
            {"change", "state"},
        )
        judged = {entity_id: judge_counter_rows(rows, start_utc if lookback else None)
                  for entity_id, rows in stats.items()}
        settled = await self._settled_counter_energy(judged, start_utc, end_utc)

        result = CounterIntervals()
        for entity_id, rows in judged.items():
            values: list[tuple[datetime, float]] = []
            invalidated: set[datetime] = set()
            for row in rows:
                # The recorder returns the bucket that starts exactly on end,
                # so the still-running current day/hour would otherwise be
                # pushed as if it were complete. Keep the window half-open.
                if row.start >= end_utc or (lookback and row.start < start_utc):
                    continue
                if row.verdict == "unknown":
                    if row.invalidated:
                        invalidated.add(row.start)
                    continue
                energy = (settled.get((entity_id, row.start)) if row.verdict == "suspect"
                          else usable_change(row.change))
                if energy is None:
                    if row.invalidated or row.verdict == "trusted":
                        invalidated.add(row.start)
                    continue
                values.append((row.start, energy))
            bound_w = self.bounds_w.get(entity_id)
            if bound_w and lookback:
                # A counter that stalled and caught up: more in one period than
                # the equipment can draw. The total is real, its timing is not.
                seconds = lookback.total_seconds()
                before = {at for at, _ in values}
                values, stalls, open_ended = retract_catch_ups(values, bound_w / 1000 * seconds / 3600, seconds)
                invalidated.update(before - {at for at, _ in values})
                for first, caught_up, energy in stalls:
                    _LOGGER.warning(
                        "Counter %s stalled from %s and caught up %.3f kWh at %s (most it can use in "
                        "one period: %.3f kWh); those periods are left unmeasured", entity_id,
                        first.isoformat(), energy, caught_up.isoformat(), bound_w / 1000 * seconds / 3600)
                if open_ended and (self.open_catch_up is None or open_ended > self.open_catch_up):
                    self.open_catch_up = open_ended
            result[entity_id] = values
            if invalidated:
                result.invalidated[entity_id] = invalidated
        return result

    async def _settled_counter_energy(
        self, judged: dict[str, list[CounterRow]], start: datetime, end: datetime,
    ) -> dict[tuple[str, datetime], float]:
        """Settle the rows Recorder synthesised from the counters' own readings.

        Rare, so the readings are fetched only for the counters and the span
        that need them. Readings that cannot be read leave the rows unknown.
        """
        suspects = {entity_id: [row for row in rows if row.verdict == "suspect"
                                and start <= row.start < end and row.end is not None]
                    for entity_id, rows in judged.items()}
        suspects = {entity_id: rows for entity_id, rows in suspects.items() if rows}
        if not suspects:
            return {}
        first = min(row.start for rows in suspects.values() for row in rows)
        last = max(row.end for rows in suspects.values() for row in rows)
        try:
            states = await self.history.states(first, last, sorted(suspects), with_attributes=False)
        except HouseholdReadError as err:
            _LOGGER.warning("Counter readings could not be read; affected periods stay unknown: %s", err)
            return {}
        settled: dict[tuple[str, datetime], float] = {}
        for entity_id, rows in suspects.items():
            readings: list[tuple[datetime, float]] = []
            for at, state, _attributes in states.get(entity_id, ()):
                try:
                    value = float(state)
                except (TypeError, ValueError):
                    continue
                if isfinite(value) and isinstance(at, datetime):
                    readings.append((at.astimezone(timezone.utc), value))
            readings.sort()
            for row in rows:
                energy = settled_energy(row, readings)
                _LOGGER.info("Counter %s went backwards in the period from %s: %s", entity_id,
                             row.start.isoformat(),
                             "energy unknown" if energy is None else f"{energy:.3f} kWh from its readings")
                if energy is not None:
                    settled[(entity_id, row.start)] = energy
        return settled
