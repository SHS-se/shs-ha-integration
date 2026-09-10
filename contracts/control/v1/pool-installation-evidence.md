# Pool installation evidence and missing setup

Read-only HA survey, 10 September 2026, approximately 13:00 Europe/Stockholm.
Evidence was obtained using entity search, registry lookup, current states,
helper configuration and the persisted SHS options. No hardware/configuration
writes occurred. Entity names below are evidence for this home, not presets or
bindings in executable code. UUIDs and fixture names elsewhere are synthetic.

## Observed service paths

| Role | Evidence | Consequence under the agreed customer-automation boundary |
| --- | --- | --- |
| Nibe band | Persisted SHS options reference `number.pool_1_start_temperature_40688` and `number.pool_1_stop_temperature_40690`. Registry platform `nibe_heatpump`; unique IDs end in `-40688` / `-40690`. Observed band 29.5–30.0 °C, step 0.1; hardware ranges 5–79.5 / 5.5–80 °C. | Customer automation owns both registers; they are not new SHS bindings. Hardware bounds are not reviewed pool operating limits. |
| Nibe heat permission | `switch.pool_heater` is a HA switch group whose current sole member is `switch.pool_1_activated_40692` (`nibe_heatpump`). | Current group is not an electric resistance-heater relay. Customer automation owns permission. Preserve historical identity; do not extrapolate current membership backwards. |
| Circulation | `switch.esphome_pool_pump_switch`; registry domain `switch`, platform `esphome`. Associated power is `sensor.esphome_pool_pump_power`. | Customer automation owns circulation and sequencing. SHS can observe pump energy without controlling the switch. |
| Existing SHS overlap | `sensor.pool_heater_energy` mapping lists the heater group plus pump companion; `sensor.pool_pump_energy` mapping independently lists the same pump switch. | Visible `legacy_pool_ownership_overlap`; retire direct mappings through reviewed handover, not by adding a second owner. |
| Pool room floor heating | Category list includes `sensor.pool_room_floor_heater_energy`, but its saved setpoint mapping uses `climate.pool_bathroom_floor_thermostat` and room area `basement_bathroom`. | Explicit room association; not pool-water service, regardless of category. |
| Electrical heat-pump allocation | UI template `sensor.heat_pump_pool_power` reads `sensor.instantaneous_used_power_32167` only when `sensor.priority_31029` is `POOL`, otherwise zero. `sensor.heat_pump_pool_energy` integrates it (left, k prefix, hours). | Evidenced candidate electrical input allocated by Nibe priority; auxiliary coverage and accounting overlap still need verification. |
| Old named pool meter | `sensor.pool_heater_energy` and `sensor.pool_heater_power` have template registry identities; their observed values matched the newer heat-pump pool series during the survey. | Matching values do not prove template lineage or historical continuity. `pool_meter_lineage_unverified` remains open. Preserve both statistics; do not count both. |
| Pump accounting overlap | A helper's saved template states the pump moved onto the floor-heater circuit on 2026-08-24, with a 203.887 kWh pump baseline. Net floor-heater energy subtracts only pump accumulation above that baseline. Another aggregate still lists both raw floor-heater power and pump power. | This is configuration evidence, not a wiring audit. `shared_circuit_accounting_unverified`; do not introduce another count or silently rekey history. |
| Water-temperature chain | SHS uses `sensor.filtered_pool_water_temperature` (platform `filter`, unique ID `filtered_pool_water_temperature`). `sensor.pool_temperature_sensor_temperature` is a Shelly temperature sensor. | Filter input/config and effective freshness chain still require evidence. `temperature_chain_unverified`; do not assume the similarly named sensor is the filter source. |
| Nibe temperature/status candidates | `sensor.pool_bt51_30028`, `sensor.priority_31029`, `sensor.pool_1_pump_status_31829`, `sensor.pool_1_qn19_31135`, compressor status. Priority observed `POOL`; pump/valve values observed `1`. | Customer may use these to report feedback. Numeric state meanings and physical heat delivery have not been validated by this survey. |
| Existing customer logic | Season helper `input_boolean.pool_heating`, nominal/target temperature helpers and Node-RED pool status entities exist. User confirms external Node-RED/HA automations own the specialized behavior. | Keep that logic in the customer's domain. Internal triggers/order are not SHS design work. |

## Required setup report before acceptance

| Setup item | Status / exact missing evidence |
| --- | --- |
| `roles.request` | `customer_interface_missing`: bind a dedicated customer HA script implementing the complete heat/defer/release request contract. Existing Nibe switches are not that interface. |
| `roles.feedback` | `customer_feedback_missing`: request-ID/revision-correlated acknowledgement and honest operation/release feedback. Existing generic status entities do not prove this protocol. |
| Customer request lifetime | `customer_expiry_policy_missing`: demonstrate idempotence, stale-request rejection, expiry/release behavior and restoration to the customer's normal policy. |
| Objective | `objective_review_required`: observed Nibe 29.5–30.0 differs from an existing 30.2 °C helper target. Do not declare either a newly accepted website objective without explicit reconciliation. |
| Operating limits | `reviewed_pool_limits_missing`: persisted SHS options did not include reviewed pool min/max. The synthetic fixture's 20–32 °C range is not an installation recommendation. |
| Water temperature | `temperature_chain_unverified`: establish actual upstream sensor, filtering delay and effective sample age. |
| Heating observation | `heating_feedback_unverified`: define which customer feedback proves heating, limited deferral or unavailable heat; schedule acknowledgement alone cannot. |
| Energy model | `pool_meter_lineage_unverified` and `shared_energy_allocation_unverified`: inspect named template lineage and historical source-change dates; establish compressor/auxiliary coverage and mutually exclusive plant allocation. |
| Circulation energy | `shared_circuit_accounting_unverified`: reconcile pump/floor-heater aggregate and historical baseline; retain the pump as observation only. |
| Old SHS ownership | `legacy_pool_ownership_overlap`: review release of both old mappings and any persisted ownership before binding the customer interface. No live permission was changed by this work. |

The request/feedback interface is intentionally missing setup, not a promise to
build the customer's automation in SHS. Step 1 is complete with these gaps visible;
acceptance, live control and physical handover require later implementation and
commissioning. The survey does not establish that the named pool meter measured
Nibe electricity throughout its historical lifetime.
