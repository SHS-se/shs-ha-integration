# Battery execution contract v2

User requirement, 17 October 2026 (evening). The planner states each interval's
battery mode and the exact demand the battery may cover. HA applies that law to
live measurements and invents no rule of its own. The planner's website shows
the same instruction it sends to HA.

## Versioning

- HA sends snapshot schema 10 and accepts plan schema 10. For a schema-10
  snapshot the planner returns plan `schema_version: 10` with
  `battery_execution.schema = "battery-plan-execution-v2"`.
- For schema-9 snapshots the planner keeps returning v1. Its `follows_demand`
  is true only when the published discharge ceiling covers the served residual
  demand, so an old client caps a partial allocation instead of draining.
- HA reads v1 and v2 into the same domain. A v1 interval executes as it always
  meant: `follows_demand` means follow demand, otherwise a fixed figure with no
  import target, over the v1 `battery_supply_scope`.

## Demand (contract field `demand`)

```json
{
  "loads": [
    {"key": "sensor.car_charging_total_energy", "name": "Car charging total", "category": "ev_charging"},
    {"key": "sensor.pool_heater_energy", "name": "Pool heater", "category": "pool_heating"},
    {"key": "sensor.pool_pump_energy", "name": "Pool pump", "category": "pool_heating"},
    {"key": "sensor.water_boiler_energy", "name": "Water boiler", "category": "hot_water"}
  ],
  "profiles": [
    {"id": "base+loads", "include_base": true,
     "eligible_keys": ["sensor.pool_heater_energy", "sensor.pool_pump_energy", "sensor.water_boiler_energy"]},
    {"id": "base+loads-hot-water", "include_base": true,
     "eligible_keys": ["sensor.pool_heater_energy", "sensor.pool_pump_energy"]}
  ]
}
```

- `loads` lists every planned or monitored load the planner separated from base
  load: exactly the plan's `device_models`, one entry per key, sorted by key.
  Devices whose planning role is base load are not listed and stay in base.
- Base load = house consumption minus every listed load.
- A profile's eligible demand = base (when `include_base`) plus its eligible
  loads. Keys are sorted, unique and a subset of `loads`. Profile ids are unique.
- The planner owns eligibility. Today: base and every load except EV charging;
  hot water only in intervals where the plan allows the battery to supply it.
- HA measures each listed load with that key's configured Power sensor. It does
  not consult its own planned-device list, device categories or hot-water
  permissions. A device enrolled after the plan is not listed, so it stays in
  base exactly as that plan's base forecast assumed. A listed load whose power
  cannot be read makes battery supply unavailable: HA holds the last setting.

## Interval instruction (interval field `instruction`)

All powers are integer AC watts at the household boundary.

```json
{"kind": "idle"}
{"kind": "hold", "charge_ac_limit_w": 8800}
{"kind": "solar_charge", "charge_ac_limit_w": 8800}
{"kind": "grid_charge", "charge_ac_w": 6000}
{"kind": "supply", "profile_id": "base+loads", "charge_ac_limit_w": 8800, "discharge_ac_limit_w": 9600,
 "response": {"mode": "follow_demand"}}
{"kind": "supply", "profile_id": "base+loads", "charge_ac_limit_w": 8800, "discharge_ac_limit_w": 9600,
 "response": {"mode": "import_target", "import_target_w": 15000}}
{"kind": "supply", "profile_id": "base+loads", "charge_ac_limit_w": 8800, "discharge_ac_limit_w": 9600,
 "response": {"mode": "fixed", "discharge_ac_w": 864, "import_target_w": 15000}}
{"kind": "export", "profile_id": "base+loads", "discharge_ac_w": 5000}
```

- `hold`, `solar_charge` and `supply` keep the plant's surplus-charge permission
  up to `charge_ac_limit_w`; `idle` closes it.
- A fixed house-supply discharge always carries the house import limit as its
  `import_target_w` (user requirement); `null` is reserved and means no target.
- Removed from v2 intervals: `operation`, `follows_demand`,
  `grid_charge_allowed`, `export_allowed`, `charge_ac_limit_w`,
  `discharge_ac_limit_w`. The reference energies (`charge_ac_mwh` … `rounding_mwh`),
  `stored_*`, `target_kind` and timing are unchanged and remain accounting only.
- Plan schema 10 has no `battery_supply_scope`; the contract's `demand` replaces it.
- The plan states the same objects for its own views: `plan.battery_demand`
  equals the contract's `demand`, and each selected slot's `battery_instruction`
  is copied verbatim into its interval. The schema-3 `battery_command` remains
  for older readers; it cannot express an import target, so a peak-shave quarter
  appears there as self-consumption.

## Planner candidates

The optimiser's chosen candidate is the instruction; nothing is inferred from
forecast flows. Native `self_consumption` follows demand; a sized discharge to
an inventory level is `supply_house` (fixed, defending the house limit); the new
`shave_import` candidate is offered when holding would breach the limit
(planner ABI 11).

## Law HA applies

With H gross house consumption, P solar, R pending reserved import not yet in H,
S the profile's eligible supply bound (existing proportional solar attribution),
X `import_target_w`, F `discharge_ac_w`:

| Response | Requested AC discharge |
| --- | --- |
| follow_demand | S |
| import_target | min(S, max(0, H + R − P − X)) |
| fixed, no target | min(S, F) |
| fixed with target | min(S, max(F, H + R − P − X)) |

Then `discharge_ac_limit_w`, the native rating, the conversion model and the
storage reserve apply. Export keeps its existing sizing with `discharge_ac_w`.

## House import admission

Admission subtracts the larger of:

1. measured battery discharge, capped by the lowest discharge ceiling among the
   observed, in-flight and proposed battery settings (17 October requirement);
2. while the current instruction is `import_target` or fixed with a target, the
   discharge that instruction promises for the import being admitted:
   min(discharge ceiling, S, import after the change − X), with no solar credit.

The promised credit lapses when the app is not connected, a stop or charging
setting is in flight, stored energy is at the reserve, or the instruction's
interval ends. Lapsing only blocks increases; it never writes to equipment.

## Website

The plan view describes the interval instruction and its forecast together, for
example "Covers demand above 15 kW (forecast 0.86 kW). May supply: home, pool
heater, pool pump. Not: car charging." HA's battery explanation uses the same
instruction.
