# 0.8.0-beta.25 — Per-device plan execution

Deploy the accompanying website schema-7 contract and optimiser first, then
upgrade HA. The website continues serving schema 5/6 to existing clients during
the coordinated release. This integration requests schema 7; it refuses an
older server instead of inventing device commands. Cached schema-6 plans can
still serve the existing system controllers, but cannot drive new device paths.

The new `device_commands` field names every planned device in every quarter.
It carries room temperature targets, explicit whole-quarter relay runs,
hot-water permit/inhibit decisions, and EV current targets. Watt forecasts
remain measurements/model outputs and do not grant command authority.

- Room relay runs are solved before plan simulation. The search checks comfort
  and power bounds, including mixed relay/thermostat rooms. It retains up to
  64 thermal states and supports at most eight relays per room. An infeasible
  discrete schedule makes the scenario infeasible, with a reason.
- Setpoints use the planned next-quarter temperature within the planner's
  comfort envelope and locally reviewed bounds. Only active Celsius heating
  thermostats or Celsius number entities are supported; HVAC mode is preserved.
- Whole-quarter switch commands honour reviewed minimum on/off times of up to one quarter. A
  handover waits in the journal if an immediate restoration would violate a
  minimum run time. Hot-water inhibition is limited by the reviewed maximum.
- EV variable power continues through its existing current/start-stop executor
  and reviewed amp envelope. Other variable-power units, arbitrary modes,
  offsets, coupled actuators, and non-room relay planning remain
  explicitly unsupported until their planning contract exists.
- Each new local control is off initially. Its enable field requires an
  executable current plan, and saving enabled setup requires valid local
  limits. Assign control ownership and disable competing automations first.
- Shared actuators are refused. External changes suspend control and preserve
  the changed value; switch local control off and on to resume. Exclusion or a
  method change on the website hands the device back even with an older plan.
- Device diagnostics and the Devices controller sensor show commands, faults,
  overrides, unsupported plans, and pending restoration. Acknowledged targets
  are not reported as delivered heat or power.

Config-entry version 4 prevents older builds from loading an installation with
new controller journal records. Existing settings and device/history identities
are retained; no new execution permission is granted by migration.

Validation: 326 Python tests, three frontend tests, 76 server optimiser/contract
tests, 46 portal plan-reader tests, website typechecking, and a server-generated
schema-7 fixture validated by HA. Deployment and live actuator verification belong to
Phase 5 and have not been performed.

Climate handling follows HA's [state and service implementation](https://github.com/home-assistant/core/blob/dev/homeassistant/components/climate/__init__.py): state temperatures use the configured system unit, and `set_temperature` does not require changing HVAC mode.
