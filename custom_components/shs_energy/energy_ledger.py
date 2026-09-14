"""Measured gross electrical actuals, independent of requests and forecasts.

Energy uses integer milli-watt-hours (mWh); time uses absolute milliseconds.
Each stream is one physical cumulative register and one direction at one boundary.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Literal, Optional


def _integer(value, minimum=0):
    if type(value) is not int or not minimum <= value <= 2 ** 53:
        raise ValueError("expected bounded nonnegative integer")


def _identity(value):
    if type(value) is not str or not 0 < len(value) <= 128:
        raise ValueError("expected identity of 1..128 characters")


@dataclass(frozen=True)
class EnergyBounds:
    lower_mwh: int
    upper_mwh: Optional[int]

    def __post_init__(self):
        _integer(self.lower_mwh)
        if self.upper_mwh is not None:
            _integer(self.upper_mwh, self.lower_mwh)

    def plus(self, other: EnergyBounds) -> EnergyBounds:
        upper = None if self.upper_mwh is None or other.upper_mwh is None else self.upper_mwh + other.upper_mwh
        return EnergyBounds(self.lower_mwh + other.lower_mwh, upper)


ZERO = EnergyBounds(0, 0)


@dataclass(frozen=True)
class MeterSpec:
    stream_id: str
    device_id: str
    boundary_id: str
    direction: Literal["import", "export", "charge", "discharge", "consume", "produce"]
    maximum_power_w: Optional[int]
    power_bound_evidence: Optional[str] = None

    def __post_init__(self):
        for value in (self.stream_id, self.device_id, self.boundary_id):
            _identity(value)
        if self.direction not in ("import", "export", "charge", "discharge", "consume", "produce"):
            raise ValueError("unsupported measured direction")
        if self.maximum_power_w is not None:
            _integer(self.maximum_power_w, 1)
            _identity(self.power_bound_evidence)
        elif self.power_bound_evidence is not None:
            raise ValueError("power evidence requires a declared maximum")


@dataclass(frozen=True)
class CounterSample:
    stream_id: str
    source_id: str
    epoch: int
    revision: int
    at_ms: int
    total_mwh: int
    epoch_reason: Optional[str] = None

    def __post_init__(self):
        for value in (self.stream_id, self.source_id):
            _identity(value)
        for value in (self.epoch, self.revision, self.at_ms, self.total_mwh):
            _integer(value)
        if self.epoch_reason is not None:
            _identity(self.epoch_reason)


def _maximum(spec, duration_ms):
    # Outward rounding preserves conservative bounds at fractional mWh cuts.
    return None if spec.maximum_power_w is None else (spec.maximum_power_w * duration_ms + 3599) // 3600


def _segment(spec, left, right, start_ms, end_ms):
    duration = end_ms - start_ms
    if left.epoch != right.epoch:
        return EnergyBounds(0, _maximum(spec, duration))
    delta = right.total_mwh - left.total_mwh
    if start_ms == left.at_ms and end_ms == right.at_ms:
        return EnergyBounds(delta, delta)
    outside = _maximum(spec, right.at_ms - left.at_ms - duration)
    inside = _maximum(spec, duration)
    return EnergyBounds(0 if outside is None else max(0, delta - outside),
                        delta if inside is None else min(delta, inside))


def _validate_pair(spec, left, right):
    if right.revision <= left.revision or right.at_ms <= left.at_ms or right.epoch < left.epoch:
        raise ValueError("sample revision, time or epoch regressed")
    if right.epoch == left.epoch:
        if right.source_id != left.source_id or right.epoch_reason is not None:
            raise ValueError("source changes require an explicit new epoch")
        delta = right.total_mwh - left.total_mwh
        maximum = _maximum(spec, right.at_ms - left.at_ms)
        if delta < 0 or (maximum is not None and delta > maximum):
            raise ValueError("counter decrease or impossible measured rate")
    elif right.epoch_reason is None:
        raise ValueError("epoch change requires reset/rebinding evidence")


@dataclass(frozen=True)
class MeterStream:
    spec: MeterSpec
    samples: tuple[CounterSample, ...] = ()
    started_at_ms: Optional[int] = None
    archived: EnergyBounds = ZERO
    archived_intervals: int = 0

    def __post_init__(self):
        _integer(self.archived_intervals)
        if type(self.samples) is not tuple or len(self.samples) > 257:
            raise ValueError("invalid sample chain size")
        if not self.samples:
            if self.started_at_ms is not None or self.archived != ZERO or self.archived_intervals:
                raise ValueError("unanchored stream cannot contain actuals")
            return
        _integer(self.started_at_ms)
        if self.started_at_ms > self.samples[0].at_ms:
            raise ValueError("invalid retention floor")
        archived_ms = self.samples[0].at_ms - self.started_at_ms
        if bool(archived_ms) != bool(self.archived_intervals) or self.archived_intervals > archived_ms:
            raise ValueError("invalid archived interval count")
        if self.started_at_ms == self.samples[0].at_ms and self.archived != ZERO:
            raise ValueError("unpruned stream cannot contain archived actuals")
        if self.started_at_ms == self.samples[0].at_ms and self.samples[0].epoch_reason is None:
            raise ValueError("first anchor requires epoch provenance")
        for sample in self.samples:
            if sample.stream_id != self.spec.stream_id:
                raise ValueError("sample belongs to another stream")
        for left, right in zip(self.samples, self.samples[1:]):
            _validate_pair(self.spec, left, right)
        maximum = _maximum(self.spec, archived_ms)
        if maximum is not None and self.archived_intervals:
            # Sum of per-segment ceilings may exceed the aggregate ceiling by
            # at most count-1 mWh. Do not reject that conservative rounding.
            maximum += self.archived_intervals - 1
            if self.archived.upper_mwh is None or self.archived.upper_mwh > maximum:
                raise ValueError("archived actuals exceed the physical bound")

    @property
    def lifetime(self) -> EnergyBounds:
        """Measured/uncertain use since the first anchor, including archived use."""
        result = self.archived
        for left, right in zip(self.samples, self.samples[1:]):
            result = result.plus(_segment(self.spec, left, right, left.at_ms, right.at_ms))
        return result


@dataclass(frozen=True)
class EnergyLedger:
    id: str
    mapping_revision: str
    revision: int
    streams: tuple[MeterStream, ...]
    max_intervals: int = 128

    def __post_init__(self):
        _identity(self.id)
        _identity(self.mapping_revision)
        _integer(self.revision)
        _integer(self.max_intervals, 1)
        if self.max_intervals > 256 or type(self.streams) is not tuple or not 0 < len(self.streams) <= 64:
            raise ValueError("ledger exceeds its declared bounds")
        if len({s.spec.stream_id for s in self.streams}) != len(self.streams):
            raise ValueError("duplicate stream identity")
        if self.revision < sum(len(s.samples) + s.archived_intervals for s in self.streams):
            raise ValueError("ledger revision cannot precede its observations")
        if len({(s.spec.device_id, s.spec.boundary_id, s.spec.direction) for s in self.streams}) != len(self.streams):
            raise ValueError("duplicate physical boundary/direction mapping")
        sources = {}
        for stream in self.streams:
            if len(stream.samples) > self.max_intervals + 1:
                raise ValueError("retention capacity reached; prune explicitly before retry")
            for sample in stream.samples:
                owner = sources.setdefault(sample.source_id, stream.spec.stream_id)
                if owner != stream.spec.stream_id:
                    raise ValueError("one physical counter cannot feed multiple streams")
            stream.lifetime  # Validate bounded totals as well as individual counters.


def create_ledger(identity: str, mapping_revision: str, specs: tuple[MeterSpec, ...], *, max_intervals=128) -> EnergyLedger:
    return EnergyLedger(identity, mapping_revision, 0, tuple(MeterStream(spec) for spec in specs), max_intervals)


def _sample_admission(ledger, sample, now_ms):
    _integer(now_ms)
    stream = next((s for s in ledger.streams if s.spec.stream_id == sample.stream_id), None)
    if stream is None:
        raise ValueError("unregistered meter stream")
    if stream.samples:
        previous = stream.samples[-1]
        if sample.revision < previous.revision:
            return stream, "stale_meter_sample"
        if sample.revision == previous.revision:
            if sample != previous:
                raise ValueError("conflicting meter sample at the same revision")
            return stream, "duplicate_meter_sample"
        _validate_pair(stream.spec, previous, sample)
    elif sample.epoch_reason is None:
        raise ValueError("first anchor requires epoch provenance")
    if sample.at_ms > now_ms:
        raise ValueError("future meter sample")
    return stream, "meter_recorded"


def record_sample(ledger: EnergyLedger, sample: CounterSample, now_ms: int) -> tuple[EnergyLedger, str]:
    """Standalone append; capacity maintenance is explicit for this low-level API."""
    stream, outcome = _sample_admission(ledger, sample, now_ms)
    if outcome != "meter_recorded":
        return ledger, outcome
    return _append_sample(ledger, stream, sample), outcome


def _append_sample(ledger, stream, sample):
    updated = replace(stream, samples=(*stream.samples, sample),
                      started_at_ms=sample.at_ms if stream.started_at_ms is None else stream.started_at_ms)
    return replace(ledger, revision=ledger.revision + 1,
                   streams=tuple(updated if s.spec.stream_id == sample.stream_id else s for s in ledger.streams))


def prune_ledger(ledger: EnergyLedger, before_ms: int, *, stream_id: Optional[str] = None) -> EnergyLedger:
    """Archive only complete segments; preserve counter anchors and lifetime bounds."""
    _integer(before_ms)
    if stream_id is not None and stream_id not in {s.spec.stream_id for s in ledger.streams}:
        raise ValueError("unknown retention stream")
    streams = []
    for stream in ledger.streams:
        if stream_id is not None and stream.spec.stream_id != stream_id:
            streams.append(stream)
            continue
        remove = 0
        archived = stream.archived
        for left, right in zip(stream.samples, stream.samples[1:]):
            if right.at_ms > before_ms:
                break
            archived = archived.plus(_segment(stream.spec, left, right, left.at_ms, right.at_ms))
            remove += 1
        streams.append(replace(stream, samples=stream.samples[remove:], archived=archived,
                               archived_intervals=stream.archived_intervals + remove))
    if tuple(streams) == ledger.streams:
        return ledger
    return replace(ledger, streams=tuple(streams), revision=ledger.revision + 1)


@dataclass(frozen=True)
class ActualsWatermark:
    ledger_id: str
    mapping_revision: str
    ledger_revision: int
    at_ms: int

    def __post_init__(self):
        _identity(self.ledger_id)
        _identity(self.mapping_revision)
        _integer(self.ledger_revision)
        _integer(self.at_ms)


def mark_actuals(ledger: EnergyLedger, at_ms: int) -> ActualsWatermark:
    _integer(at_ms)
    if any(s.samples and s.samples[-1].at_ms > at_ms for s in ledger.streams):
        raise ValueError("watermark precedes current ledger evidence")
    return ActualsWatermark(ledger.id, ledger.mapping_revision, ledger.revision, at_ms)


@dataclass(frozen=True)
class StreamActuals:
    spec: MeterSpec
    energy: EnergyBounds
    counter_measured_ms: int
    uncertain_ms: int
    reasons: tuple[str, ...]

    def __post_init__(self):
        _integer(self.counter_measured_ms)
        _integer(self.uncertain_ms)
        allowed = {"no_meter_anchor", "before_first_anchor", "epoch_gap", "partial_counter_interval", "awaiting_meter_sample"}
        if type(self.reasons) is not tuple or len(self.reasons) > 5 or not set(self.reasons) <= allowed or tuple(sorted(set(self.reasons))) != self.reasons:
            raise ValueError("invalid actuals coverage reasons")


def actuals_since(ledger: EnergyLedger, watermark: ActualsWatermark, until_ms: int) -> tuple[StreamActuals, ...]:
    """Read bounded actuals by physical stream; never allocate, net or prorate them."""
    _integer(until_ms, watermark.at_ms)
    if (watermark.ledger_id, watermark.mapping_revision) != (ledger.id, ledger.mapping_revision) or watermark.ledger_revision > ledger.revision:
        raise ValueError("foreign or future actuals watermark")
    return tuple(_stream_actuals(stream, watermark.at_ms, until_ms) for stream in ledger.streams)


def _stream_actuals(stream, start, until_ms):
    samples = stream.samples
    if samples and stream.started_at_ms < samples[0].at_ms and start < samples[0].at_ms:
        raise ValueError("actuals watermark precedes retained history")
    energy, measured, uncertain, reasons = ZERO, 0, 0, set()

    def unknown(left, right, reason):
        nonlocal energy, uncertain
        duration = max(0, min(until_ms, right) - max(start, left))
        if duration:
            energy = energy.plus(EnergyBounds(0, _maximum(stream.spec, duration)))
            uncertain += duration
            reasons.add(reason)

    if not samples:
        unknown(start, until_ms, "no_meter_anchor")
    else:
        unknown(start, samples[0].at_ms, "before_first_anchor")
        for left, right in zip(samples, samples[1:]):
            a, b = max(start, left.at_ms), min(until_ms, right.at_ms)
            if a >= b:
                continue
            energy = energy.plus(_segment(stream.spec, left, right, a, b))
            if left.epoch != right.epoch:
                uncertain += b - a
                reasons.add("epoch_gap")
            elif a != left.at_ms or b != right.at_ms:
                uncertain += b - a
                reasons.add("partial_counter_interval")
            else:
                measured += b - a
        unknown(samples[-1].at_ms, until_ms, "awaiting_meter_sample")
    return StreamActuals(stream.spec, energy, measured, uncertain, tuple(sorted(reasons)))


@dataclass(frozen=True)
class SettledActuals:
    through_ms: int
    actuals: StreamActuals

    def __post_init__(self):
        _integer(self.through_ms)


def _plus_actuals(left, right):
    if left.spec != right.spec:
        raise ValueError("cannot combine different measured streams")
    return StreamActuals(left.spec, left.energy.plus(right.energy),
                         left.counter_measured_ms + right.counter_measured_ms,
                         left.uncertain_ms + right.uncertain_ms,
                         tuple(sorted(set(left.reasons) | set(right.reasons))))


def start_settlement(ledger: EnergyLedger, origin: ActualsWatermark) -> tuple[SettledActuals, ...]:
    actuals_since(ledger, origin, origin.at_ms)
    return tuple(SettledActuals(origin.at_ms, StreamActuals(s.spec, ZERO, 0, 0, ())) for s in ledger.streams)


def validate_settlement(ledger, origin, settled):
    if ((origin.ledger_id, origin.mapping_revision) != (ledger.id, ledger.mapping_revision)
            or origin.ledger_revision > ledger.revision or len(settled) != len(ledger.streams)):
        raise ValueError("settlement ledger identity or cardinality differs")
    for stream, prefix in zip(ledger.streams, settled):
        actual = prefix.actuals
        if (actual.spec != stream.spec or prefix.through_ms < origin.at_ms
                or actual.counter_measured_ms + actual.uncertain_ms != prefix.through_ms - origin.at_ms):
            raise ValueError("settlement scope or duration differs")
        if prefix.through_ms == origin.at_ms and (actual.energy != ZERO or actual.reasons):
            raise ValueError("empty settlement must contain no actuals")
        if prefix.through_ms > origin.at_ms and (not stream.samples or prefix.through_ms != stream.samples[0].at_ms):
            raise ValueError("settlement must end at the retained physical counter anchor")
        _stream_actuals(stream, prefix.through_ms, prefix.through_ms)


def reconciled_actuals(ledger, origin, settled, until_ms) -> tuple[StreamActuals, ...]:
    validate_settlement(ledger, origin, settled)
    _integer(until_ms, origin.at_ms)
    if any(prefix.through_ms > until_ms for prefix in settled):
        raise ValueError("query precedes settled accounting")
    return tuple(_plus_actuals(prefix.actuals, _stream_actuals(stream, prefix.through_ms, until_ms))
                 for stream, prefix in zip(ledger.streams, settled))


def settle_and_prune(ledger, origin, settled, before_ms, *, stream_id=None):
    validate_settlement(ledger, origin, settled)
    pruned = prune_ledger(ledger, before_ms, stream_id=stream_id)
    prefixes = []
    for old, new, prefix in zip(ledger.streams, pruned.streams, settled):
        if len(new.samples) < len(old.samples) and new.samples[0].at_ms > prefix.through_ms:
            through = new.samples[0].at_ms
            prefix = SettledActuals(through, _plus_actuals(
                prefix.actuals, _stream_actuals(old, prefix.through_ms, through)))
        prefixes.append(prefix)
    result = tuple(prefixes)
    validate_settlement(pruned, origin, result)
    return pruned, result


def record_actuals(ledger, sample, now_ms, *, origin=None, settled=()):
    """Validate, settle capacity pressure and append as one immutable transaction."""
    stream, outcome = _sample_admission(ledger, sample, now_ms)
    if origin is not None:
        validate_settlement(ledger, origin, settled)
    elif settled:
        raise ValueError("settlement requires an origin watermark")
    if outcome != "meter_recorded":
        return ledger, settled, outcome
    if len(stream.samples) == ledger.max_intervals + 1:
        cut = stream.samples[1].at_ms
        if origin is None:
            ledger = prune_ledger(ledger, cut, stream_id=sample.stream_id)
        else:
            ledger, settled = settle_and_prune(ledger, origin, settled, cut, stream_id=sample.stream_id)
        stream = next(s for s in ledger.streams if s.spec.stream_id == sample.stream_id)
    return _append_sample(ledger, stream, sample), settled, outcome
