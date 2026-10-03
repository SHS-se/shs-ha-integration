export type StorageCensus = {diagnostic_retention_days:number; databases:{name:string;error?:string;file_bytes?:number;wal_bytes?:number;shm_bytes?:number;schema_version?:number;journal_mode?:string;free_pages?:number;census_ms?:number;tables?:{name:string;rows:number;pages:number;bytes:number}[];receipts?:{received_through:number;processed_through:number;pending:number};retained_ranges?:Record<string,{first:number|string|null;last:number|string|null}>}[];archives:{name:string;bytes:number}[];operations?:Record<string,unknown>};
export type Temperature = {key:string; name:string; target_c:number|null};
export type Slot = {
  start: string; duration_hours: number; binding: boolean;
  pv_w: number|null; base_w: number|null; load_w: number|null;
  shadow_import_sek_per_kwh: number|null; shadow_export_sek_per_kwh: number|null;
  grid_import_w: number|null; grid_export_w: number|null;
  battery_charge_w: number|null; battery_discharge_w: number|null;
  battery_soc: number|null; ev_soc: number|null;
  temperatures_c: Record<string,number|null>;
  import_cost_sek: number|null; export_revenue_sek: number|null;
  device_loads_w: Record<string,number>|null;
  device_commands: Record<string,unknown>|null; battery_command: unknown;
  ev_w: number|null; pool_w: number|null; boiler_expected_w: number|null;
};
export type Plan = {plan_id:string; issued_at:string; valid_until:string; binding_until:string; timezone:string; currency:string; temperatures:Temperature[]; household:Slot[]; execution:Slot[]|null; devices:{key:string; name:string; category:string}[]};
export type Measurement = {value:number|null; unit:string; entity_id:string|null; observed_at:string|null};
export type Attention = {title?:string; detail?:string; items?:string[]; next_step?:string; fix?:{fields?:{key:string; message?:string; url:string}[]}};
export type Entry = {id:string; title:string; state:string; operation?:{state:string; label?:string; reason:string}; schedule?:Plan|null; controllers?:Record<string,{state:string;reason?:string;[key:string]:unknown}>; measurements?:Record<string,Measurement>; attention?:Attention[]};
export type Resources = {cpu_percent:number; memory_usage:number; memory_limit:number; blk_read:number; blk_write:number; network_rx:number; network_tx:number};
export type History = {sampled_at:string; resources:Resources|null; telemetry?:Record<string,{measured_w:number|null; planned_w:number|null; plan_id:string|null}>};
export type State = {
  app_version:string; required_companion:string; protocol:number;
  connection:{state:string; message:string; loaded_version?:string};
  recovery:{processed_receipt:number|null}|null;
  snapshot:{sampled_at:string; integration_version:string; entries:Entry[]}|null;
  system:{storage?:StorageCensus;sampled_at:string|null; resources:Resources|null; error:string|null; filesystem_free_bytes?:number; database:{file_bytes:number; schema_version:number; page_size:number; free_pages:number; journal_mode:string; tables:{name:string;bytes:number;pages:number;rows:number}[]; history:History[]; query_plan:string[]; operations:{name:string;count:number;errors:number;last_ms:number;max_recent_ms:number;queue_ms:number}[]}|null};
  companion:{state:string;message:string}; app_slug:string|null; sidebar_enabled:boolean|null; control_owner:string;
};
