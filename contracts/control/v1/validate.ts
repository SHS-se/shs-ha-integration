/** Shared contract validation; not an executor or migration runner.
 * Keep the complete v1 bundle byte-identical in both repositories.
 */
import Ajv from "npm:ajv@6.12.6";

import type { Control, ControlDocument } from "./types.ts";

import schema from "./schema.json" with { type: "json" };
const shape = new Ajv({
  allErrors: true,
  coerceTypes: false,
  useDefaults: false,
}).compile(schema);
const associations: Record<string, string[]> = {
  battery_dispatch: ["battery", "battery_policy"],
  pool_service: ["pool", "pool_temperature"],
  temperature_target: ["room", "room_comfort"],
  adjustable_output: ["ev", "ev_charge"],
  permission: ["hot_water", "hot_water_service"],
  relay_schedule: ["timed_load", "timed_run"],
};
function require(condition: unknown, code: string): asserts condition {
  if (!condition) throw new Error(code);
}
function unique(values: unknown[]) {
  require(values.length === new Set(values).size, "duplicate_identity");
}
function equal(a: unknown, b: unknown): boolean {
  if (a === b) return true;
  if (!a || !b || typeof a !== "object" || typeof b !== "object") return false;
  return Object.keys(a).length === Object.keys(b).length &&
    Object.keys(a).every((k) =>
      Object.hasOwn(b, k) &&
      equal(
        (a as Record<string, unknown>)[k],
        (b as Record<string, unknown>)[k],
      )
    );
}
function utc(value: string): number {
  require(
    value.endsWith("Z") && Number.isFinite(Date.parse(value)),
    "invalid_time",
  );
  return Date.parse(value);
}
export function revision(c: Control) {
  return {
    desired: c.desired.revision,
    binding: c.local.binding_revision,
    contract: c.contract,
    planning_model: c.desired.energy_model.revision,
  };
}

/** Passing this check never grants execution permission. */
export function validate(input: unknown, context?: unknown): void {
  require(shape(input), "schema");
  // The schema establishes these shapes before semantic checks read fields.
  const document = input as ControlDocument;
  const kind = document.kind;
  if (kind === "definition") {
    unique(document.sources.map((s) => s.source_id));
    unique(
      document.sources.filter((s) => s.statistic_id !== undefined).map((s) =>
        s.statistic_id
      ),
    );
    unique(document.controls.map((c) => c.control_id));
    const sources = new Map(
      document.sources.map((s) => [s.source_id, s]),
    );
    for (const source of sources.values()) {
      if (source.accounting === "alias") {
        require(
          sources.get(source.canonical_source_id)?.accounting === "canonical",
          "unknown_source",
        );
      }
      require(
        (source.derived_from ?? []).every((p) =>
          sources.has(p) && p !== source.source_id
        ),
        "unknown_source",
      );
    }
    const counted = new Set<string>();
    for (const c of document.controls) {
      const name = c.contract.name, desired = c.desired, local = c.local;
      if (desired.objective === null || local.limits === null) {
        require(local.setup_gaps.length > 0, "missing_setup_reason");
      }
      const allowed = [associations[name]];
      if (name === "relay_schedule") allowed.push(["room", "room_comfort"]);
      require(
        allowed.some(([kind, objective]) =>
          desired.association.kind === kind &&
          (desired.objective === null || desired.objective.kind === objective)
        ),
        "association_mismatch",
      );
      unique(local.capabilities.map((cap) => cap.role));
      if (local.setup_gaps.length === 0) {
        const requiredRoles: Record<string, string[]> = {
          pool_service: ["request", "feedback", "water_temperature"],
          battery_dispatch: [
            "mode",
            "charge_ceiling",
            "discharge_ceiling",
            "authority",
            "authority_confirmation",
            "battery_power",
            "soc",
            "soc_floor",
            "pv_power",
            "load_power",
            "grid_import_power",
            "grid_export_power",
          ],
        };
        const roles = requiredRoles[name] ?? [];
        require(
          roles.every((r) => local.capabilities.some((cap) => cap.role === r)),
          "missing_capability",
        );
      }
      for (const cap of local.capabilities) {
        if (cap.minimum !== undefined && cap.maximum !== undefined) {
          require(cap.minimum <= cap.maximum, "invalid_limits");
        }
      }
      const limits = local.limits, objective = desired.objective;
      if (name === "pool_service") {
        require(
          local.handover === "release_customer_automation",
          "customer_boundary",
        );
        require(
          local.capabilities.every((cap) =>
            ["request", "feedback", "water_temperature"].includes(cap.role)
          ),
          "customer_boundary",
        );
        const operations: Record<string, string[]> = {
          request: ["request_state", "state"],
          feedback: ["observe", "state"],
          water_temperature: ["observe", "°C"],
        };
        require(
          local.capabilities.every((cap) =>
            equal([cap.operation, cap.unit], operations[cap.role])
          ),
          "customer_boundary",
        );
      }
      if (name === "pool_service" && limits !== null && objective !== null) {
        require(objective.kind === "pool_temperature", "association_mismatch");
        require("minimum_c" in limits && "step_c" in limits, "invalid_limits");
        require(
          equal(Object.keys(limits).sort(), [
            "maximum_c",
            "minimum_c",
            "step_c",
          ]),
          "invalid_limits",
        );
        require(
          limits.minimum_c <= objective.start_c &&
            objective.start_c < objective.stop_c &&
            objective.stop_c <= limits.maximum_c,
          "invalid_band",
        );
        for (const key of ["start_c", "stop_c"] as const) {
          const ticks = (objective[key] - limits.minimum_c) / limits.step_c;
          require(Math.abs(ticks - Math.round(ticks)) < 1e-7, "invalid_band");
        }
      } else if (
        name === "battery_dispatch" && limits !== null && objective !== null
      ) {
        require("charge_max_w" in limits, "invalid_limits");
        require(objective.kind === "battery_policy", "association_mismatch");
        require(
          0 <= limits.normal_charge_w &&
            limits.normal_charge_w <= limits.charge_max_w &&
            0 <= limits.normal_discharge_w &&
            limits.normal_discharge_w <= limits.discharge_max_w,
          "invalid_limits",
        );
        require(
          limits.minimum_soc_pct <= objective.reserve_soc_pct &&
            objective.reserve_soc_pct <= objective.export_reserve_soc_pct &&
            objective.export_reserve_soc_pct <= objective.maximum_soc_pct &&
            objective.maximum_soc_pct <= limits.maximum_soc_pct,
          "invalid_limits",
        );
        require(
          local.handover === "reviewed_self_consumption",
          "invalid_handover",
        );
      }
      for (const link of c.sources) {
        const source = sources.get(link.source_id);
        require(source, "unknown_source");
        if (link.accounting_use === "count") {
          require(
            source.accounting === "canonical" &&
              source.role === "electrical_energy",
            "noncanonical_accounting",
          );
          require(!counted.has(source.source_id), "duplicate_accounting");
          require(name !== "battery_dispatch", "storage_is_not_load");
          counted.add(source.source_id);
        }
      }
    }
    return;
  }
  if (kind === "migration") {
    unique(document.rows.map((r) => r.legacy_key));
    unique(document.rows.flatMap((r) => r.preserved_statistic_ids));
    for (const row of document.rows) {
      require(
        row.status !== "needs_setup" || row.gaps.length > 0,
        "missing_setup_reason",
      );
      require(
        row.status !== "mapped" || row.association !== null,
        "association_mismatch",
      );
    }
    return;
  }
  require(
    context && typeof context === "object" && "kind" in context &&
      context.kind === "definition",
    "definition_required",
  );
  validate(context);
  const definition = context as Extract<
    ControlDocument,
    { kind: "definition" }
  >;
  require(document.home_id === definition.home_id, "home_scope");
  const controls = new Map(
    definition.controls.map((c) => [c.control_id, c]),
  );
  const records = kind === "plan" ? document.commands : [document];
  unique(records.map((r) => r.control_id));
  if (kind === "plan") {
    require(
      utc(document.valid_from_utc) < utc(document.valid_until_utc),
      "invalid_time",
    );
  }
  for (const record of records) {
    const c = controls.get(record.control_id);
    require(c, "unknown_control");
    if (
      "kind" in record &&
      (record.kind === "pool_request" || record.kind === "pool_feedback")
    ) {
      require(c.contract.name === "pool_service", "instruction_mismatch");
      if (record.kind === "pool_feedback") {
        utc(record.reported_at_utc); // Historical feedback never grants authority.
        continue;
      }
      require(equal(record.accepted, revision(c)), "revision_mismatch");
      const lifetime = utc(record.expires_at_utc) - utc(record.issued_at_utc);
      require(lifetime > 0 && lifetime <= 120_000, "invalid_time");
      if (record.action === "release") {
        require(
          record.objective === null && record.plan_id === null,
          "objective_mismatch",
        );
      } else {
        require(record.plan_id !== null, "plan_required");
        require(
          equal(record.objective, c.desired.objective),
          "objective_mismatch",
        );
        require(
          c.local.setup_gaps.length === 0 && c.desired.included,
          "not_ready",
        );
      }
      continue;
    }
    if ("kind" in record && record.kind === "local_binding") {
      require(
        record.binding_revision === c.local.binding_revision,
        "revision_mismatch",
      );
      unique(record.roles.map((r) => r.role));
      if (c.contract.name === "pool_service") {
        for (const role of record.roles) {
          require(
            ["request", "feedback", "water_temperature"].includes(role.role) &&
              !role.members?.length,
            "customer_boundary",
          );
          const domain = role.role === "request" ? "script" : "sensor";
          require(
            role.registry.domain === domain,
            "customer_boundary",
          );
        }
      }
      continue;
    }
    if ("kind" in record && record.kind === "runtime") {
      // Accepted/active can deliberately lag pending desired settings.
      if (record.lease !== null) utc(record.lease.expires_at_utc);
      require(
        record.active === null || record.permission.enabled,
        "permission_off_active",
      );
      require(
        record.active === null || record.lease !== null,
        "active_without_lease",
      );
      continue;
    }
    require(!("kind" in record), "instruction_mismatch");
    require(equal(record.accepted, revision(c)), "revision_mismatch");
    require(c.local.setup_gaps.length === 0 && c.desired.included, "not_ready");
    const instruction = record.instruction, name = c.contract.name;
    const objective = c.desired.objective, limits = c.local.limits;
    if (name === "battery_dispatch") {
      require("intent" in instruction, "instruction_mismatch");
      require(limits !== null && "charge_max_w" in limits, "invalid_limits");
      require(objective?.kind === "battery_policy", "association_mismatch");
      const intent = instruction.intent;
      require(
        c.local.supported_intents?.includes(intent),
        "unsupported_intent",
      );
      require(
        instruction.charge_ceiling_w <= limits.charge_max_w &&
          instruction.discharge_ceiling_w <= limits.discharge_max_w,
        "invalid_limits",
      );
      if (["charge_pv_first", "charge_grid_first"].includes(intent)) {
        require(objective.allow_grid_charge, "grid_charge_not_allowed");
        require(instruction.discharge_ceiling_w === 0, "intent_limits");
      }
      if (intent === "discharge_pv_first") {
        require(objective.allow_battery_export, "export_not_allowed");
        require(instruction.charge_ceiling_w === 0, "intent_limits");
      }
      if (intent === "hold") {
        require(
          instruction.charge_ceiling_w === 0 &&
            instruction.discharge_ceiling_w === 0,
          "intent_limits",
        );
      }
    } else if (name === "pool_service") {
      require("request" in instruction, "instruction_mismatch");
      require(objective?.kind === "pool_temperature", "association_mismatch");
      require(
        (["start_c", "stop_c", "defer_policy"] as const).every((k) =>
          instruction[k] === objective[k]
        ),
        "objective_mismatch",
      );
    } else throw new Error("instruction_mismatch");
  }
}
