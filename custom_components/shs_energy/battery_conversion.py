"""Battery-terminal power and measured installation conversion losses.

Curves describe reported meter boundaries, not cell-internal roundtrip efficiency.
Neither percentages clipped for display nor forecast power are fit observations.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from hashlib import sha256
import json
from math import isfinite, sqrt
from statistics import median


@dataclass(frozen=True)
class Curve:
    gain: float
    overhead_w: float

    def __post_init__(self):
        if any(type(x) not in (int, float) or not isfinite(x) for x in (self.gain, self.overhead_w)):
            raise ValueError("conversion curve needs finite numbers")
        if not 0 < self.gain <= 1 or not 0 <= self.overhead_w <= 10000:
            raise ValueError("conversion gain/overhead outside physical bounds")

    def output(self, input_w):
        return max(0.0, self.gain * input_w - self.overhead_w) if input_w > 0 else 0.0

    def input(self, output_w):
        return (output_w + self.overhead_w) / self.gain if output_w > 0 else 0.0


@dataclass(frozen=True)
class Conversion:
    revision: str
    grid_charge: Curve
    surplus_charge: Curve
    discharge: Curve
    idle_loss_w: float

    def __post_init__(self):
        if not isinstance(self.revision, str) or not 0 < len(self.revision) <= 128:
            raise ValueError("conversion needs a model revision")
        if type(self.idle_loss_w) not in (int, float) or not isfinite(self.idle_loss_w) or not 0 <= self.idle_loss_w <= 10000:
            raise ValueError("invalid idle loss")

    @classmethod
    def read(cls, value):
        if type(value) is not dict or set(value) != {"revision", "grid_charge", "surplus_charge", "discharge", "idle_loss_w"}:
            raise ValueError("unknown conversion model fields")
        curves = []
        for key in ("grid_charge", "surplus_charge", "discharge"):
            row = value[key]
            if type(row) is not dict or set(row) != {"gain", "overhead_w"}:
                raise ValueError("unknown conversion curve fields")
            curves.append(Curve(**row))
        return cls(value["revision"], *curves, value["idle_loss_w"])

    def wire(self):
        return asdict(self)

    def solar_capacity(self, pv_w, house_w):
        return self.surplus_charge.output(max(0.0, pv_w - house_w))

    def charge_inputs(self, dc_w, pv_w, house_w):
        solar = min(dc_w, self.solar_capacity(pv_w, house_w))
        return self.surplus_charge.input(solar), self.grid_charge.input(max(0.0, dc_w - solar))

    def net_grid(self, charge_dc_w, discharge_dc_w, pv_w, house_w):
        """One site balance; active-curve overhead is not added twice."""
        solar, grid = self.charge_inputs(charge_dc_w, pv_w, house_w)
        idle = self.idle_loss_w if charge_dc_w == discharge_dc_w == 0 else 0.0
        return house_w - pv_w + solar + grid - self.discharge.output(discharge_dc_w) + idle


@dataclass(frozen=True)
class LossWindow:
    """Integrated comparable readings over one stable observation window."""
    start_ms: int
    end_ms: int
    branch: str
    input_wh: float
    output_wh: float
    source_revision: str
    raw_loss_wh: float

    def __post_init__(self):
        if (type(self.start_ms) is not int or type(self.end_ms) is not int or
                not 0 <= self.start_ms < self.end_ms or self.end_ms - self.start_ms > 900000 or
                self.branch not in ("grid_charge", "surplus_charge", "discharge", "idle") or not self.source_revision):
            raise ValueError("invalid conversion observation interval")
        if any(type(v) not in (int, float) or not isfinite(v) for v in (self.input_wh, self.output_wh, self.raw_loss_wh)):
            raise ValueError("invalid conversion observation energy")
        if self.input_wh < 0 or self.output_wh < 0:
            raise ValueError("direction must be isolated before fitting")

    @property
    def hours(self):
        return (self.end_ms - self.start_ms) / 3600000


def fit_branch(windows, branch):
    rows = [w for w in windows if w.branch == branch]
    if not rows:
        return {"state": "no_observations", "windows": 0}
    if len({w.source_revision for w in rows}) != 1:
        raise ValueError("conversion observations use different source mappings")
    inputs = [w.input_wh / w.hours for w in rows]
    outputs = [w.output_wh / w.hours for w in rows]
    total_in = sum(w.input_wh for w in rows)
    evidence = {"windows": len(rows), "input_kwh": total_in / 1000,
                "input_range_w": [min(inputs), max(inputs)],
                "effective_energy_ratio": sum(w.output_wh for w in rows) / total_in if total_in > 0 else None,
                "basis": "reported_site_surplus" if branch == "surplus_charge" else "installation_including_overhead"}
    # Subtracting AC house consumption from PV does not independently identify
    # the PV-to-battery converter. Keep useful balances without calling them a fit.
    if branch == "surplus_charge":
        return {**evidence, "state": "source_paths_not_isolated"}
    if len(rows) < 6 or total_in < 200 or max(inputs) - min(inputs) < 500:
        return {**evidence, "state": "insufficient_power_variation"}
    weights = [w.hours for w in rows]
    duration = sum(weights)
    mean_x = sum(w*x for w,x in zip(weights,inputs)) / duration
    mean_y = sum(w*y for w,y in zip(weights,outputs)) / duration
    variance = sum(w*(x-mean_x)**2 for w,x in zip(weights,inputs))
    gain = sum(w*(x-mean_x)*(y-mean_y) for w,x,y in zip(weights,inputs,outputs)) / variance
    overhead = gain*mean_x-mean_y
    rms = sqrt(sum(w*(y-(gain*x-overhead))**2 for w,x,y in zip(weights,inputs,outputs))/duration)
    if not 0 < gain <= 1 or not 0 <= overhead <= 10000 or rms > max(50, mean_y*.05):
        return {**evidence, "state": "inconsistent_fit", "rms_w": rms}
    return {**evidence, "state": "measured", "curve": asdict(Curve(gain, overhead)), "rms_w": rms}


def conversion_model(windows, *, charge_efficiency, discharge_efficiency, source_revision):
    """Existing configured assumptions remain labelled until a curve is identified.

    This does not switch command paths or silently replace a failed live reading.
    Solar-path calibration needs independent branch information; a site residual
    is retained as evidence, never relabelled as pure DC conversion efficiency.
    """
    rows = [w for w in windows if w.source_revision == source_revision][-2000:]
    fits = {key: fit_branch(rows, key) for key in ("grid_charge", "surplus_charge", "discharge")}
    curves = {}
    for key, fit in fits.items():
        if fit["state"] == "measured":
            curves[key] = Curve(**fit["curve"])
            fit["model_source"] = "measured"
        else:
            curves[key] = Curve(discharge_efficiency if key == "discharge" else charge_efficiency, 0)
            fit["model_source"] = "configured"
    idle = [w.raw_loss_wh/w.hours for w in rows if w.branch == "idle"]
    idle_w = median(idle) if len(idle) >= 6 else 0.0
    if not 0 <= idle_w <= 10000:
        raise ValueError("inconsistent idle-loss observations")
    fits["idle"]={"windows":len(idle),"model_source":"measured" if len(idle)>=6 else "unmeasured_zero", "overhead_w":idle_w}
    identity = {"source": source_revision, "curves": {k:asdict(v) for k,v in curves.items()}, "idle_loss_w": idle_w}
    revision = "sha256:"+sha256(json.dumps(identity,sort_keys=True).encode()).hexdigest()
    return Conversion(revision, curves['grid_charge'], curves['surplus_charge'], curves['discharge'], idle_w), fits


def windows_from_statistics(series, *, source_revision, unit_scale=1000, end_ms):
    """Fit evidence from complete aligned HA five-minute statistics, not live state.

    Min/max reject changing direction or source mixes inside an average. Historical
    aggregates cannot provide current command freshness or physical confirmations.
    """
    if set(series) != {"battery", "house", "pv", "grid"}:
        raise ValueError("four explicit source mappings are required")
    if not isfinite(unit_scale) or unit_scale<=0:
        raise ValueError("positive unit scale required")
    maps = {key: {r['start']:r for r in rows} for key,rows in series.items()}
    result = []
    for start in sorted(set.intersection(*(set(v) for v in maps.values()))):
        if type(start) is not int or start+300000 > end_ms:
            continue
        try:
            b,h,p,g = [{k:float(maps[key][start][k])*unit_scale for k in ('mean','min','max')} for key in ('battery','house','pv','grid')]
            if any(not isfinite(v) for r in (b,h,p,g) for v in r.values()):
                continue
            if any(not r['min']<=r['mean']<=r['max'] for r in (b,h,p,g)) or min(h['min'],p['min'])<0:
                continue
            if any(r['max']-r['min'] > 250 for r in (b,h,p,g)):
                continue
            loss = p['mean']+g['mean']-b['mean']-h['mean']
            if p['max'] <= 20 and b['min'] >= 100:
                branch,x,y = 'grid_charge',g['mean']-h['mean'],b['mean']
            elif p['max'] <= 20 and b['max'] <= -100:
                branch,x,y = 'discharge',-b['mean'],h['mean']-g['mean']
            elif max(abs(g['min']),abs(g['max'])) <= 20 and b['min'] >= 100:
                branch,x,y = 'surplus_charge',p['mean']-h['mean'],b['mean']
            elif p['max'] <= 20 and max(abs(b['min']),abs(b['max'])) <= 20:
                branch,x,y = 'idle',max(0,g['mean']-h['mean']),0
            else:
                continue
            if x < 0 or y < 0:
                continue
            result.append(LossWindow(start,start+300000,branch,x/12,y/12,source_revision,loss/12))
        except (KeyError, TypeError, ValueError):
            continue
    return tuple(result)
