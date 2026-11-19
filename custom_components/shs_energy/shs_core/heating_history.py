"""Native Recorder intervals for heating learning, shared by capture and replay.

An old hourly mean is one measured hour, never four invented quarters. Counter
cleaning stays with Household._statistics_changes; this owner only joins those
cleaned intervals to the temperatures and their explicit physical sources.
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from math import isfinite
from hashlib import sha256
import json
from typing import Any, Protocol

from .thermal import configured_room_air_probes, house_temperature_quarters, quarter_means, thermal_zone_inputs, time_weighted_quarters
from .probe_evidence import native_probe_bounds, probe_interval_facts
from .household_ports import HouseholdReadError

QUARTER = timedelta(minutes=15)
HOUR = timedelta(hours=1)


class RecorderHistory(Protocol):
    async def statistics(self, start, end, entities, period, units, kinds): ...
    async def states(self, start, end, entities, *, with_attributes): ...


EnergyReader = Callable[[list[str], datetime, datetime, str],
                        Awaitable[dict[str, list[tuple[datetime, float]]]]]


@dataclass(frozen=True)
class HeatingRoomBinding:
    room_key: str
    source_key: str
    temperature_entity_id: str
    device_keys: tuple[str, ...]
    # No-cooling evidence from climate capabilities or an explicitly configured
    # heating-only relay purpose. This does not classify heater technology.
    heating_only: bool = False
    actuator_entity_ids: tuple[str, ...] = ()
    heating_only_actuator_ids: tuple[str, ...] = ()
    # Explicit attribution limit for a meter independently known to include
    # other loads. Raw recorded energy remains unchanged in the payload.
    heater_limits_w: Mapping[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class HeatingHistoryCapture:
    observations: list[dict[str, Any]]
    diagnostics: dict[str, int]
    probe_observations: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class HeatingHistorySources:
    bindings: list[HeatingRoomBinding]
    device_statistics: dict[str, str]
    room_air_probes: dict[str, str]
    outdoor_entity: str | None
    lineage_revision: str = ""
    device_power_statistics: dict[str, str] = field(default_factory=dict)
    counter_epochs: dict[str, tuple[str | None, str | None]] = field(default_factory=dict)
    counter_qualification: dict[str, object] = field(default_factory=dict)

    def for_room(self, binding: HeatingRoomBinding) -> HeatingHistorySources:
        return HeatingHistorySources([binding],
            {key: self.device_statistics[key] for key in binding.device_keys if key in self.device_statistics},
            self.room_air_probes, self.outdoor_entity, self.lineage_revision,
            {key: self.device_power_statistics[key] for key in binding.device_keys if key in self.device_power_statistics},
            {key: self.counter_epochs[key] for key in binding.device_keys if key in self.counter_epochs},
            {key: self.counter_qualification[key] for key in binding.device_keys if key in self.counter_qualification})

    @property
    def signature(self) -> str:
        identity = {"revision": 1, **asdict(self)}
        return sha256(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def heating_history_sources(devices, mappings, rooms, read_state, *, outdoor_entity=None,
                            pool_water_entity=None) -> HeatingHistorySources:
    """Resolve only configured room-air sources and verified heating controls.

    Plain switches in the user-configured heating category express a heat-only
    operating contract; they do not establish resistance-heater technology. A
    climate actuator retains its own capabilities and recorded mode, including
    a reversible climate controlled with the switch_schedule contract.
    """
    # The server admits a native room only when every mapped controllable
    # setpoint/relay member belongs to heating. Match that membership before
    # capture so one mixed-purpose room cannot stall the home's history ACK.
    mixed_rooms = {(device.get("mapping_summary") or {}).get("room_key") for device in devices
                   if device.get("category") != "heating" and device.get("planning_role") == "controllable"
                   and device.get("control_type") in ("setpoint", "switch_schedule")
                   and (mappings.get(str(device["key"])) or {}).get("control_type") == device.get("control_type")}
    heating = [device for device in devices if device.get("category") == "heating"
               and (device.get("mapping_summary") or {}).get("room_key") not in mixed_rooms]
    zones = thermal_zone_inputs(heating, mappings)
    excluded = {outdoor_entity, pool_water_entity, None}
    probes = configured_room_air_probes(rooms, excluded)
    statistics = {str(device["key"]): str(device["statistic_id"]) for device in heating
                  if isinstance(device.get("statistic_id"), str) and device["statistic_id"]}
    bindings = []
    for room, zone in sorted(zones.items()):
        temperature = zone["temperature_entity_id"]
        if temperature in excluded:
            continue
        # Room catalogue and device mapping must identify the same probe. A
        # stale device mapping must not relabel another sensor's observations.
        if probes.get(room) != temperature:
            continue
        actuators = tuple(sorted(set(zone["actuator_entity_ids"])))
        def heat_only(entity):
            if entity.startswith(("switch.", "input_boolean.")):
                # The configured purpose supplies the operating contract; do
                # not infer a physical technology from the relay or load type.
                return True
            state = read_state(entity) if entity.startswith("climate.") else None
            modes = getattr(state, "attributes", {}).get("hvac_modes") if state is not None else None
            return isinstance(modes, (list, tuple)) and "heat" in modes and set(modes) <= {"heat", "off"}
        bindings.append(HeatingRoomBinding(
            room, sha256(temperature.encode()).hexdigest(), temperature,
            tuple(sorted(set(zone["device_keys"]))), bool(actuators) and all(map(heat_only, actuators)),
            actuators, tuple(entity for entity in actuators if heat_only(entity))))
    statistics = {key: statistics[key] for binding in bindings for key in binding.device_keys if key in statistics}
    powers = {key: mappings[key]['power'] for key in statistics if isinstance(mappings.get(key), dict)
              and isinstance(mappings[key].get('power'), str) and mappings[key]['power']}
    return HeatingHistorySources(bindings, statistics, probes, outdoor_entity, device_power_statistics=powers)


def heating_recent_window(sources: HeatingHistorySources, cursor, end: datetime, *, room_count=None):
    """Repair recent late data and catch up missed uploads in bounded windows."""
    start = _hour(end) - 2 * HOUR
    if isinstance(cursor, dict) and cursor.get("signature") == sources.signature:
        try:
            accepted = datetime.fromisoformat(str(cursor.get("accepted_until"))).astimezone(timezone.utc)
        except (ValueError, TypeError):
            pass
        else:
            start = min(start, _hour(accepted) - 2 * HOUR)
    start = max(start, _hour(end) - timedelta(days=400))
    # At least four hours allows forward progress beyond the two-hour overlap.
    hours = max(4, min(24, 256 // max(1, 4 * (room_count or len(sources.bindings)))))
    return start, min(end, start + timedelta(hours=hours))


def heating_history_window(sources: HeatingHistorySources, cursor, end: datetime, *, room_count=None):
    """One bounded older window, restarted when physical source identities change."""
    from .device_observations import history_resend_window
    if not sources.bindings:
        return None
    # Whole-hour boundaries allow historical means to remain native. Recent
    # capture overlaps from this same hour through the latest complete quarter.
    recent_start = _hour(end) - 2 * HOUR
    signature = sources.signature
    if not isinstance(cursor, dict) or cursor.get("signature") != signature:
        cursor = {"signature": signature, "before": recent_start.isoformat()}
    hours = max(1, min(24, 256 // (4 * (room_count or len(sources.bindings)))))
    return history_resend_window(cursor, signature, _hour(end),
                                 chunk=timedelta(hours=hours), reach=timedelta(days=400))


def accepted_heating_cursor(result, submitted, pending, previous=None, *, probe_observations=None):
    """Advance each exact source ACK independently; a stale room cannot stall peers."""
    total = result.get("heating_observations_accepted")
    acknowledgements = result.get("heating_observations_accepted_by_source")
    if (submitted is None or not isinstance(pending, dict) or type(total) is not int or total < 0
            or not isinstance(acknowledgements, list)):
        return None
    counts = Counter((row.get("room_key"), row.get("source_key")) for row in submitted)
    accepted = {}
    duplicates = set()
    for item in acknowledgements:
        if not isinstance(item, dict):
            return None
        key = item.get("room_key"), item.get("source_key")
        count = item.get("accepted")
        if not all(isinstance(value, str) for value in key) or type(count) is not int or count < 0:
            return None
        if key in accepted:
            duplicates.add(key)
        accepted[key] = count
    if total != sum(accepted.values()) or total > len(submitted):
        return None
    old = previous.get("rooms", {}) if isinstance(previous, dict) else {}
    rooms = {room: old[room] for room in pending.get("rooms", {}) if room in old}
    advanced = False
    deferred = set(pending.get("deferred_rooms", []))
    for room, cursor in pending.get("rooms", {}).items():
        if room in deferred:
            continue
        keys = [key for key in counts if key[0] == room]
        # Empty successful reads are explicit through the overall response;
        # no source-specific row can exist when nothing was submitted for it.
        if all(key not in duplicates and accepted.get(key, 0) == counts[key] for key in keys):
            rooms[room] = cursor
            advanced = True
    evidence = pending.get("probe_evidence")
    captured = pending.get("probe_capture")
    if isinstance(evidence, dict) and isinstance(captured, dict):
        evidence = dict(evidence)
        ack = (probe_observations is not None and type(result.get("heating_probe_observations_accepted")) is int
               and result["heating_probe_observations_accepted"] == len(probe_observations))
        failures = captured.get("read_failed_windows", []) if ack else captured.get("windows", [])
        repaired = captured.get("repair_window")
        if ack and repaired and repaired not in failures:
            settle_probe_repair(evidence, *repaired)
        for start, end in failures:
            queue_probe_repair(evidence, start, end)
        # Auxiliary progress is independent of thermal source acceptance.
        return {"rooms": rooms, "probe_evidence": evidence}
    return {"rooms": rooms} if advanced else None


def queue_probe_repair(evidence, start, end):
    """Add only the failed range; never bridge previously completed gaps."""
    windows = probe_repair_windows(evidence)
    if start < end:
        evidence["repair_windows"] = [*windows, [start, end]]
        probe_repair_windows(evidence)


def probe_repair_windows(evidence):
    """Normalize the durable interval set, migrating the former span once."""
    windows = list(evidence.get("repair_windows", []))
    before = evidence.pop("repair_before", None)
    start = evidence.pop("repair_from", None)
    if start is not None and before is not None and start < before:
        windows.append([start, before])
    merged = []
    for start, end in sorted(windows):
        if start >= end:
            continue
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    evidence["repair_windows"] = merged
    return merged


def settle_probe_repair(evidence, start, end):
    """Subtract exactly the successfully captured and acknowledged interval."""
    remaining = []
    for first, last in probe_repair_windows(evidence):
        if end <= first or start >= last:
            remaining.append([first, last])
        else:
            if first < start:
                remaining.append([first, start])
            if end < last:
                remaining.append([end, last])
    evidence["repair_windows"] = remaining


def next_probe_repair(evidence, end: datetime, hours: int):
    """Oldest outstanding work first; new failures cannot reset its progress."""
    floor = (_hour(end) - timedelta(days=400)).isoformat()
    evidence["repair_windows"] = [[max(first, floor), last]
        for first, last in probe_repair_windows(evidence) if last > floor]
    for first, last in evidence["repair_windows"]:
        start = datetime.fromisoformat(first)
        finish = min(end, datetime.fromisoformat(last), start + timedelta(hours=hours))
        if start < finish:
            return start, finish
    return None


def _number(value: Any) -> float | None:
    return float(value) if type(value) in (int, float) and isfinite(value) else None


def _time(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc)
    number = _number(value)
    return datetime.fromtimestamp(number, timezone.utc) if number is not None else None


def _hour(value: datetime) -> datetime:
    return value.replace(minute=0, second=0, microsecond=0)


def _temperature_means(rows, start: datetime, end: datetime, seconds: int):
    values = []
    for row in rows:
        at, value = _time(row.get("start")), _number(row.get("mean"))
        if at is None or value is None or not -50 <= value <= 80 or at < start or at + timedelta(seconds=seconds) > end:
            continue
        if at.timestamp() % seconds:
            continue
        finish = _time(row.get("end"))
        if finish is not None and finish != at + timedelta(seconds=seconds):
            continue
        values.append((at, value))
    # This mean is paired with the full interval's measured energy. Two of
    # three samples cannot describe that same 900-second temperature window.
    return quarter_means(values, min_coverage=1.0) if seconds == 300 else dict(values)


def _energy_intervals(rows, start: datetime, end: datetime, seconds: int):
    values = {at.astimezone(timezone.utc): number for at, value in rows
              if (number := _number(value)) is not None and number >= 0
              and start <= at < end and at.timestamp() % seconds == 0
              and at + timedelta(seconds=seconds) <= end}
    if seconds == 3600:
        return values
    quarters = {at.replace(minute=at.minute // 15 * 15) for at in values}
    return {at: sum(values[at + timedelta(minutes=offset)] for offset in (0, 5, 10))
            for at in quarters if all(at + timedelta(minutes=offset) in values for offset in (0, 5, 10))}


def _cooling(state: Any, attributes: dict[str, Any] | None) -> float | None:
    """Only actual action or an unambiguous mode establishes no cooling."""
    if not isinstance(state, str) or state.lower() in ("unavailable", "unknown", "none", ""):
        return None
    action = (attributes or {}).get("hvac_action")
    if isinstance(action, str):
        if action.lower() in ("cooling", "drying", "fan"):
            return 1.0
        if action.lower() in ("heating", "idle", "off"):
            return 0.0
    if state.lower() in ("cool", "dry", "fan_only"):
        return 1.0
    if state.lower() in ("heat", "off"):
        return 0.0
    return None


def _mode(binding: HeatingRoomBinding, starts: Sequence[datetime], cooling):
    if binding.heating_only:
        return False
    if not binding.actuator_entity_ids:
        return None
    observed = [cooling.get(entity, {}).get(at)
                for entity in binding.actuator_entity_ids if entity not in binding.heating_only_actuator_ids
                for at in starts]
    if any(value is not None and value > 0 for value in observed):
        return True
    return False if all(value is not None for value in observed) else None


async def capture_heating_history(
    *, history: RecorderHistory, read_energy: EnergyReader,
    bindings: Sequence[HeatingRoomBinding], device_statistics: Mapping[str, str],
    room_air_probes: Mapping[str, str], outdoor_entity: str | None,
    start: datetime, end: datetime,
    probe_topology: Mapping[str, Any] | None = None,
) -> HeatingHistoryCapture:
    """Capture a caller-bounded window without changing planning or raw actuals.

    Bindings and room_air_probes are explicit, verified room-air sources. The
    sensor registry's generic temperature class also contains equipment chips,
    water and outdoor sensors, and is deliberately not a room-air catalogue.
    An hour uses four complete fine observations when possible, otherwise one
    complete native hour. Partial finer evidence survives when no complete hour
    is available. Every selected row states its actual interval and meter IDs.
    """
    for binding in bindings:
        if not isinstance(binding.heater_limits_w, Mapping) or any(
            key not in binding.device_keys or _number(limit) is None or limit <= 0
            for key, limit in binding.heater_limits_w.items()
        ):
            raise ValueError("Heater attribution limits must name bound devices with finite positive watts")
    start, end = start.astimezone(timezone.utc), end.astimezone(timezone.utc)
    if end <= start or not bindings:
        return HeatingHistoryCapture([], {})
    probes = {binding.temperature_entity_id for binding in bindings} | set(room_air_probes.values())
    if outdoor_entity:
        probes.add(outdoor_entity)
    meters = sorted({device_statistics[key] for binding in bindings for key in binding.device_keys
                     if key in device_statistics})
    temperatures, energies, houses, invalidated, bounds = {}, {}, {}, {}, {}
    topology = probe_topology or {}
    companions = {entity for metadata in topology.values() for entity in metadata.get("companions", [])} - probes
    probe_failures = 0
    # Counter capture has its own settlement state. Keep its period reads
    # sequential rather than racing two calls on the same Household owner.
    for seconds, period in ((300, "5minute"), (3600, "hour")):
        duration = 900 if seconds == 300 else 3600
        read_start = start - timedelta(seconds=duration)
        raw = await history.statistics(read_start, end, probes, period, {"temperature": "°C"}, {"mean", "min", "max"})
        temperatures[duration] = {entity: _temperature_means(rows, start, end, seconds)
                                  for entity, rows in raw.items()}
        companion_rows = {}
        if companions:
            try:
                companion_rows = await history.statistics(read_start, end, companions, period, None, {"mean", "min", "max"})
            except HouseholdReadError:
                # Optional source corroboration cannot erase measured energy.
                probe_failures += 1
        bounds[duration] = {entity: native_probe_bounds(rows, read_start, end, seconds)
                            for entity, rows in {**raw, **companion_rows}.items()}
        cleaned = await read_energy(meters, start, end, period)
        energies[duration] = {entity: _energy_intervals(rows, start, end, seconds)
                             for entity, rows in cleaned.items()}
        invalidated[duration] = {entity: {at.astimezone(timezone.utc).replace(minute=at.minute // 15 * 15)
                                          if duration == 900 else at.astimezone(timezone.utc)
                                          for at in times if start <= at < end}
                                 for entity, times in getattr(cleaned, "invalidated", {}).items()}
        # Reuse the production area-median definition, with explicitly selected
        # room air sensors. A duplicated probe has one physical vote.
        area_by_entity = {entity: room for room, entity in sorted(room_air_probes.items())}
        houses[duration] = house_temperature_quarters(temperatures[duration], area_by_entity)

    actuators = sorted({entity for binding in bindings if not binding.heating_only
                        for entity in binding.actuator_entity_ids if entity not in binding.heating_only_actuator_ids})
    states = await history.states(start, end, actuators, with_attributes=True) if actuators else {}
    cooling = {entity: time_weighted_quarters(rows, start, end, _cooling, min_coverage=1.0)
               for entity, rows in states.items()}

    observations: list[dict[str, Any]] = []
    counts: Counter = Counter()
    for binding in bindings:
        def observation(at: datetime, duration: int):
            thermal, electrical = temperatures[duration], energies[duration]
            keys = binding.device_keys
            recorded = {key: electrical.get(device_statistics.get(key), {}).get(at) for key in keys}
            room = thermal.get(binding.temperature_entity_id, {}).get(at)
            # Retractions survive resolution selection and invalidate older
            # overlaps before the server stores this newly measured interval.
            withdrew = sorted(key for key in keys if
                _hour(at) in invalidated[3600].get(device_statistics.get(key), ()) or any(
                    at + offset * QUARTER in invalidated[900].get(device_statistics.get(key), ())
                    for offset in range(duration // 900)))
            own_withdrawal = any(at in invalidated[duration].get(device_statistics.get(key), ()) for key in keys)
            if room is None and not own_withdrawal and not any(value is not None for value in recorded.values()):
                return None
            steps = [at + offset * QUARTER for offset in range(duration // 900)]
            return {"room_key": binding.room_key, "source_key": binding.source_key,
                    "start": at.isoformat(), "duration_s": duration,
                    "source": "recorder_5minute_mean" if duration == 900 else "recorder_hour_mean",
                    "room_temperature_c": room,
                    "outdoor_temperature_c": thermal.get(outdoor_entity, {}).get(at),
                    "house_temperature_c": houses[duration].get(at), "solar_w_per_m2": None,
                    "device_statistics": {key: device_statistics.get(key) for key in keys},
                    "device_energy_kwh": recorded, "cooling": _mode(binding, steps, cooling),
                    "invalidated_device_keys": withdrew,
                    "heater_energy_limits_w": dict(binding.heater_limits_w)}

        def complete(row):
            return row is not None and all(row[key] is not None for key in
                ("room_temperature_c", "outdoor_temperature_c", "house_temperature_c", "cooling")) \
                and bool(row["device_energy_kwh"]) and all(value is not None for value in row["device_energy_kwh"].values())

        available = set()
        for duration in (900, 3600):
            available.update(_hour(at) for at in temperatures[duration].get(binding.temperature_entity_id, {}))
            for key in binding.device_keys:
                available.update(_hour(at) for at in energies[duration].get(device_statistics.get(key), {}))
                available.update(_hour(at) for at in invalidated[duration].get(device_statistics.get(key), ()))
        for hour in sorted(available):
            fine = [observation(hour + offset * QUARTER, 900) for offset in range(4)
                    if start <= hour + offset * QUARTER and hour + (offset + 1) * QUARTER <= end]
            coarse = observation(hour, 3600) if start <= hour and hour + HOUR <= end else None
            if len(fine) == 4 and all(complete(row) for row in fine):
                selected = fine
            elif complete(coarse):
                selected = [coarse]
            else:
                selected = [row for row in fine if row is not None] or ([coarse] if coarse is not None else [])
            for row in selected:
                observations.append(row)
                counts["quarter_intervals" if row["duration_s"] == 900 else "hour_intervals"] += 1
                if not complete(row):
                    counts["incomplete_intervals"] += 1
                if row["cooling"] is None:
                    counts["cooling_unobserved_intervals"] += 1
                if any(value is None for value in row["device_energy_kwh"].values()):
                    counts["incomplete_meter_intervals"] += 1
    observations.sort(key=lambda row: (row["start"], row["room_key"]))
    buckets = sorted({(row["start"], row["duration_s"]) for row in observations})
    facts = [probe_interval_facts(datetime.fromisoformat(at), duration, room_air_probes,
                topology, bounds[duration], temperatures[duration]) for at, duration in buckets]
    if probe_failures:
        counts["probe_evidence_read_failures"] = probe_failures
    return HeatingHistoryCapture(observations, dict(counts), facts)
