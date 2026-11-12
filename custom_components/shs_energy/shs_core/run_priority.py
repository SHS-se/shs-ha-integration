"""Live priority across whole heating and charging runs.

User requirement, 10 November 2026. The website planner owns prices,
schedules, targets, comfort requirements and authorised recovery windows. The
quarter says what is authorised now; the whole run says what is urgent. From a
compact summary prepared when a plan is accepted, plus live measurements, this
module estimates how long each eligible load could give up its power and still
meet its accepted objective, and shares the measured import budget in that
order. Nobody ranks devices by hand.

Everything here is arithmetic over a handful of rows. It scans no plan,
reads no history and runs no solver; see docs/run-priority.md.
"""
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from math import floor, isfinite

from .const import EU_AC_PHASE_COUNT, EU_AC_PHASE_VOLTAGE

QUARTER_MS = 900_000
HOUR_MS = 3_600_000
EV_WATTS_PER_AMP = EU_AC_PHASE_COUNT * EU_AC_PHASE_VOLTAGE
# Plan capacity is compared in 100 W steps so neighbouring quarters coalesce.
SPARE_QUANTUM_W = 100
# Two choices within six minutes, or a tenth, of each other have effectively
# the same impact: the current allocation stays to avoid needless switching.
SAME_SLACK_H = 0.1
SAME_SLACK_RATIO = 0.1
# Delivery is integrated between consecutive decisions. A longer silence
# (restart, missing readings) is unknown, never measured zero or full delivery.
DELIVERY_GAP_MS = 5 * 60_000
# A reduction is in flight until the meter shows it. Equipment that has not
# responded within this window keeps being asked, and meanwhile counts as the
# fixed load it measurably is, so another run takes the reduction.
RESPONSE_MS = 60_000
# Without evidence, yield in the earlier order: battery, then car, then heat.
RANK = {'heat': 0, 'ev': 1, 'battery': 2}


def _stamp(text):
    return int(datetime.fromisoformat(text.replace('Z', '+00:00')).timestamp() * 1000)


def _number(value):
    return type(value) in (int, float) and isfinite(value)


def _coalesce(rows):
    """Merge neighbouring rows that carry the same values."""
    merged = []
    for row in rows:
        if merged and merged[-1][1] == row[0] and merged[-1][2:] == row[2:]:
            merged[-1][1] = row[1]
        else:
            merged.append(list(row))
    return merged


def summarize_runs(plan):
    """Compact per-owner run summaries, prepared once when a plan is accepted.

    A charge run lists its authorised windows as
    `[start_ms, end_ms, planned_w, maximum_w, spare_w]`: what the plan
    schedules, what it authorises, and what it leaves unused under the import
    limit for every load together. A heat run lists the room's comfort floor
    over time and how fast the plan expects the room to warm and cool.
    """
    try:
        slots = plan['plans']['priority']['slots']
    except (KeyError, TypeError):
        return {}
    limit = (plan.get('grid') or {}).get('import_limit_w')
    identity = plan.get('plan_id')
    spans, spare = [], {}
    for slot in slots:
        try:
            start = _stamp(slot['start'])
        except (KeyError, TypeError, ValueError, AttributeError):
            return {}
        spans.append((start, start + QUARTER_MS))
        unused = 0
        if _number(limit) and _number(slot.get('grid_import_w')):
            unused = floor(max(0.0, limit - slot['grid_import_w']) / SPARE_QUANTUM_W) * SPARE_QUANTUM_W
        spare[start] = unused
    runs = {}

    car = []
    for slot, (start, end) in zip(slots, spans):
        current, ceiling, planned = (slot.get(key) for key in ('ev_target_current_a', 'ev_max_current_a', 'ev_w'))
        if _number(current) and current > 0:
            nominal = current * EV_WATTS_PER_AMP
            car.append([start, end, round(planned if _number(planned) and planned > 0 else nominal),
                        round(max(nominal, ceiling * EV_WATTS_PER_AMP if _number(ceiling) else 0)), spare[start]])
    if car:
        runs['ev'] = {'kind': 'charge', 'id': f'{identity}:ev', 'windows': _coalesce(car)}

    battery = _battery_run(plan, spare)
    if battery:
        runs['battery'] = battery

    projection = plan.get('thermal_projection') or {}
    starts = projection.get('starts') or []
    for zone in projection.get('zones') or []:
        try:
            run = _heat_run(zone, [_stamp(start) for start in starts])
        except (KeyError, TypeError, ValueError, IndexError, AttributeError):
            continue  # One unreadable room leaves the other runs intact.
        for key in zone.get('device_keys') or ():
            runs['device:' + key] = run
    return runs


def _battery_run(plan, spare):
    """The battery's stored-energy objectives and the windows that may serve them."""
    contract = plan.get('battery_execution')
    if not isinstance(contract, dict):
        return None
    try:
        battery = plan.get('battery') or {}
        rating = battery.get('charge_max_w')
        efficiency = (contract.get('storage') or {}).get('charge_efficiency', battery.get('charge_efficiency', 1.0))
        efficiency = efficiency if _number(efficiency) and 0 < efficiency <= 1 else 1.0
        def window_parts(start, end, planned, maximum):
            # A recipe or nominal interval can straddle forecast quarters.
            while start < end:
                quarter = start - start % QUARTER_MS
                until = min(end, quarter + QUARTER_MS)
                yield [start, until, planned, maximum, spare.get(quarter, 0)]
                start = until

        windows = []
        for row in contract['intervals']:
            kind = (row.get('instruction') or {}).get('kind', row.get('operation'))
            if kind != 'grid_charge':
                continue
            start, end = row['start_ms'], row['end_ms']
            planned = round(row['charge_ac_mwh'] * 3600 / (end - start))
            windows.extend(window_parts(start, end, planned, round(max(planned, rating if _number(rating) else 0))))
        targets = [[o['start_ms'], o['deadline_ms'], round(o['target_mwh'] / 1000)]
                   for o in contract['objectives'] if o['kind'] == 'stored_energy']
        recipes = []
        for row in contract.get('recovery') or ():
            # The planner derives recipes from the same forecast grid
            # headroom. Offer the authorized DC watts as a conservative
            # estimate of their AC requirement, capped by the stored energy.
            start, end = row['start_ms'], row['end_ms']
            hours = (end - start) / HOUR_MS
            extra = floor(min(row['extra_charge_dc_w'], row['max_correction_mwh'] / (1000 * efficiency * hours)))
            recipes.extend(window_parts(start, end, 0, extra))
        windows = sorted([*_coalesce(windows), *_coalesce(sorted(recipes))])
        if not windows or not targets:
            return None
        return {'kind': 'charge', 'id': contract['id'], 'windows': windows,
                'targets': targets, 'capacity_wh': round(contract['capacity_mwh'] / 1000),
                'efficiency': efficiency}
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        return None


def _heat_run(zone, starts):
    floors = zone['comfort_min_c']
    temperature, power, rated = zone['planned_temperature_c'], zone['planned_power_w'], zone['rated_power_w']
    rows = _coalesce([start, start + QUARTER_MS, float(low)] for start, low in zip(starts, floors))
    warm = cool = 0.0
    for index in range(1, min(len(temperature), len(power))):
        change = (temperature[index] - temperature[index - 1]) * 4
        if rated > 0 and power[index] >= rated / 2:
            warm = max(warm, change)
        elif power[index] <= 0:
            cool = max(cool, -change)
    return {'kind': 'heat', 'floor': rows,
            'warm_c_per_h': round(warm, 3) or None, 'cool_c_per_h': round(cool, 3) or None}


def valid_run(value):
    """A summary crosses the gateway as plain JSON; refuse anything else."""
    if value is None:
        return True
    if type(value) is not dict:
        return False
    rows = lambda field, width: (type(value.get(field)) is list and len(value[field]) <= 512 and all(
        type(row) is list and len(row) == width and all(_number(item) for item in row) and row[0] < row[1]
        for row in value[field]))
    rate = lambda field: value.get(field) is None or _number(value[field]) and value[field] > 0
    if value.get('kind') == 'heat':
        return set(value) == {'kind', 'floor', 'warm_c_per_h', 'cool_c_per_h'} and rows('floor', 3) \
            and rate('warm_c_per_h') and rate('cool_c_per_h')
    if value.get('kind') != 'charge' or type(value.get('id')) is not str or not rows('windows', 5):
        return False
    if set(value) == {'kind', 'id', 'windows'}:
        return True
    return (set(value) == {'kind', 'id', 'windows', 'targets', 'capacity_wh', 'efficiency'} and rows('targets', 3)
            and _number(value['capacity_wh']) and value['capacity_wh'] > 0
            and _number(value['efficiency']) and 0 < value['efficiency'] <= 1)


def advance_delivery(progress, run, now_ms, measured_w):
    """Carry planned-minus-measured energy across quarters for one charge run.

    `progress` is the owner's retained row or None. A replacement plan starts a
    new reference, because its snapshot already holds the measured state.
    """
    planned = planned_now(run, now_ms)
    if progress is None or progress.get('id') != run['id']:
        return {'id': run['id'], 'at_ms': now_ms, 'shortfall_wh': 0.0, 'planned_w': planned, 'measured_w': measured_w}
    elapsed = now_ms - progress['at_ms']
    shortfall = progress['shortfall_wh']
    if 0 < elapsed <= DELIVERY_GAP_MS:
        shortfall += (progress['planned_w'] - progress['measured_w']) * elapsed / 3_600_000
    return {'id': run['id'], 'at_ms': now_ms, 'shortfall_wh': shortfall, 'planned_w': planned, 'measured_w': measured_w}


def planned_now(run, now_ms):
    return float(sum(row[2] for row in run['windows'] if row[0] <= now_ms < row[1]))


def responded(draw_w, granted_w):
    """Whether measured draw has come down to a granted share."""
    return draw_w <= granted_w + max(100.0, 0.05 * granted_w)


def _block(run, now_ms, deadline_ms=None):
    """The windows of the run in progress: contiguous for a car, up to the deadline for a target."""
    rows = [row for row in run['windows'] if row[1] > now_ms]
    if deadline_ms is not None:
        return [row for row in rows if row[0] < deadline_ms]
    block = []
    through = now_ms
    for row in rows:
        if row[0] > through:
            break
        block.append(row)
        through = max(through, row[1])
    return block


@dataclass(frozen=True, slots=True)
class RecoveryWindow:
    start_ms: int
    end_ms: int
    owner_w: float
    shared_w: float


@dataclass(frozen=True, slots=True)
class ChargeOutlook:
    required_wh: float
    planned_wh: float
    windows: tuple[RecoveryWindow, ...]


def charge_outlook(run, now_ms, *, shortfall_wh=0.0, stored_wh=None) -> ChargeOutlook:
    """Remaining work and the capacity left in the run's authorised windows (Wh, AC)."""
    target = None
    if stored_wh is not None:
        target = next((row for row in run.get('targets', ()) if row[0] <= now_ms < row[1]), None)
    rows = _block(run, now_ms, target[1] if target else None)
    planned = 0.0
    windows = []
    for start, end, nominal, ceiling, spare in rows:
        start = max(start, now_ms)
        end = min(end, target[1]) if target else end
        planned += nominal * (end - start) / HOUR_MS
        available = min(ceiling - nominal, spare)
        if available > 0:
            windows.append(RecoveryWindow(start, end, available, spare))
    if target:
        required = max(0.0, target[2] - stored_wh) / run['efficiency']
    else:
        required = max(0.0, planned + shortfall_wh)
    return ChargeOutlook(required, planned, tuple(windows))


def _recovery_capacity(outlooks: Mapping[str, ChargeOutlook]) -> tuple[dict[str, float], float]:
    """Integrate each owner's capacity and the maximum they can use together."""
    events = []
    for owner, outlook in outlooks.items():
        for index, window in enumerate(outlook.windows):
            events.extend(((window.start_ms, owner, index, window), (window.end_ms, owner, index, None)))
    active = {owner: {} for owner in outlooks}
    capacity = dict.fromkeys(outlooks, 0.0)
    joint = 0.0
    events.sort()
    through = events[0][0] if events else 0
    for at, owner, index, window in events:
        if at > through:
            hours = (at - through) / HOUR_MS
            total_w = shared_w = 0.0
            for candidate, offers in active.items():
                # Nominal spare and a recipe are alternate offers to the same
                # charger. Scoped plans can offer different amounts of spare
                # on the same connection; each owner keeps its own cap.
                owner_w = 0.0
                for offer in offers.values():
                    owner_w = max(owner_w, offer.owner_w)
                    shared_w = max(shared_w, offer.shared_w)
                capacity[candidate] += owner_w * hours
                total_w += owner_w
            joint += min(total_w, shared_w) * hours
            through = at
        if window is None:
            del active[owner][index]
        else:
            active[owner][index] = window
    return capacity, joint


def share_recovery(outlooks: Mapping[str, ChargeOutlook]) -> dict[str, float]:
    """Give the more pressing charger its claim, with Wh still usable by each.

    With two chargers, their individual and joint capacities fully describe
    what fits. A claim need not pick particular hours, so a charger can still
    use its other windows when the first can recover elsewhere.
    """
    assert len(outlooks) <= 2
    capacity, joint = _recovery_capacity(outlooks)
    slack = {}
    used = 0.0
    for owner, outlook in sorted(outlooks.items(), key=lambda item: (
            item[1].planned_wh + capacity[item[0]] - item[1].required_wh, item[0])):
        available = min(capacity[owner], max(0.0, joint - used))
        used += min(available, max(0.0, outlook.required_wh - outlook.planned_wh))
        slack[owner] = outlook.planned_wh + available - outlook.required_wh
    return slack


def charge_slack_h(slack_wh, power_w):
    """Hours this run could go without its present power and still finish."""
    return slack_wh / max(power_w, 1.0)


def material_capacity_shortfall(candidate, granted_w):
    """Use the smallest useful planning change for the run being trimmed."""
    if granted_w >= candidate['want_w'] - 1e-6:
        return False
    if candidate['kind'] == 'heat':
        # The planner can move a heater by a quarter. A smaller comfort
        # deficit can be monitored while the room continues to resume locally.
        return candidate['slack_h'] <= -QUARTER_MS / HOUR_MS
    if candidate['kind'] == 'ev' and 'slack_wh' in candidate:
        return -candidate['slack_wh'] >= candidate['step_w'] * QUARTER_MS / HOUR_MS
    return False  # Battery shortfalls are diagnosed by its execution account.


def heat_slack_h(run, now_ms, temperature_c):
    """How long the room can pause while meeting its accepted comfort floors.

    A room that warms slowly needs longer to make up a setback, so the same
    margin buys it less. Without a known floor or cooling rate the room is
    treated as already at its limit rather than given an invented margin.
    """
    if run is None:
        return 0.0
    floor_c = next((row[2] for row in run['floor'] if row[0] <= now_ms < row[1]), None)
    if floor_c is None:
        return 0.0
    margin = temperature_c - floor_c
    cool, warm = run['cool_c_per_h'], run['warm_c_per_h']
    if cool is None:
        return 0.0 if margin > 0 else margin
    slack = margin / cool
    if warm and slack > 0:
        slack /= 1 + cool / warm
    for start, end, floor_c in run['floor']:
        if start <= now_ms:
            continue
        starts_in = (start - now_ms) / HOUR_MS
        ends_in = (end - now_ms) / HOUR_MS
        margin = temperature_c - floor_c
        crossing = margin / cool
        if crossing >= ends_in:
            continue  # Cooling alone keeps the room above this floor.
        if crossing < starts_in:
            # At the start, a pause of p leaves T - cool*p + warm*(starts_in-p).
            crossing = (margin + warm * starts_in) / (cool + warm) if warm else 0.0
        slack = min(slack, crossing)
    return slack


def step_down(watts, step_w):
    return floor(watts / step_w + 1e-9) * step_w if step_w else watts


def share_budget(budget_w, candidates):
    """Give the import budget to the most pressing runs first.

    Each candidate states `owner`, `kind`, `slack_h`, the power it `want_w`,
    its supported `step_w` (0 when continuous), the least it can run at
    `floor_w`, and whether it is `trimmed` now. A `fixed` candidate has not
    responded to its reduction and simply keeps its measured power. Sizing is absolute, from the
    budget, so a meter that has not caught up cannot ratchet a setting. The
    result both trims and restores: whatever a less pressing run is not given
    is what it yields, in the smallest step its equipment supports.
    """
    def order(candidate):
        if candidate.get('fixed'):
            return float('-inf'), 0, candidate['owner']  # Measured, unresponsive load comes first.
        slack = candidate['slack_h']
        if candidate.get('trimmed'):
            slack += max(SAME_SLACK_H, SAME_SLACK_RATIO * abs(slack))
        return slack, RANK[candidate['kind']], candidate['owner']

    shares, left = {}, max(0.0, budget_w)
    for candidate in sorted(candidates, key=order):
        grant = step_down(min(candidate['want_w'], left), candidate['step_w'])
        if grant < candidate['floor_w']:
            grant = 0.0
        shares[candidate['owner']] = grant
        left -= grant
    return shares, left
