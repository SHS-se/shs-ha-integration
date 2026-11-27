"""Which Recorder changes of a cumulative counter are energy that was used.

Home Assistant's Recorder turns a counter into a running ``sum``. Its handling
of a counter that goes backwards (``sensor/recorder.py``) is right for exactly
one case: a counter that restarts from zero. Every other backwards step is
booked as consumption, because a fall of more than a tenth starts "a new cycle
at 0" and the next reading counts in full:

- a reading of zero between two good readings books the whole counter;
- a replaced meter books the new meter's starting value;
- one high reading books the jump up, then the whole counter on the way back.

A period that Recorder skipped is also not the period's own energy: the next
row's change is the difference of two sums and covers the skipped time too.

Recorder stores each period's last reading (``state``) beside the sum. A change
that is not the difference of two consecutive readings is one Recorder
synthesised, so those rows are found without any assumption about how much a
device can draw. Each is then settled from the readings themselves, where they
can be read: a restart from zero keeps the energy on both sides of it, a bad
reading between two good ones keeps their difference, and anything else is
unknown. Unknown is never zero: the sample is left out, so the quarter that
needs it stays incomplete.

A counter can also stall: the source keeps its last reading while the link to
the equipment is down, then catches up in one step. Nothing in the counter
contradicts itself, so the stored readings cannot show it. What can is the
equipment: a flat counter followed by energy above its greatest recorded
power suggests a catch-up. ``retract_catch_ups`` leaves that sample out,
together with the run of zero samples before it. A continuously advancing
counter can cross the power envelope because its reporting buckets are uneven;
a power peak alone does not disprove its energy. The total across the
stall was really used; when, nobody measured, and a zero from a counter that
did not move is not a measurement of zero.

Not found here, because nothing contradicts it: a stall whose catch-up stays
under that power, a counter with no power reading to bound it, and a replaced
meter that starts higher than the old one.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from math import isfinite
from typing import Any, Iterable

from .const import MAX_NEGATIVE_CHANGE_KWH


def _moment(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc)
    if isinstance(value, (int, float)) and not isinstance(value, bool) and isfinite(value):
        return datetime.fromtimestamp(float(value), timezone.utc)
    return None


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if isfinite(value) else None


class CounterIntervals(dict[str, list[tuple[datetime, float]]]):
    """Measured intervals plus explicit cleaner withdrawals, never read failures.

    Existing readers consume the mapping unchanged. Learning capture can also
    retract an earlier observation when later counter evidence disproves it.
    """
    def __init__(self):
        super().__init__()
        self.invalidated: dict[str, set[datetime]] = {}


@dataclass
class CounterRow:
    """One Recorder statistics row of a counter, judged against its neighbour."""
    start: datetime
    end: datetime | None
    change: float
    state: float | None
    previous_state: float | None = None
    # "trusted": the change is the difference of two consecutive readings.
    # "suspect": Recorder synthesised it; the readings decide.
    # "unknown": the change is not this period's energy.
    verdict: str = "trusted"
    invalidated: bool = False


def _consistent(change: float, before: float, after: float) -> bool:
    return abs(change - (after - before)) <= 1e-6 + 1e-9 * max(abs(before), abs(after))


def judge_counter_rows(
    rows: Iterable[dict[str, Any]], window_start: datetime | None = None,
) -> list[CounterRow]:
    """Judge each row's change against the readings Recorder stored with it.

    Rows without a reading, or without an ``end`` to prove two rows adjoin,
    cannot be judged and are trusted as they always were. ``window_start`` is
    where the caller's own window begins when it also asked for the period
    before it: a first row at or after it has no neighbour to be judged
    against, so its change may cover time Recorder skipped.
    """
    judged: list[CounterRow] = []
    for row in rows:
        start, change = _moment(row.get("start")), _number(row.get("change"))
        if start is None or change is None:
            continue
        judged.append(CounterRow(start, _moment(row.get("end")), change, _number(row.get("state"))))
    judged.sort(key=lambda row: row.start)
    if judged and window_start is not None and judged[0].end is not None \
            and judged[0].start >= window_start.astimezone(timezone.utc):
        judged[0].verdict = "unknown"
    # The level a counter fell from. Reaching it again means the fall was a bad
    # reading, and the row that climbs back carries the whole counter.
    fell_from: float | None = None
    for index, row in enumerate(judged[1:], start=1):
        previous = judged[index - 1]
        if previous.end is None:
            continue
        if previous.end != row.start:
            # Recorder skipped a period, so this change covers that time too.
            row.verdict = "unknown"
            row.invalidated = True
            continue
        if row.state is None or previous.state is None:
            continue
        row.previous_state = previous.state
        if not _consistent(row.change, previous.state, row.state):
            row.verdict = "suspect"
            row.invalidated = True
            if row.state < previous.state - MAX_NEGATIVE_CHANGE_KWH:
                fell_from = max(previous.state, fell_from if fell_from is not None else previous.state)
                _retract_excursion(judged, index, row.state)
            continue
        if fell_from is not None and previous.state < fell_from <= row.state:
            row.verdict = "unknown"
            row.invalidated = True
            fell_from = None
    return judged


def _retract_excursion(judged: list[CounterRow], fall: int, level: float) -> None:
    """A counter that falls back to a level it had already passed never used
    the energy in between: retract the rows from where it climbed above that
    level. A counter that restarted (no such climb in these rows) keeps them."""
    for index in range(fall - 1, 0, -1):
        row = judged[index]
        if row.state is None or row.previous_state is None or row.state <= level + MAX_NEGATIVE_CHANGE_KWH:
            return
        if row.previous_state <= level + MAX_NEGATIVE_CHANGE_KWH:
            for excursion in judged[index:fall]:
                excursion.verdict = "unknown"
                excursion.invalidated = True
            return


def settled_energy(row: CounterRow, readings: list[tuple[datetime, float]]) -> float | None:
    """Energy a suspect row's period really saw, from the counter's own readings.

    ``readings`` are the counter's numeric states in time order, in its own
    unit, with the last one at or before the period's start included. Returns
    kWh, or ``None`` when the readings cannot tell.
    """
    if row.end is None or row.state is None or row.previous_state is None:
        return None
    baseline = next((value for at, value in reversed(readings) if at <= row.start), None)
    inside = [value for at, value in readings if row.start < at <= row.end]
    if baseline is None or not inside:
        return None
    # The readings are in the entity's unit and the statistics in kWh; the two
    # readings Recorder kept give the factor between them.
    if abs(baseline) > 1e-12 and abs(row.previous_state) > 1e-12:
        scale = row.previous_state / baseline
    elif abs(inside[-1]) > 1e-12 and abs(row.state) > 1e-12:
        scale = row.state / inside[-1]
    else:
        return None
    if not isfinite(scale) or scale <= 0:
        return None
    rounding = MAX_NEGATIVE_CHANGE_KWH / scale
    energy = 0.0
    since_fall = 0.0
    fell_from: float | None = None
    level = baseline
    for reading in inside:
        step = reading - level
        if step >= 0:
            if fell_from is None:
                energy += step
            elif reading >= fell_from:
                # Back at or above where it fell from: a bad reading, and the
                # counter's own difference across it is the energy.
                energy += reading - fell_from
                fell_from, since_fall = None, 0.0
            else:
                since_fall += step
        elif -step <= rounding:
            pass
        elif reading <= rounding:
            # A restart from zero. The reading itself is not consumption.
            fell_from = level if fell_from is None else fell_from
            since_fall = 0.0
        else:
            # Fell to a level that is neither zero nor rounding: a replaced
            # meter or a bad reading. What was used cannot be told.
            return None
        level = reading
    return (energy + since_fall) * scale


def retract_catch_ups(
    samples: list[tuple[datetime, float]], limit_kwh: float, period_seconds: float,
) -> tuple[list[tuple[datetime, float]], list[tuple[datetime, datetime, float]], datetime | None]:
    """Leave out catch-ups after a flat counter or a gap, preserving continuous reports.

    ``samples`` are one counter's trusted ``(start, kWh)`` samples in time
    order; ``limit_kwh`` is what the equipment can use in one period at its
    greatest recorded power. Returns the samples kept, each stall as
    ``(first start, catch-up start, kWh)`` and, when a stall reaches the first
    sample given, that catch-up's start: the stall may have begun before the
    samples do.
    """
    if not (isfinite(limit_kwh) and limit_kwh > 0):
        return samples, [], None
    dropped: set[int] = set()
    stalls: list[tuple[datetime, datetime, float]] = []
    open_ended: datetime | None = None
    for index, (start, energy) in enumerate(samples):
        if not energy > limit_kwh:
            continue
        first = index
        while first > 0 and samples[first - 1][1] == 0 and first - 1 not in dropped \
                and (samples[first][0] - samples[first - 1][0]).total_seconds() == period_seconds:
            first -= 1
        # A sampled power maximum is not an energy validity limit. Require
        # a flat counter or a missing interval before inferring a catch-up.
        gap_before = index > 0 and (start - samples[index - 1][0]).total_seconds() > period_seconds
        if first == index and not gap_before:
            continue
        dropped.update(range(first, index + 1))
        stalls.append((samples[first][0], start, energy))
        if first == 0:
            open_ended = start
    return [sample for index, sample in enumerate(samples) if index not in dropped], stalls, open_ended
