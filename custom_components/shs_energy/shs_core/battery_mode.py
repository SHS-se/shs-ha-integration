"""The planner-stated household supply law (battery execution contract v2).

The planner chooses how supply responds to live demand; this module only
evaluates that statement. All powers are AC watts at the household boundary.
"""
from __future__ import annotations

from math import isfinite

SUPPLY_MODES = ("none", "follow_demand", "import_target", "fixed")


def _watts(*values):
    for value in values:
        if type(value) not in (int, float) or not isfinite(value) or value < 0:
            raise ValueError("supply law requires finite nonnegative watts")


def import_support_w(house_w, pv_w, pending_import_w, import_target_w):
    """Supply needed to keep whole-house import at the stated target."""
    _watts(house_w, pv_w, pending_import_w, import_target_w)
    return max(0.0, house_w + pending_import_w - pv_w - import_target_w)


def supply_request_ac_w(mode, *, eligible_bound_w, discharge_ac_w, import_target_w,
                        house_w, pv_w, pending_import_w):
    """AC discharge the stated response requests, before ceilings and reserves.

    The eligible bound is the planner's demand profile measured live. No
    response supplies demand outside it, whatever import it would avoid.
    """
    _watts(eligible_bound_w)
    if mode == "follow_demand":
        return float(eligible_bound_w)
    need = 0.0 if import_target_w is None else import_support_w(house_w, pv_w, pending_import_w, import_target_w)
    if mode == "import_target":
        return min(float(eligible_bound_w), need)
    if mode == "fixed":
        _watts(discharge_ac_w)
        return min(float(eligible_bound_w), max(float(discharge_ac_w), need))
    raise ValueError("household supply has no stated response")


def promised_credit_w(*, import_after_w, import_target_w, eligible_bound_w, discharge_ceiling_ac_w):
    """Discharge a capped response promises for import being admitted.

    No solar credit: admission compares gross demand with the target.
    """
    _watts(import_after_w, import_target_w, eligible_bound_w, discharge_ceiling_ac_w)
    return max(0.0, min(float(discharge_ceiling_ac_w), float(eligible_bound_w), import_after_w - import_target_w))


def read_cover_offer(value):
    """Validate the app's published promise for the gateway's final admission.

    `meters` are the power sensors that define the instruction's eligible
    demand: subtracted from the house total when base is included, otherwise
    summed. None withdraws the promise.
    """
    if value is None:
        return None
    if type(value) is not dict or set(value) != {"import_target_w", "discharge_ac_max_w", "include_base", "meters", "valid_until_ms"}:
        raise ValueError("unknown or missing cover offer fields")
    for key in ("import_target_w", "discharge_ac_max_w", "valid_until_ms"):
        if type(value[key]) is not int or value[key] < 0:
            raise ValueError("cover offer needs integer watts and validity")
    meters = value["meters"]
    if (type(value["include_base"]) is not bool or type(meters) is not list
            or any(type(m) is not str or not m.startswith("sensor.") for m in meters) or sorted(set(meters)) != meters):
        raise ValueError("cover offer needs explicit sorted power sensors")
    return {**value, "meters": tuple(meters)}
