/** Static shapes for schema.json. Runtime validation remains authoritative for
 * numeric bounds, required conditional fields and cross-field relationships.
 * Keep this file with the byte-identical v1 bundle in both repositories.
 */
export type Provenance = {
  owner: "website" | "ha" | "model" | "migration";
  source: "user" | "discovered" | "learned" | "measured" | "migrated";
  evidence: string;
};

export type Estimate = {
  value: number;
  provenance: Provenance;
};

export type Contract = {
  name:
    | "battery_dispatch"
    | "pool_service"
    | "relay_schedule"
    | "temperature_target"
    | "adjustable_output"
    | "permission";
  version: 1;
};

export type Revision = {
  desired: number;
  binding: number;
  contract: Contract;
  planning_model: number;
};

export type Association = {
  kind: "battery";
  id: string;
} | {
  kind: "pool";
  id: string;
} | {
  kind: "room";
  id: string;
} | {
  kind: "ev";
  id: string;
} | {
  kind: "hot_water";
  id: string;
} | {
  kind: "timed_load";
  id: string;
};

export type BatteryObjective = {
  kind: "battery_policy";
  allow_grid_charge: boolean;
  allow_battery_export: boolean;
  reserve_soc_pct: number;
  export_reserve_soc_pct: number;
  maximum_soc_pct: number;
};

export type PoolObjective = {
  kind: "pool_temperature";
  start_c: number;
  stop_c: number;
  defer_policy: "best_effort_local_thermostat";
};

export type Objective = BatteryObjective | PoolObjective | {
  kind: "room_comfort";
  minimum_c: number;
  maximum_c: number;
} | {
  kind: "ev_charge";
  target_soc_pct: number;
  deadline_utc: string;
} | {
  kind: "timed_run";
  duration_minutes: number;
  deadline_utc: string;
} | {
  kind: "hot_water_service";
  minimum_c: number;
  maximum_c: number;
};

export type Source = {
  source_id: string;
  role:
    | "electrical_energy"
    | "electrical_power"
    | "temperature"
    | "soc"
    | "status"
    | "thermal_energy";
  provenance: Provenance;
  statistic_id?: string;
  accounting: "canonical" | "alias" | "derived" | "observation";
  canonical_source_id?: string;
  derived_from?: Array<string>;
};

export type SourceLink = {
  source_id: string;
  purpose: "consumption" | "storage_flow" | "temperature" | "soc" | "status";
  accounting_use: "count" | "observe";
};

export type Capability = {
  role: string;
  operation: "request_state" | "set_number" | "select" | "switch" | "observe";
  unit: "W" | "kW" | "\u00b0C" | "%" | "A" | "state" | "kWh";
  minimum?: number;
  maximum?: number;
  step?: number;
  options?: Array<string>;
};

export type Gap = {
  code: string;
  path: string;
  message: string;
};

export type BatteryLimits = {
  charge_max_w: number;
  discharge_max_w: number;
  normal_charge_w: number;
  normal_discharge_w: number;
  minimum_soc_pct: number;
  maximum_soc_pct: number;
};

export type PoolLimits = {
  minimum_c: number;
  maximum_c: number;
  step_c: number;
};

export type Local = {
  binding_revision: number;
  capabilities: Array<Capability>;
  limits: BatteryLimits | PoolLimits | {
    minimum: number;
    maximum: number;
    unit: string;
  } | {
    minimum_on_seconds: number;
    minimum_off_seconds: number;
  } | {
    maximum_inhibit_seconds: number;
  } | null;
  handover:
    | "reviewed_self_consumption"
    | "release_customer_automation"
    | "restore_reviewed_profile";
  supported_intents?: Array<
    | "self_consumption"
    | "solar_charge"
    | "charge_pv_first"
    | "charge_grid_first"
    | "discharge_pv_first"
    | "hold"
  >;
  setup_gaps: Array<Gap>;
};

export type Desired = {
  revision: number;
  included: boolean;
  association: Association;
  objective: Objective | null;
  energy_model: {
    revision: number;
    load_characteristic: "storage" | "fixed" | "variable" | "thermostatic";
    running_power_w?: Estimate;
    thermal_conversion?: Estimate;
    thermal_loss_kw_per_c?: Estimate;
  };
  provenance: Provenance;
};

export type Control = {
  control_id: string;
  presentation: {
    name: string;
    category: string;
  };
  contract: Contract;
  sources: Array<SourceLink>;
  desired: Desired;
  local: Local;
};

export type Definition = {
  kind: "definition";
  schema_version: 1;
  home_id: string;
  sources: Array<Source>;
  controls: Array<Control>;
};

export type BatteryCommand = {
  intent:
    | "self_consumption"
    | "solar_charge"
    | "charge_pv_first"
    | "charge_grid_first"
    | "discharge_pv_first"
    | "hold";
  charge_ceiling_w: number;
  discharge_ceiling_w: number;
};

export type PoolCommand = {
  request: "heat" | "defer";
  start_c: number;
  stop_c: number;
  defer_policy: "best_effort_local_thermostat";
};

export type Command = {
  control_id: string;
  accepted: Revision;
  instruction: BatteryCommand | PoolCommand;
  forecast: {
    electrical_power_w: number;
  };
};

export type Plan = {
  kind: "plan";
  schema_version: 1;
  home_id: string;
  plan_id: string;
  valid_from_utc: string;
  valid_until_utc: string;
  commands: Array<Command>;
};

export type Runtime = {
  kind: "runtime";
  schema_version: 1;
  home_id: string;
  control_id: string;
  accepted: Revision | null;
  active: Revision | null;
  permission: {
    revision: number;
    enabled: boolean;
  };
  status: {
    setup: "ready" | "missing" | "invalid";
    synchronization: "pending" | "accepted" | "conflict";
    plan: "none" | "waiting" | "active" | "expired";
    operation:
      | "disabled"
      | "scheduled"
      | "observed_heating"
      | "limited_deferral"
      | "unavailable_heat"
      | "constrained"
      | "overridden"
      | "handover"
      | "handover_failed";
    reason: string;
  };
  lease: null | {
    plan_id: string;
    expires_at_utc: string;
  };
  observations: Array<{
    source_id: string;
    observed_at_utc: string;
    value: number | string | boolean;
  }>;
};

export type Registry = {
  domain: string;
  platform: string;
  unique_id: string;
};

export type BindingRole = {
  role: string;
  registry: Registry;
  last_entity_id?: string;
  members?: Array<Registry>;
  provenance: Provenance;
};

export type LocalBinding = {
  kind: "local_binding";
  schema_version: 1;
  home_id: string;
  control_id: string;
  binding_revision: number;
  roles: Array<BindingRole>;
};

export type MigrationRow = {
  legacy_key: string;
  control_id: string;
  association: Association | null;
  status: "mapped" | "needs_setup" | "monitor_only";
  preserved_statistic_ids: Array<string>;
  gaps: Array<Gap>;
};

export type Migration = {
  kind: "migration";
  schema_version: 1;
  home_id: string;
  migration_id: "control-definitions-v1";
  rows: Array<MigrationRow>;
  history_action: "preserve_keys_and_samples";
  permission_action: "preserve_disabled_do_not_grant";
  ownership_action: "release_old_journal_before_reassignment";
};

export type PoolRequest = {
  kind: "pool_request";
  schema_version: 1;
  home_id: string;
  control_id: string;
  request_id: string;
  accepted: Revision;
  action: "heat" | "defer" | "release";
  issued_at_utc: string;
  expires_at_utc: string;
  objective: PoolObjective | null;
  request_sequence: number;
  plan_id: string | null;
};

export type PoolFeedback = {
  kind: "pool_feedback";
  schema_version: 1;
  home_id: string;
  control_id: string;
  request_id: string;
  accepted: Revision;
  reported_at_utc: string;
  acknowledgement: "accepted" | "rejected" | "released" | "release_failed";
  operation:
    | "scheduled"
    | "observed_heating"
    | "limited_deferral"
    | "unavailable_heat"
    | "unknown"
    | "released";
  reason: string;
  request_sequence: number;
  plan_id: string | null;
};

export type ControlDocument =
  | Definition
  | Plan
  | Runtime
  | LocalBinding
  | Migration
  | PoolRequest
  | PoolFeedback;
