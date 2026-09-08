const TABS = [["energy", "Energy"], ["devices", "Devices"], ["schedule", "Schedule"], ["status", "Status"]];
const MAPPINGS_KEY = "device_control_mappings";

class ShsEnergyConfigPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._hass = undefined;
    this._panel = undefined;
    this._data = undefined;
    this._draft = undefined;
    this._savedDraft = undefined;
    this._tab = "schedule";
    this._search = "";
    this._room = "";
    this._category = "";
    this._expanded = new Set();
    this._added = new Set();
    this._fullHorizon = false;
    this._selectedSlot = null;
    this._loading = false;
    this._saving = false;
    this._savingDeviceKey = "";
    this._deviceErrors = {};
    this._error = "";
    this._notice = "";
    this._entryId = new URLSearchParams(window.location.search).get("config_entry");
    this._boundClick = (event) => this._onClick(event);
    this._boundChange = (event) => this._onChange(event);
    this._boundInput = (event) => { if (event.target.dataset.filter) this._onChange(event); };
    this._boundBeforeUnload = (event) => {
      if (!this._dirty) return;
      event.preventDefault();
      event.returnValue = "";
    };
  }

  set hass(value) {
    this._hass = value;
    if (this.isConnected && !this._data && !this._loading) {
      this._load(true);
    }
  }

  set panel(value) {
    this._panel = value;
  }

  connectedCallback() {
    this.shadowRoot.addEventListener("click", this._boundClick);
    this.shadowRoot.addEventListener("change", this._boundChange);
    this.shadowRoot.addEventListener("input", this._boundInput);
    window.addEventListener("beforeunload", this._boundBeforeUnload);
    this._poller = setInterval(() => this._poll(), 30000);
    this._render();
    if (this._hass && !this._data && !this._loading) {
      this._load(true);
    }
  }

  disconnectedCallback() {
    this.shadowRoot.removeEventListener("click", this._boundClick);
    this.shadowRoot.removeEventListener("change", this._boundChange);
    this.shadowRoot.removeEventListener("input", this._boundInput);
    window.removeEventListener("beforeunload", this._boundBeforeUnload);
    clearInterval(this._poller);
  }

  _clone(value) {
    return value === undefined ? undefined : JSON.parse(JSON.stringify(value));
  }

  _escape(value) {
    return String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#039;");
  }

  _label(value) { return this._data?.labels?.[value] || "Not available"; }

  _time(value) {
    return value ? new Date(value).toLocaleString(this._hass?.locale?.language || undefined, { dateStyle: "medium", timeStyle: "short" }) : "Not yet";
  }

  _generalFields() {
    const owned = new Set((this._data.devices || []).flatMap(d => d.system_fields || []).map(f => f.key));
    return this._data.sections.flatMap(section => [...(section.toggle ? [section.toggle] : []), ...section.fields])
      .filter(f => !owned.has(f.key) && !f.key.endsWith("_control_enabled"));
  }

  _patch(fields) {
    return Object.fromEntries(fields.filter(f => JSON.stringify(this._draft[f.key]) !== JSON.stringify(this._savedDraft[f.key]))
      .map(f => [f.key, this._draft[f.key] ?? null]));
  }

  get _dirty() {
    return JSON.stringify(this._draft) !== JSON.stringify(this._savedDraft);
  }

  get _configurationDirty() { return Object.keys(this._patch(this._generalFields())).length > 0; }

  _deviceDirty(deviceKey) {
    const device = this._data?.devices?.find(d => d.key === deviceKey);
    return JSON.stringify(this._draft?.[MAPPINGS_KEY]?.[deviceKey]) !== JSON.stringify(this._savedDraft?.[MAPPINGS_KEY]?.[deviceKey])
      || Object.keys(this._patch(device?.system_fields || [])).length > 0;
  }

  _entityLabel(entityId) {
    const entity = this._data?.entities?.find((item) => item.entity_id === entityId);
    const label = entity && entity.name !== entityId
      ? `${entity.name} · ${entityId}`
      : entityId;
    return entity ? `${label} · ${entity.area_name || "No area"}` : label;
  }

  _clearDeviceError(deviceKey) {
    if (deviceKey && this._deviceErrors[deviceKey]) {
      delete this._deviceErrors[deviceKey];
    }
  }

  async _load(refreshRoles) {
    if (!this._hass || this._loading) return;
    this._loading = true;
    this._error = "";
    this._render();
    const message = {
      type: "shs_energy/config/get",
      refresh_roles: refreshRoles,
    };
    if (this._entryId) message.config_entry = this._entryId;
    try {
      const data = await this._hass.callWS(message);
      this._mergePanel(data);
      this._deviceErrors = {};
      if (!data.requires_entry_selection) {
        this._entryId = data.entry.entry_id;
        if (!this._draft) {
          this._draft = this._clone(data.configuration);
          this._savedDraft = this._clone(data.configuration);
          if (!data.configuration.configuration_reviewed_at) this._tab = "energy";
        }
      }
    } catch (error) {
      this._error = error?.message || String(error);
    } finally {
      this._loading = false;
      this._render();
    }
  }

  async _save() {
    if (
      !this._configurationDirty || this._saving || this._savingDeviceKey ||
      !this._entryId
    ) return;
    this._saving = true;
    this._error = "";
    this._notice = "";
    this._render();
    const configuration = this._patch(this._generalFields());
    try {
      await this._hass.callWS({
        type: "shs_energy/config/save",
        config_entry: this._entryId,
        configuration,
      });
      const savedMappings = this._clone(this._savedDraft?.[MAPPINGS_KEY] || {});
      for (const key of Object.keys(configuration)) this._savedDraft[key] = this._clone(this._draft[key]);
      this._savedDraft[MAPPINGS_KEY] = savedMappings;
      this._discovery = null;
      this._notice =
        "Energy and planning settings saved. Device edits have their own Save button.";
    } catch (error) {
      this._error = error?.message || String(error);
    } finally {
      this._saving = false;
      this._render();
    }
  }

  async _saveDevice(deviceKey) {
    if (
      !this._deviceDirty(deviceKey) ||
      this._savingDeviceKey ||
      this._saving ||
      !this._entryId
    ) return;
    this._savingDeviceKey = deviceKey;
    this._clearDeviceError(deviceKey);
    this._error = "";
    this._notice = "";
    this._render();
    const mapping = this._clone(
      this._draft?.[MAPPINGS_KEY]?.[deviceKey] || null
    );
    try {
      const result = await this._hass.callWS({
        type: "shs_energy/config/save_device",
        config_entry: this._entryId,
        device_key: deviceKey,
        mapping,
        configuration: this._patch(this._data.devices.find(d => d.key === deviceKey)?.system_fields || []),
      });
      const savedMapping = this._clone(
        result.panel?.configuration?.[MAPPINGS_KEY]?.[deviceKey] ?? mapping
      );
      if (result.panel) {
        this._data = result.panel;
        // Room sources are shared. Refresh saved views while retaining other drafts.
        const current = result.panel.configuration?.[MAPPINGS_KEY] || {};
        for (const [key, updated] of Object.entries(current)) {
          if (key === deviceKey) continue;
          const draft = this._draft[MAPPINGS_KEY]?.[key];
          const previous = this._savedDraft[MAPPINGS_KEY]?.[key];
          if (!this._deviceDirty(key)) {
            this._draft[MAPPINGS_KEY][key] = this._clone(updated);
          } else if (draft && draft.temperature_entity_id === previous?.temperature_entity_id) {
            if (updated.temperature_entity_id === undefined) delete draft.temperature_entity_id;
            else draft.temperature_entity_id = updated.temperature_entity_id;
          }
          this._savedDraft[MAPPINGS_KEY][key] = this._clone(updated);
        }
      }
      if (!this._draft[MAPPINGS_KEY]) this._draft[MAPPINGS_KEY] = {};
      if (!this._savedDraft[MAPPINGS_KEY]) this._savedDraft[MAPPINGS_KEY] = {};
      if (savedMapping) {
        this._draft[MAPPINGS_KEY][deviceKey] = this._clone(savedMapping);
        this._savedDraft[MAPPINGS_KEY][deviceKey] = this._clone(savedMapping);
      } else {
        delete this._draft[MAPPINGS_KEY][deviceKey];
        delete this._savedDraft[MAPPINGS_KEY][deviceKey];
      }
      const device = this._data.devices.find((item) => item.key === deviceKey);
      for (const field of device?.system_fields || []) this._savedDraft[field.key] = this._clone(this._draft[field.key]);
      if (device) {
        device.mapping_status = result.mapping_status;
        device.mapping_error = result.mapping_error;
        device.mapping_summary = result.mapping_summary || {};
      }
      this._notice = mapping
        ? `${device?.name || deviceKey} is saved and ${result.mapping_status === "ready" ? "ready" : this._label(result.mapping_status)}.`
        : `${device?.name || deviceKey} setup was removed.`;
    } catch (error) {
      this._deviceErrors[deviceKey] = error?.message || String(error);
    } finally {
      this._savingDeviceKey = "";
      this._render();
    }
  }

  async _discover() {
    if (!this._entryId || this._loading) return;
    this._loading = true;
    this._error = "";
    this._notice = "";
    this._render();
    try {
      const result = await this._hass.callWS({
        type: "shs_energy/config/discover",
        config_entry: this._entryId,
      });
      const sources = this._data.sections.filter(s => s.tab === "energy" && s.id !== "sharing").flatMap(s => s.fields);
      for (const field of sources) {
        if (JSON.stringify(this._draft[field.key]) !== JSON.stringify(this._savedDraft[field.key])) continue;
        if (field.key in result.configuration) this._draft[field.key] = this._clone(result.configuration[field.key]);
        else delete this._draft[field.key];
      }
      this._discovery = result;
      this._notice =
        "Review the proposed source changes below, then Save or Cancel. Your current selections are still in use.";
    } catch (error) {
      this._error = error?.message || String(error);
    } finally {
      this._loading = false;
      this._render();
    }
  }

  _discard() {
    this._draft = this._clone(this._savedDraft);
    this._error = "";
    this._discovery = null;
    this._notice = "Changes discarded.";
    this._render();
  }

  _goBack() {
    if (
      this._dirty &&
      !window.confirm("Discard the unsaved SHS Energy configuration changes?")
    ) {
      return;
    }
    if (window.history.length > 1) {
      window.history.back();
      return;
    }
    window.location.assign("/config/integrations/integration/shs_energy");
  }

  _mapping(deviceKey, create = false) {
    if (!this._draft[MAPPINGS_KEY] && create) {
      this._draft[MAPPINGS_KEY] = {};
    }
    const mappings = this._draft[MAPPINGS_KEY] || {};
    const device = this._data.devices.find((item) => item.key === deviceKey);
    const stale =
      mappings[deviceKey] &&
      mappings[deviceKey].control_type !== device?.control_type;
    if ((!mappings[deviceKey] || stale) && create) {
      mappings[deviceKey] = { control_type: device?.control_type };
    }
    return stale && !create ? undefined : mappings[deviceKey];
  }

  _setField(scope, key, rawValue, field, deviceKey) {
    let target;
    if (scope === "mapping") {
      target = this._mapping(deviceKey, true);
    } else {
      target = this._draft;
    }
    if (field.kind === "toggle") {
      target[key] = Boolean(rawValue);
      return;
    }
    if (field.kind === "number") {
      if (rawValue === "") {
        delete target[key];
        return;
      }
      const number = Number(rawValue);
      target[key] = field.scale ? number / field.scale : number;
      return;
    }
    const value = String(rawValue || "").trim();
    if (!value) delete target[key];
    else target[key] = value;
  }

  _addMulti(scope, key, value, deviceKey) {
    const entityId = String(value || "").trim();
    if (!entityId) return;
    const target = scope === "mapping" ? this._mapping(deviceKey, true) : this._draft;
    const current = Array.isArray(target[key]) ? [...target[key]] : [];
    if (!current.includes(entityId)) current.push(entityId);
    target[key] = current;
    this._clearDeviceError(deviceKey);
    this._notice = "";
    this._render();
  }

  _removeMulti(scope, key, value, deviceKey) {
    const target = scope === "mapping" ? this._mapping(deviceKey, true) : this._draft;
    const current = Array.isArray(target[key]) ? target[key] : [];
    const next = current.filter((item) => item !== value);
    if (next.length) target[key] = next;
    else delete target[key];
    this._clearDeviceError(deviceKey);
    this._notice = "";
    this._render();
  }

  _useSuggestions(deviceKey) {
    const device = this._data.devices.find((item) => item.key === deviceKey);
    if (!device) return;
    const mapping = this._mapping(deviceKey, true);
    for (const [key, value] of Object.entries(device.suggested_mapping || {})) {
      if (mapping[key] === undefined || mapping[key] === "" || mapping[key]?.length === 0) {
        mapping[key] = this._clone(value);
      }
    }
    mapping.control_type = device.control_type;
    this._clearDeviceError(deviceKey);
    this._notice =
      "Suggestions were copied into the draft. Review every value before saving.";
    this._render();
  }

  _clearMapping(deviceKey) {
    if (this._draft[MAPPINGS_KEY]) {
      delete this._draft[MAPPINGS_KEY][deviceKey];
    }
    this._clearDeviceError(deviceKey);
    this._notice =
      "Setup removed from this draft. Save to apply, or Cancel to keep the saved setup.";
    this._render();
  }

  _selectEntry(entryId) {
    this._entryId = entryId;
    const url = new URL(window.location.href);
    url.searchParams.set("config_entry", entryId);
    window.history.replaceState(null, "", url);
    this._data = undefined;
    this._load(true);
  }

  _fieldFromElement(element) {
    const key = element.dataset.fieldKey;
    const scope = element.dataset.scope || "configuration";
    const deviceKey = element.dataset.deviceKey;
    const fields =
      scope === "mapping"
        ? this._data.devices
            .find((device) => device.key === deviceKey)
            ?.fields || []
        : this._data.sections.flatMap((section) => [
            ...(section.toggle ? [section.toggle] : []),
            ...section.fields,
          ]);
    return { key, scope, deviceKey, field: fields.find((item) => item.key === key) };
  }

  _onChange(event) {
    const element = event.target;
    if (!(element instanceof HTMLInputElement || element instanceof HTMLSelectElement)) {
      return;
    }
    if (element.dataset.filter) {
      this["_" + element.dataset.filter] = element.value; this._render(); return;
    }
    if (element.dataset.share) {
      const excluded = new Set(this._draft.excluded_device_readings || []);
      if (element.checked) excluded.delete(element.dataset.share); else excluded.add(element.dataset.share);
      this._draft.excluded_device_readings = [...excluded]; this._render(); return;
    }
    if (element.dataset.permission) {
      this._control(element.dataset.permission, element.checked); return;
    }
    if (!element.dataset.fieldKey) return;
    const { key, scope, deviceKey, field } = this._fieldFromElement(element);
    if (!field) return;
    const value = field.kind === "toggle" ? element.checked : element.value;
    this._setField(scope, key, value, field, deviceKey);
    if (scope === "mapping") this._clearDeviceError(deviceKey);
    if (scope === "mapping" && key === "control_entity_id" && value) {
      const entity = this._data.entities.find((item) => item.entity_id === value);
      const mapping = this._mapping(deviceKey, true);
      if (mapping.minimum_value === undefined && Number.isFinite(Number(entity?.minimum))) {
        mapping.minimum_value = Number(entity.minimum);
      }
      if (mapping.maximum_value === undefined && Number.isFinite(Number(entity?.maximum))) {
        mapping.maximum_value = Number(entity.maximum);
      }
    }
    this._notice = "";
    this._render();
  }

  _onClick(event) {
    const button = event.target.closest("button");
    if (!button) return;
    const action = button.dataset.action;
    if (!action) return;
    if (action === "download") this._download();
    else if (action === "horizon") { this._fullHorizon = !this._fullHorizon; this._render(); }
    else if (action === "slot") { this._selectedSlot = Number(button.dataset.index); this._render(); }
    else if (action === "edit-device") { this._tab = "devices"; this._expanded.add("device:" + button.dataset.deviceKey); this._render(); }
    else if (action === "add-field") { this._added.add(button.dataset.token); this._render(); }
    else if (action === "cancel-device") this._cancelDevice(button.dataset.deviceKey);
    else if (action === "back") this._goBack();
    else if (action === "save") this._save();
    else if (action === "save-device") this._saveDevice(button.dataset.deviceKey);
    else if (action === "discard") this._discard();
    else if (action === "refresh") this._load(true);
    else if (action === "retry") this._load(false);
    else if (action === "discover") this._discover();
    else if (action === "tab") {
      this._tab = button.dataset.tab;
      this._render();
    } else if (action === "select-entry") {
      this._selectEntry(button.dataset.entryId);
    } else if (action === "use-suggestions") {
      this._useSuggestions(button.dataset.deviceKey);
    } else if (action === "clear-mapping") {
      this._clearMapping(button.dataset.deviceKey);
    } else if (action === "add-multi") {
      const editor = button.closest(".multi-editor");
      const input = editor.querySelector("input");
      this._addMulti(
        button.dataset.scope,
        button.dataset.fieldKey,
        input.value,
        button.dataset.deviceKey
      );
    } else if (action === "remove-multi") {
      this._removeMulti(
        button.dataset.scope,
        button.dataset.fieldKey,
        button.dataset.value,
        button.dataset.deviceKey
      );
    }
  }

  _statusBadge(status, label) {
    return `<span class="badge ${this._escape(status)}">${this._escape(label || this._label(status))}</span>`;
  }

  _renderField(field, value, scope = "configuration", deviceKey = "") {
    const key = this._escape(field.key);
    const label = this._escape(field.label);
    const help = field.help ? `<div class="field-help">${this._escape(field.help)}</div>` : "";
    const required = field.required ? '<span class="required">Required</span>' : "";
    const common = `aria-label="${label}" data-field-key="${key}" data-scope="${this._escape(scope)}" data-device-key="${this._escape(deviceKey)}"`;
    let control = "";
    if (field.kind === "toggle") {
      control = `<label class="switch"><input type="checkbox" ${common} ${value ? "checked" : ""}><span></span></label>`;
    } else if (field.kind === "select") {
      control = `<select ${common}>
        <option value="">Select…</option>
        ${(field.choices || [])
          .map(
            (choice) =>
              `<option value="${this._escape(choice.value)}" ${choice.value === value ? "selected" : ""}>${this._escape(choice.label)}</option>`
          )
          .join("")}
      </select>`;
    } else if (field.kind === "entities") {
      const values = Array.isArray(value) ? value : [];
      control = `<div class="multi-editor">
        <div class="chips">
          ${values
            .map(
              (entityId) => `<span class="chip">${this._escape(this._entityLabel(entityId))}<button type="button" aria-label="Remove ${this._escape(entityId)}" data-action="remove-multi" data-scope="${this._escape(scope)}" data-device-key="${this._escape(deviceKey)}" data-field-key="${key}" data-value="${this._escape(entityId)}">×</button></span>`
            )
            .join("")}
        </div>
        <div class="add-row"><input type="text" aria-label="Add ${label}" list="shs-entity-list" placeholder="Search or enter an entity"><button type="button" class="secondary small" data-action="add-multi" data-scope="${this._escape(scope)}" data-device-key="${this._escape(deviceKey)}" data-field-key="${key}">Add</button></div>
      </div>`;
    } else if (field.kind === "number") {
      const displayed = value === undefined || value === null || value === ""
        ? ""
        : Number(value) * (field.scale || 1);
      control = `<div class="with-unit"><input type="number" ${common} value="${this._escape(displayed)}" ${field.step !== undefined ? `step="${field.step}"` : ""} ${field.minimum !== undefined ? `min="${field.minimum}"` : ""} ${field.maximum !== undefined ? `max="${field.maximum}"` : ""}><span>${this._escape(field.unit || "")}</span></div>`;
    } else if (field.kind === "time") {
      control = `<input type="time" ${common} value="${this._escape(value || "")}">`;
    } else if (field.kind === "power") {
      control = `<div class="with-unit"><input type="text" list="shs-power-list" ${common} value="${this._escape(value ?? "")}" placeholder="Power entity or watts"><span>W</span></div>`;
    } else {
      control = `<input type="text" ${common} ${field.kind === "entity" ? 'list="shs-entity-list"' : ""} value="${this._escape(value || "")}" placeholder="${field.kind === "entity" ? "Search or enter an entity" : ""}">`;
    }
    return `<div class="field ${field.kind === "toggle" ? "toggle-field" : ""}">
      <div class="field-label"><label>${label}</label>${required}</div>
      ${control}${help}
    </div>`;
  }

  _mergePanel(data) {
    if (this._draft && data.configuration) {
      for (const key of new Set([...Object.keys(this._savedDraft), ...Object.keys(data.configuration)])) {
        if (key === MAPPINGS_KEY) continue;
        if (JSON.stringify(this._draft[key]) === JSON.stringify(this._savedDraft[key])) {
          if (key in data.configuration) this._draft[key] = this._clone(data.configuration[key]);
          else delete this._draft[key];
        }
        if (key in data.configuration) this._savedDraft[key] = this._clone(data.configuration[key]);
        else delete this._savedDraft[key];
      }
      for (const key of new Set([...Object.keys(this._savedDraft[MAPPINGS_KEY] || {}), ...Object.keys(data.configuration[MAPPINGS_KEY] || {})])) {
        this._draft[MAPPINGS_KEY] ||= {}; this._savedDraft[MAPPINGS_KEY] ||= {};
        if (JSON.stringify(this._draft[MAPPINGS_KEY][key]) === JSON.stringify(this._savedDraft[MAPPINGS_KEY][key])) {
          this._draft[MAPPINGS_KEY][key] = this._clone(data.configuration[MAPPINGS_KEY]?.[key]);
        }
        this._savedDraft[MAPPINGS_KEY][key] = this._clone(data.configuration[MAPPINGS_KEY]?.[key]);
      }
    }
    this._data = data;
  }

  async _poll() {
    if (!this._entryId || this._loading || this._saving || this._savingDeviceKey || document.hidden) return;
    try {
      const data = await this._hass.callWS({ type: "shs_energy/config/get", config_entry: this._entryId, refresh_roles: false });
      this._mergePanel(data); this._refreshError = ""; this._render();
    } catch (error) { this._refreshError = error?.message || String(error); this._render(); }
  }

  async _control(key, enabled) {
    if (this._saving || this._savingDeviceKey) return;
    this._savingDeviceKey = key; this._error = ""; this._render();
    try {
      const data = await this._hass.callWS({ type: "shs_energy/config/control", config_entry: this._entryId, device_key: key, enabled });
      this._mergePanel(data);
      this._notice = enabled ? "Permission saved. SHS may operate this device while the plan is valid." : "Permission removed. Any settings SHS still owns will be restored; progress is shown in Status.";
    } catch (error) { this._error = error?.message || String(error); }
    finally { this._savingDeviceKey = ""; this._render(); }
  }

  _cancelDevice(key) {
    if (this._draft[MAPPINGS_KEY]) this._draft[MAPPINGS_KEY][key] = this._clone(this._savedDraft[MAPPINGS_KEY]?.[key]);
    for (const field of this._data.devices.find(d => d.key === key)?.system_fields || []) {
      if (field.key in this._savedDraft) this._draft[field.key] = this._clone(this._savedDraft[field.key]);
      else delete this._draft[field.key];
    }
    this._clearDeviceError(key); this._render();
  }

  _download() {
    // Deliberate allowlist: no options, entity addresses, names, URLs, raw errors or recorder rows.
    const value = { version: 1, exported_at: new Date().toISOString(),
      plan: { state: this._data.operation.state, issued_at: this._data.operation.issued_at,
        binding_until: this._data.operation.binding_until, valid_until: this._data.operation.valid_until },
      devices: this._data.devices.map((d, index) => ({ device: index + 1, included: d.included,
        control_enabled: d.permission.enabled, state: d.execution_status?.state })),
      delivery: { last_daily_push: this._data.diagnostics.last_daily_push,
        last_plan_push: this._data.readiness.last_plan_push, electrical_history_until: this._data.readiness.actuals_accepted_until,
        thermal_history_until: this._data.diagnostics.thermal_slots_accepted_until },
      upgrade: this._data.diagnostics.migration ? Object.fromEntries(["imported", "removed", "needs_attention"].map(k => [k, this._data.diagnostics.migration[k]?.length || 0])) : null };
    const url = URL.createObjectURL(new Blob([JSON.stringify(value, null, 2)], { type: "application/json" }));
    const a = document.createElement("a"); a.href = url; a.download = "shs-diagnostics.json"; a.click(); URL.revokeObjectURL(url);
  }

  _present(value) { return value !== undefined && value !== null && value !== "" && (!Array.isArray(value) || value.length > 0); }

  _fields(fields, values, scope = "configuration", deviceKey = "") {
    const visible = [], optional = [];
    const linked = {
      pool_start_temperature_entity: ["pool_stop_temperature_entity", "pool_temperature_minimum", "pool_temperature_maximum"],
      offset_entity_id: ["offset_minimum", "offset_maximum"],
      battery_authority_entity: ["battery_authority_confirm_entity", "battery_authority_confirm_state"],
    };
    const dependent = new Set(Object.entries(linked).filter(([key]) => this._present(values[key])).flatMap(([, keys]) => keys));
    for (const field of fields) {
      const token = `${scope}:${deviceKey}:${field.key}`;
      const required = field.required || dependent.has(field.key);
      const populated = this._present(values[field.key]) && (field.kind !== "toggle" || values[field.key] || this._data.configured_keys?.includes(field.key));
      const inheritedLocation = ["pv_forecast_latitude", "pv_forecast_longitude"].includes(field.key) && !this._data.configured_keys?.includes(field.key);
      if (required || (populated && !inheritedLocation) || this._added.has(token)) visible.push(this._renderField({ ...field, required }, values[field.key], scope, deviceKey));
      else if (!["permit_entity_id", "mode_entity_id", "offset_entity_id", "offset_minimum", "offset_maximum", "companion_actuator_entity_ids"].includes(field.key)) {
        optional.push(`<button class="text" data-action="add-field" data-token="${this._escape(token)}">Add ${this._escape(field.label.toLowerCase())}</button>`);
      }
    }
    return `<div class="field-grid">${visible.join("")}</div>${optional.length ? `<details class="compact" data-open-key="optional:${this._escape(scope + deviceKey + fields[0]?.key)}"><summary>Add a setting</summary>${optional.join("")}</details>` : ""}`;
  }

  _renderSection(section) {
    const fields = section.fields;
    const summary = fields.filter(f => this._present(this._draft[f.key])).map(f => {
      const value = this._draft[f.key];
      return Array.isArray(value) ? `${f.label}: ${value.length}` : f.label;
    }).join(" · ") || "No settings configured";
    const id = "section:" + section.id;
    return `<details class="card compact" data-open-key="${id}" ${this._expanded.has(id) ? "open" : ""}>
      <summary>${this._escape(section.title)}<span>${this._escape(summary)}</span></summary>
      ${section.description ? `<p class="description">${this._escape(section.description)}</p>` : ""}
      ${this._fields(fields, this._draft)}
    </details>`;
  }

  _renderEnergy() {
    const sections = this._data.sections.filter(s => s.tab === "energy" && s.id !== "sharing");
    const changes = this._patch(sections.flatMap(s => s.fields));
    const selected = sections.flatMap(s => s.fields).flatMap(field => {
      const value = this._draft[field.key];
      return (Array.isArray(value) ? value : typeof value === "string" && value.includes(".") ? [value] : [])
        .map(id => ({ field, id, entity: this._data.entities.find(e => e.entity_id === id) }));
    });
    return `<div class="page-intro"><h2>Energy shared with SHS</h2><p>Daily energy totals, completed 15-minute readings, device profiles and room temperatures help SHS predict demand. Devices remain counted within the household total.</p></div>
      ${this._discovery ? `<section class="card"><h2>Review proposed source changes</h2>${Object.keys(changes).length ? `<ul>${Object.entries(changes).map(([key, value]) => {
        const field = sections.flatMap(s => s.fields).find(f => f.key === key);
        return `<li><strong>${this._escape(field?.label || "Source")}</strong>: ${this._escape(JSON.stringify(this._savedDraft[key] ?? []))} → ${this._escape(JSON.stringify(value))}</li>`;
      }).join("")}</ul>` : "No source changes proposed."}<p>Save changes to apply, or Cancel to retain your current sources.</p></section>` : ""}
      ${sections.map(s => this._renderSection(s)).join("")}
      <details class="card compact"><summary>Individual device readings</summary><p>Choose which devices send individual readings. Excluded devices remain in the website inventory and inside household totals; their individual load is not scheduled. Shared room observations may still be sent for another heater in the same room.</p>
      ${(this._data.meter_inventory || []).map(d => `<label class="choice-row"><span>${this._escape(d.name)}</span><input type="checkbox" aria-label="Share ${this._escape(d.name)} readings" data-share="${this._escape(d.key)}" ${(this._draft.excluded_device_readings || []).includes(d.key) ? "" : "checked"}></label>`).join("")}</details>
      <p class="muted">Solar location ${this._data.configured_keys?.some(k => ["pv_forecast_latitude", "pv_forecast_longitude"].includes(k)) ? "uses your configured override" : "comes from Home Assistant"}. An override can be added in Solar and electrical measurements.</p>
      <details class="card compact"><summary>Current readings · ${selected.length} selected sources</summary><div class="table-wrap"><table><thead><tr><th>Source</th><th>Used for</th><th>Reading</th><th>Last update</th><th>Selected in</th></tr></thead><tbody>${selected.map(({ field, entity, id }) => `<tr><td>${this._escape(entity?.name || id)}</td><td>${this._escape(field.label)}</td><td>${this._escape(entity ? `${entity.state} ${entity.unit || ""}` : "Unavailable")}</td><td>${this._time(entity?.last_updated)}</td><td>${this._data.configured_keys?.includes(field.key) ? "SHS configuration" : "HA Energy / discovery"}</td></tr>`).join("")}</tbody></table></div></details>`;
  }

  _choices(device) {
    const permission = device.permission;
    const disabled = Boolean(this._saving || this._savingDeviceKey || (!permission.enabled && (this._refreshError || permission.reason || this._deviceDirty(device.key))));
    return `<div class="choices">
      <div class="choice-row"><span>Include in the plan</span><strong>${this._escape(device.choice_label)} · <a href="${this._escape(this._data.website_url)}" target="_blank" rel="noreferrer">Website</a></strong></div>
      <div class="choice-row"><span>Let SHS operate it</span><label class="switch"><input type="checkbox" aria-label="Let SHS operate ${this._escape(device.name)}" data-permission="${this._escape(device.key)}" ${permission.enabled ? "checked" : ""} ${disabled ? "disabled" : ""}><span></span></label>
      <small>${this._escape(permission.reason || (this._deviceDirty(device.key) ? "Save setup changes first" : "Off leaves the device's own controls in charge."))}</small></div>
    </div>`;
  }

  _renderDevice(device) {
    const mapping = this._mapping(device.key) || {};
    const dirty = this._deviceDirty(device.key);
    const id = "device:" + device.key;
    const edit = this._expanded.has(id) || dirty;
    return `<details class="card device-card" data-open-key="${this._escape(id)}" ${edit ? "open" : ""}>
      <summary><div><strong>${this._escape(device.name)}</strong><small>${this._escape(device.room_name || "No room")} · ${this._escape(this._label(device.category))}</small></div>
        ${this._statusBadge(device.included ? device.mapping_status : "base_load")}</summary>
      <div class="device-body"><p>${this._escape(this._label(device.control_type))}</p>
        ${device.mapping_error ? `<p class="inline-warning">${this._escape(device.mapping_error)}</p>` : ""}
        ${this._deviceErrors[device.key] ? `<p role="alert" class="inline-error">${this._escape(this._deviceErrors[device.key])}</p>` : ""}
        ${this._fields(device.fields || [], mapping, "mapping", device.key)}
        ${this._fields(device.system_fields || [], this._draft, "configuration", device.key)}
        ${Object.keys(device.suggested_mapping || {}).some(k => k !== "control_type" && !this._present(mapping[k])) ? `<button class="text" data-action="use-suggestions" data-device-key="${this._escape(device.key)}">Review suggested setup</button>` : ""}
        ${device.fields?.length || device.system_fields?.length ? `<div class="device-save-row"><span>${dirty ? "Unsaved setup changes" : "Saved setup"}</span><button class="text" data-action="cancel-device" data-device-key="${this._escape(device.key)}" ${dirty ? "" : "disabled"}>Cancel</button><button class="primary" data-action="save-device" data-device-key="${this._escape(device.key)}" ${dirty && !this._saving && !this._savingDeviceKey ? "" : "disabled"}>Save setup</button></div>` : `<p>Choose how this device runs on the website to set it up here.</p>`}
        ${this._choices(device)}
        <small class="muted">${this._escape(device.statistic_id || "Equipment settings")}</small>
      </div></details>`;
  }

  _renderDevices() {
    const devices = this._data.devices;
    const filtered = devices.filter(d => (!this._search || `${d.name} ${d.room_name || ""}`.toLowerCase().includes(this._search.toLowerCase())) && (!this._room || d.room_name === this._room) && (!this._category || d.category === this._category));
    const select = (key, placeholder, values, label) => `<select aria-label="${placeholder}" data-filter="${key}"><option value="">${placeholder}</option>${values.map(v => `<option value="${this._escape(v)}" ${this["_" + key] === v ? "selected" : ""}>${this._escape(label(v))}</option>`).join("")}</select>`;
    const equipment = this._data.sections.filter(s => s.toggle && ["battery_enabled", "pool_enabled", "ev_enabled"].includes(s.toggle.key));
    return `<div class="page-intro"><h2>Devices in your home</h2><p>Set up each device once. Selecting an entity never gives SHS permission to operate it.</p></div>
      <div class="filters"><input type="text" aria-label="Search devices" placeholder="Search devices" data-filter="search" value="${this._escape(this._search)}">${select("room", "All rooms", [...new Set(devices.map(d => d.room_name || "No room"))], v => v)}${select("category", "All types", [...new Set(devices.map(d => d.category))], v => this._label(v))}</div>
      ${filtered.map(d => this._renderDevice(d)).join("") || '<p>No matching devices.</p>'}
      <details class="card compact"><summary>Equipment present in this home</summary><p>These choices describe what is installed. Planning participation is chosen on the website.</p>${equipment.map(s => this._renderField(s.toggle, this._draft[s.toggle.key])).join("")}</details>`;
  }

  _commandText(device, slot) {
    if (device.system && !this._data.timeline?.capabilities?.[device.system]) return "No instruction";
    if (device.system === "battery") return slot.battery_charge_w > 0 ? `Charge ${slot.battery_charge_w} W` : slot.battery_discharge_w > 0 ? `Discharge ${slot.battery_discharge_w} W` : "Hold";
    if (device.system === "ev") return slot.ev_target_current_a > 0 ? `Charge ${slot.ev_target_current_a} A` : "Charging off";
    if (device.system === "pool") return slot.pool_w > 0 ? `Heat · ${slot.pool_w} W planned` : "No heating requested";
    const command = slot.commands?.[device.key];
    if (!command) return "No instruction";
    if (command.type === "setpoint") return `Hold ${command.target_c} °C`;
    if (command.type === "switch_schedule") return command.on_seconds ? "On" : "Off";
    if (command.type === "permit_inhibit") return command.permitted ? "Allowed to run" : "Paused";
    if (command.type === "variable_power") return `${command.value} ${command.unit}`;
    return command.reason || "No supported instruction";
  }

  _renderSchedule() {
    const status = this._data.operation;
    const now = Date.parse(status.now);
    const available = (this._refreshError ? [] : this._data.timeline?.slots || []).filter(slot => Date.parse(slot.start) + 900000 > now);
    const slots = this._fullHorizon ? available : available.filter(slot => Date.parse(slot.start) < now + 86400000);
    const devices = this._data.devices;
    const start = Date.parse(slots[0]?.start), end = Date.parse(slots.at(-1)?.start) + 900000;
    const position = (now - start) / (end - start) * 100;
    const selected = slots[this._selectedSlot];
    return `<div class="card"><div class="status-heading"><h2>Your schedule</h2>${this._statusBadge(status.state, status.label)}</div>
      <p>${this._escape(status.reason)}</p><p class="muted">Issued ${this._time(status.issued_at)} · Instructions until ${this._time(status.binding_until)}</p>
      ${slots.length ? `<button class="text" data-action="horizon">${this._fullHorizon ? "Show next 24 hours" : "Show full available plan"}</button><p class="muted">Solid: instructions · Striped: future advice · Red line: now. Select a quarter to inspect its requests.</p><div class="timeline-scroll"><div class="timeline"><div class="timeline-times"><span>${this._time(slots[0].start)}</span><span>${this._time(slots.at(-1).start)}</span></div>
      ${devices.filter(d => d.included).map(d => `<div class="timeline-row"><strong>${this._escape(d.name)}</strong><div class="timeline-track">${slots.map((slot, index) => {
        const text = this._commandText(d, slot);
        const active = !["Off", "Hold", "Charging off", "No heating requested", "No instruction", "Paused"].includes(text) && slot.commands?.[d.key]?.type !== "unavailable";
        return `<button class="slot ${active ? "running" : ""} ${slot.binding ? "" : "advisory"}" data-action="slot" data-index="${index}" aria-label="${this._escape(d.name + ', ' + this._time(slot.start) + ', ' + text + (slot.binding ? ', instruction' : ', advice'))}" title="${this._escape(text)}"></button>`;
      }).join("")}${position >= 0 && position <= 100 ? `<span class="now-line" style="left:${position}%"></span>` : ""}</div></div>`).join("")}</div></div>${selected ? `<div aria-live="polite"><h3>${this._time(selected.start)} · ${selected.binding ? "Instructions" : "Advice only"}</h3><ul>${devices.filter(d => d.included).map(d => `<li>${this._escape(d.name)}: ${this._escape(this._commandText(d, selected))}</li>`).join("")}</ul></div>` : ""}` : `<p>No actionable schedule is available. Details are in Status.</p>`}
      <p>Targets shown here are requests. They do not prove that heat, charging or power was delivered.</p></div>
      ${this._data.sections.filter(s => s.id === "planning").map(s => this._renderSection(s)).join("")}
      ${this._data.sections.filter(s => s.id === "electrical_limits").map(s => this._renderSection({ ...s, title: "House electrical limits", fields: s.fields.filter(f => f.key.startsWith("grid_")) })).join("")}
      <p class="muted">Planning is chosen on the website. Permission to operate is chosen here. Website choices last received ${this._time(this._data.portal.refreshed_at)}.</p>
      ${devices.map(d => `<article class="card schedule-device"><div class="status-heading"><h2>${this._escape(d.name)}</h2><button class="text" data-action="edit-device" data-device-key="${this._escape(d.key)}">Edit setup</button></div>${this._choices(d)}${d.readings?.length ? `<p class="muted">Observed: ${d.readings.map(r => `<span title="${this._escape(r.name + ", updated " + this._time(r.updated_at))}">${this._escape(r.value + " " + r.unit)}</span>`).join(" · ")}</p>` : ""}<small class="muted">${this._escape(this._label(d.execution_status?.state))}${d.execution_status?.reason ? ` · ${this._escape(d.execution_status.reason)}` : ""}${slots[1] && d.included ? ` · Next quarter ${this._time(slots[1].start)}: ${this._escape(this._commandText(d, slots[1]))}` : ""}</small></article>`).join("")}`;
  }

  _attention() { return this._data?.attention || []; }
  _attentionForTab(tab) { return tab === "status" ? this._attention().filter(i => i.severity !== "info") : []; }
  _renderAttention() {
    return this._attention().map(item => `<article class="attention-item ${this._escape(item.severity)}"><strong>${this._escape(item.title)}</strong><p>${this._escape(item.detail)}</p>${item.items?.length ? `<ul>${item.items.map(i => `<li>${this._escape(i)}</li>`).join("")}</ul>` : ""}
      ${item.fix?.url ? `<a href="${this._escape(item.fix.url)}" target="_blank" rel="noreferrer">Open website settings</a>` : item.fix?.tab ? `<button class="text" data-action="tab" data-tab="${this._escape(item.fix.tab)}">Open ${this._escape(TABS.find(([id]) => id === item.fix.tab)?.[1] || "settings")}</button>` : ""}</article>`).join("");
  }

  _renderStatus() {
    const status = this._data.operation, values = this._data.diagnostics, readiness = this._data.readiness;
    const rows = (items) => `<dl>${items.map(([label, value]) => `<div><dt>${this._escape(label)}</dt><dd>${this._escape(value ?? "Not yet")}</dd></div>`).join("")}</dl>`;
    const controllers = this._data.devices.map(d => [d.name, `${this._label(d.execution_status?.state)}${d.execution_status?.reason ? ': ' + d.execution_status.reason : ''}`]);
    return `<section class="card"><div class="status-heading"><h2>Integration status</h2>${this._statusBadge(status.state, status.label)}</div><p>${this._escape(status.reason)}</p>${this._refreshError ? `<p role="alert">Status refresh failed: ${this._escape(this._refreshError)}. These readings may be stale.</p>` : ""}<button class="secondary" data-action="download">Download redacted diagnostics</button></section>
      ${this._attention().length ? `<div class="attention" aria-label="Status details">${this._renderAttention()}</div>` : ""}
      <details class="card diagnostics compact"><summary>Data delivery</summary>${rows([["Latest daily delivery", this._time(values.last_daily_push)], ["Latest daily error", values.last_daily_push_error], ["Latest planning attempt", this._time(readiness.last_plan_attempt)], ["Latest successful exchange", this._time(readiness.last_plan_push)], ["Electrical readings received through", this._time(readiness.actuals_accepted_until)], ["New electrical quarters in latest exchange", readiness.actual_slots_accepted], ["Room readings received through", this._time(values.thermal_slots_accepted_until)], ["New room quarters in latest exchange", values.last_thermal_slots_accepted], ["Website choices received", this._time(this._data.portal.refreshed_at)]])}<p>Zero new quarters does not erase earlier history.</p></details>
      <details class="card diagnostics compact"><summary>Planner</summary>${rows([["Current plan", status.label], ["Plan identifier", status.plan_id], ["Issued", this._time(status.issued_at)], ["Instructions until", this._time(status.binding_until)], ["Valid until", this._time(status.valid_until)], ["Latest exchange error", values.last_optimisation_error], ["Tariff", this._label(values.tariff_status)], ["Subscription", values.subscription_active ? "Active" : "Inactive or unavailable"]])}${readiness.missing_inputs?.length ? `<ul>${readiness.missing_inputs.map(i => `<li>${this._escape(i)}</li>`).join("")}</ul>` : ""}</details>
      <details class="card diagnostics compact"><summary>Device execution</summary>${rows(controllers)}<p>A confirmed target means Home Assistant accepted the setting. Physical delivery requires a measurement.</p></details>
      ${values.migration ? `<details class="card compact"><summary>Configuration upgrade</summary>${[["imported", "Settings carried forward"], ["removed", "Old fields removed"], ["needs_attention", "Setup gaps found at upgrade"]].map(([key, label]) => `<h3>${label}</h3><ul>${(values.migration[key] || []).map(name => `<li>${this._escape(name)}</li>`).join("")}</ul>`).join("")}</details>` : ""}`;
  }

  _renderBody() {
    if (this._tab === "energy") return this._renderEnergy();
    if (this._tab === "devices") return this._renderDevices();
    if (this._tab === "schedule") return this._renderSchedule();
    return this._renderStatus();
  }

  _renderEntrySelection() {
    return `<main class="shell"><header class="topbar"><button type="button" class="icon-button" data-action="back" aria-label="Back">←</button><div><h1>SHS Energy configuration</h1><p>Select the Home Assistant connection to configure.</p></div></header><section class="content"><div class="card entry-list">${(this._data.entries || []).map((entry) => `<button type="button" data-action="select-entry" data-entry-id="${this._escape(entry.entry_id)}"><strong>${this._escape(entry.title)}</strong><span>${this._escape(entry.state)}</span></button>`).join("") || "No SHS Energy entries are installed."}</div></section></main>`;
  }

  _renderDatalist() {
    if (!this._data?.entities) return "";
    const options = (entities) => entities
      .map(
        (entity) =>
          `<option value="${this._escape(entity.entity_id)}">${this._escape(entity.name)}${entity.area_name ? ` · ${this._escape(entity.area_name)}` : " · No area"}${entity.unit ? ` · ${this._escape(entity.unit)}` : ""}</option>`
      )
      .join("");
    const powerEntities = this._data.entities.filter(
      (entity) => entity.domain === "sensor" && ["W", "kW"].includes(entity.unit)
    );
    return `<datalist id="shs-entity-list">${options(this._data.entities)}</datalist><datalist id="shs-power-list">${options(powerEntities)}</datalist>`;
  }

  _render() {
    if (!this.shadowRoot) return;
    const active = this.shadowRoot.activeElement;
    const focusKey = active?.dataset.fieldKey;
    const focusDevice = active?.dataset.deviceKey;
    const focusFilter = active?.dataset.filter;
    const selection = active && "selectionStart" in active ? active.selectionStart : null;
    for (const node of this.shadowRoot.querySelectorAll("details[data-open-key]")) {
      if (node.open) this._expanded.add(node.dataset.openKey); else this._expanded.delete(node.dataset.openKey);
    }
    if (!this._hass || (this._loading && !this._data)) {
      this.shadowRoot.innerHTML = `${this._styles()}<div class="center"><div class="spinner"></div><p>Loading SHS Energy configuration…</p></div>`;
      return;
    }
    if (this._error && !this._data) {
      this.shadowRoot.innerHTML = `${this._styles()}<main class="shell"><header class="topbar"><button type="button" class="icon-button" data-action="back">←</button><div><h1>SHS Energy configuration</h1></div></header><section class="content"><div class="alert error"><strong>Configuration could not be loaded</strong><span>${this._escape(this._error)}</span><button type="button" class="secondary" data-action="retry">Try again</button></div></section></main>`;
      return;
    }
    if (this._data?.requires_entry_selection) {
      this.shadowRoot.innerHTML = `${this._styles()}${this._renderEntrySelection()}`;
      return;
    }
    if (!this._data || !this._draft) return;

    this.shadowRoot.innerHTML = `${this._styles()}
      <main class="shell">
        <header class="topbar">
          <button type="button" class="icon-button" data-action="back" aria-label="Back">←</button>
          <div class="title"><h1>SHS Energy configuration</h1><p>${this._escape(this._data.entry.title)} · ${this._escape(this._data.entry.state)}</p></div>
          <div class="toolbar">
            <button type="button" class="secondary" data-action="refresh" ${this._loading ? "disabled" : ""}>${this._loading ? "Refreshing…" : "Refresh website choices"}</button>
            <button type="button" class="secondary" data-action="discover" ${this._loading ? "disabled" : ""}>Review sources from HA Energy</button>
            <button type="button" class="text" data-action="discard" ${this._dirty ? "" : "disabled"}>Discard</button>
            <button type="button" class="primary" data-action="save" ${this._configurationDirty && !this._saving && !this._savingDeviceKey ? "" : "disabled"}>${this._saving ? "Saving…" : "Save changes"}</button>
          </div>
        </header>
        <nav class="tabs" aria-label="Configuration sections">${TABS.map(([id, label]) => { const count = this._attentionForTab(id).length; return `<button type="button" data-action="tab" data-tab="${id}" class="${this._tab === id ? "active" : ""}${count ? " needs-attention" : ""}">${label}${count ? `<span class="tab-badge" aria-label="${count} item${count === 1 ? "" : "s"} to fix">${count}</span>` : ""}</button>`; }).join("")}</nav>
        <section class="content">
          ${this._error ? `<div class="alert error"><strong>Could not save or refresh</strong><span>${this._escape(this._error)}</span></div>` : ""}
          ${this._notice ? `<div class="alert notice"><span>${this._escape(this._notice)}</span></div>` : ""}
          ${this._data.portal.error ? `<div class="alert warning"><strong>Website choices could not be refreshed</strong><span>${this._escape(this._data.portal.error)} The last received choices are shown.</span></div>` : ""}
          ${this._tab === "status" ? "" : this._attention().length ? `<button class="secondary" data-action="tab" data-tab="status">${this._attention().some(i => i.severity !== "info") ? this._attention().filter(i => i.severity !== "info").length + " items need attention" : "Learning in progress"} · View status</button>` : ""}
          ${this._tab !== "status" && this._data.devices.some(d => d.execution_status?.state === "fault") ? `<div role="alert" class="alert error"><span>Device execution needs attention: ${this._data.devices.filter(d => d.execution_status?.state === "fault").map(d => this._escape(d.name)).join(", ")}</span><button class="secondary" data-action="tab" data-tab="status">View status</button></div>` : ""}
          ${this._renderBody()}
        </section>
        <footer aria-live="polite"><span>${this._dirty ? "Unsaved changes" : "All changes saved"}</span><span>Planning is chosen on the website. Permission to operate is chosen here.</span></footer>
        ${this._renderDatalist()}
      </main>`;
    const fields = [...this.shadowRoot.querySelectorAll("input,select")];
    const target = fields.find(el => focusFilter ? el.dataset.filter === focusFilter : focusKey && el.dataset.fieldKey === focusKey && el.dataset.deviceKey === focusDevice);
    if (target) { target.focus(); if (selection !== null && target.type === "text") target.setSelectionRange(selection, selection); }
  }

  _styles() {
    return `<style>
      :host { display:block; min-height:100%; color:var(--primary-text-color); background:var(--primary-background-color); font-family:var(--paper-font-body1_-_font-family, system-ui, sans-serif); }
      * { box-sizing:border-box; }
      button, input, select { font:inherit; }
      button { cursor:pointer; }
      button:disabled { cursor:default; opacity:.48; }
      .shell { min-height:100vh; }
      button:focus-visible, summary:focus-visible, a:focus-visible { outline:3px solid var(--primary-color); outline-offset:3px; }
      .switch input:focus-visible + span { outline:3px solid var(--primary-color); outline-offset:3px; }
      .switch input:disabled + span { opacity:.45; }
      .schedule-device { padding:16px 20px; margin-bottom:12px; }
      .schedule-device h2 { font-size:17px; }
      .schedule-device .choices { margin:6px 0; padding:2px 0; }
      .choice-row { display:grid; grid-template-columns:1fr auto; gap:8px; padding:10px 0; }
      .choice-row small { grid-column:1/-1; color:var(--secondary-text-color); }
      .choices { border-top:1px solid var(--divider-color); border-bottom:1px solid var(--divider-color); margin:14px 0; padding:8px 0; }
      .filters { display:flex; gap:12px; margin:18px 0; flex-wrap:wrap; }
      .filters > * { flex:1; min-width:150px; }
      .compact summary { cursor:pointer; font-weight:600; padding:8px 0; }
      .compact summary span { display:block; font-size:13px; font-weight:400; color:var(--secondary-text-color); margin-top:6px; }
      .source-list { display:grid; gap:8px; margin:15px 0; }
      .source-list small { color:var(--secondary-text-color); }
      .timeline-scroll { overflow-x:auto; }
      .timeline { min-width:720px; }
      .timeline-row { display:grid; grid-template-columns:160px 1fr; gap:12px; padding:10px 0; align-items:center; }
      .timeline-track { display:flex; height:30px; position:relative; background:var(--secondary-background-color); }
      .slot { flex:1; min-width:2px; padding:0; border:0; border-right:1px solid var(--card-background-color); background:transparent; }
      .slot.running { background:var(--primary-color); }
      .slot.advisory { opacity:.35; background-image:repeating-linear-gradient(45deg,transparent,transparent 2px,#fff5 2px,#fff5 4px); }
      .now-line { position:absolute; height:100%; width:2px; background:var(--error-color); pointer-events:none; }
      .timeline-times { display:flex; justify-content:space-between; margin-left:172px; color:var(--secondary-text-color); font-size:12px; }
      .status-heading { display:flex; justify-content:space-between; gap:18px; align-items:center; }
      .info { border-color:var(--primary-color) !important; }
      .table-wrap { overflow-x:auto; }
      .muted { color:var(--secondary-text-color); }
      a { color:var(--primary-color); }

      .topbar { min-height:84px; padding:16px 24px; display:flex; align-items:center; gap:16px; position:sticky; top:0; z-index:5; background:var(--app-header-background-color, var(--card-background-color)); color:var(--app-header-text-color, var(--primary-text-color)); border-bottom:1px solid var(--divider-color); }
      .icon-button { width:44px; height:44px; border:0; border-radius:50%; background:transparent; color:inherit; font-size:28px; }
      .icon-button:hover { background:rgba(127,127,127,.14); }
      .title { min-width:220px; flex:1; }
      h1 { font-size:24px; line-height:1.2; margin:0 0 4px; }
      .title p, .topbar p { margin:0; color:var(--secondary-text-color); font-size:14px; }
      .toolbar { display:flex; gap:8px; align-items:center; flex-wrap:wrap; justify-content:flex-end; }
      button.primary, button.secondary, button.text { min-height:40px; padding:0 16px; border-radius:10px; font-weight:600; }
      button.primary { border:1px solid var(--primary-color); color:var(--text-primary-color, white); background:var(--primary-color); }
      button.secondary { border:1px solid var(--divider-color); color:var(--primary-text-color); background:var(--card-background-color); }
      button.text { border:1px solid transparent; color:var(--primary-color); background:transparent; }
      button.danger { color:var(--error-color); }
      button.small { min-height:36px; padding:0 12px; }
      .tabs { display:flex; gap:4px; padding:12px max(24px, calc((100vw - 1280px)/2)); overflow-x:auto; border-bottom:1px solid var(--divider-color); background:var(--primary-background-color); position:sticky; top:84px; z-index:4; }
      .tabs button { white-space:nowrap; border:0; border-radius:10px; padding:11px 16px; color:var(--secondary-text-color); background:transparent; font-weight:600; }
      .tabs button.active { color:var(--primary-text-color); background:var(--secondary-background-color); }
      .tabs button.needs-attention { color:var(--warning-color, #ff9800); }
      .tab-badge { display:inline-flex; align-items:center; justify-content:center; min-width:18px; height:18px; margin-left:7px; padding:0 5px; border-radius:9px; background:var(--warning-color, #ff9800); color:#1c1c1c; font-size:11px; font-weight:700; }
      .attention { display:grid; gap:12px; margin-bottom:18px; }
      .attention-item { margin:0; border:1px solid var(--warning-color, #ff9800); border-left-width:4px; border-radius:12px; padding:14px 16px; background:var(--card-background-color); }
      .attention-item.info { border-color:var(--divider-color, #ddd); }
      .attention-item.warning > strong { color:var(--warning-color, #ff9800); }
      .attention-item.error { border-color:var(--error-color, #db4437); }
      .attention-top { display:flex; align-items:center; justify-content:space-between; gap:12px; flex-wrap:wrap; }
      .attention-item p { margin:6px 0 0; color:var(--secondary-text-color); font-size:13px; }
      .attention-item ul { margin:8px 0 0; padding-left:18px; font-size:13px; }
      .attention-item li { margin:3px 0; }
      .attention-item .fix { flex:none; border:0; border-radius:9px; padding:7px 13px; background:var(--primary-color, #03a9f4); color:var(--text-primary-color, #fff); font-weight:600; font-size:13px; text-decoration:none; cursor:pointer; }
      .attention-item .fix.muted { background:transparent; color:var(--secondary-text-color); font-weight:500; padding:0; }
      .content { width:min(1280px, 100%); margin:0 auto; padding:24px 24px 96px; }
      .card { background:var(--card-background-color); border:1px solid var(--divider-color); border-radius:16px; padding:24px; margin-bottom:20px; box-shadow:var(--ha-card-box-shadow, none); }
      .card h2 { margin:0 0 8px; font-size:20px; }
      .description, .page-intro p, .explanation p { color:var(--secondary-text-color); margin:0 0 22px; line-height:1.55; }
      .page-intro { margin:4px 0 20px; }
      .page-intro h2 { margin:0 0 6px; }
      .summary-card.ready { border-top-color:var(--success-color, #43a047); }
      .summary-card.warning { border-top-color:var(--warning-color, #ff9800); }
      .summary-card.error { border-top-color:var(--error-color, #db4437); }
      .badge { display:inline-flex; align-items:center; padding:4px 9px; border-radius:999px; white-space:nowrap; font-size:12px; font-weight:700; color:var(--secondary-text-color); background:var(--secondary-background-color); }
      .badge.ready, .badge.synchronised, .badge.observations_published { color:var(--success-color, #2e7d32); background:color-mix(in srgb, var(--success-color, #43a047) 14%, transparent); }
      .badge.warning, .badge.not_configured, .badge.device_mappings_required, .badge.outdoor_sources_required, .badge.waiting_for_history { color:var(--warning-color, #ef6c00); background:color-mix(in srgb, var(--warning-color, #ff9800) 14%, transparent); }
      .badge.error, .badge.invalid { color:var(--error-color, #c62828); background:color-mix(in srgb, var(--error-color, #db4437) 12%, transparent); }
      .field-grid { display:grid; grid-template-columns:repeat(2, minmax(0,1fr)); gap:18px 24px; }
      .field { min-width:0; }
      .field-label { display:flex; justify-content:space-between; gap:8px; align-items:center; min-height:22px; margin-bottom:7px; }
      .field-label label { font-weight:600; }
      .required { color:var(--error-color); font-size:11px; text-transform:uppercase; letter-spacing:.04em; }
      .field-help { margin-top:7px; color:var(--secondary-text-color); font-size:12px; line-height:1.4; }
      input[type=text], input[type=number], input[type=time], select { width:100%; min-height:48px; padding:10px 12px; border:1px solid var(--divider-color); border-radius:10px; color:var(--primary-text-color); background:var(--secondary-background-color); outline:none; }
      input:focus, select:focus { border-color:var(--primary-color); box-shadow:0 0 0 1px var(--primary-color); }
      .with-unit { display:flex; align-items:center; border:1px solid var(--divider-color); border-radius:10px; background:var(--secondary-background-color); }
      .with-unit:focus-within { border-color:var(--primary-color); box-shadow:0 0 0 1px var(--primary-color); }
      .with-unit input { border:0; box-shadow:none; background:transparent; }
      .with-unit span { padding:0 12px; color:var(--secondary-text-color); white-space:nowrap; }
      .toggle-field { display:grid; grid-template-columns:1fr auto; align-items:center; gap:12px; padding:8px 0; }
      .toggle-field .field-label { margin:0; }
      .toggle-field .field-help { grid-column:1/-1; margin:0; }
      .section-head { display:flex; align-items:center; justify-content:space-between; gap:16px; }
      .section-head h2 { margin:0; }
      .section-switch { flex:0 0 auto; }
      /* A section that is off keeps its heading legible and stops competing
         for attention with the ones that describe equipment this home has. */
      .form-card.section-off h2 { opacity:.7; }
      .form-card.section-off .description { margin-bottom:0; }
      .switch input { position:absolute; opacity:0; }
      .switch span { display:block; width:48px; height:28px; border-radius:20px; background:var(--disabled-color); position:relative; transition:.2s; }
      .switch span:after { content:""; position:absolute; width:22px; height:22px; left:3px; top:3px; border-radius:50%; background:white; box-shadow:0 1px 3px #0005; transition:.2s; }
      .switch input:checked + span { background:var(--primary-color); }
      .switch input:checked + span:after { transform:translateX(20px); }
      .chips { display:flex; flex-wrap:wrap; gap:6px; min-height:8px; margin-bottom:7px; }
      .chip { display:inline-flex; align-items:center; gap:6px; max-width:100%; padding:6px 7px 6px 10px; border-radius:999px; background:var(--secondary-background-color); font-size:12px; overflow-wrap:anywhere; }
      .chip button { border:0; width:20px; height:20px; border-radius:50%; padding:0; color:var(--secondary-text-color); background:transparent; font-size:17px; line-height:1; }
      .add-row { display:flex; gap:8px; }
      .add-row input { flex:1; }
      .device-card { padding:0; overflow:hidden; }
      .device-card summary { list-style:none; padding:18px 22px; display:flex; justify-content:space-between; align-items:center; gap:16px; cursor:pointer; }
      .device-card summary::-webkit-details-marker { display:none; }
      .device-card summary > div:first-child { display:flex; flex-direction:column; min-width:0; }
      .device-card summary small { color:var(--secondary-text-color); overflow:hidden; text-overflow:ellipsis; }
      .device-body { border-top:1px solid var(--divider-color); padding:22px; }
      .device-save-row { display:flex; justify-content:flex-end; align-items:center; gap:16px; margin-top:24px; padding-top:18px; border-top:1px solid var(--divider-color); }
      .device-save-row span { color:var(--secondary-text-color); font-size:13px; }
      .inline-warning { padding:11px 13px; margin:10px 0; border-radius:9px; color:var(--warning-color); background:color-mix(in srgb, var(--warning-color) 10%, transparent); }
      .inline-error { display:flex; gap:10px; align-items:center; padding:11px 13px; margin:10px 0; border-radius:9px; color:var(--error-color); background:color-mix(in srgb, var(--error-color) 10%, transparent); }
      .inline-error span { flex:1; }
      table { width:100%; border-collapse:collapse; margin-top:18px; }
      th, td { padding:12px 10px; text-align:left; border-top:1px solid var(--divider-color); }
      th { color:var(--secondary-text-color); font-size:12px; text-transform:uppercase; letter-spacing:.04em; }
      .diagnostics dl { margin:20px 0; }
      .diagnostics dl div { display:grid; grid-template-columns:minmax(180px, 1fr) 2fr; gap:18px; padding:12px 0; border-top:1px solid var(--divider-color); }
      .diagnostics dt { color:var(--secondary-text-color); }
      .diagnostics dd { margin:0; overflow-wrap:anywhere; }
      .alert { width:min(1280px,100%); display:flex; gap:12px; align-items:center; padding:14px 16px; margin:0 0 18px; border:1px solid; border-radius:12px; line-height:1.4; }
      .alert span { flex:1; }
      .alert.error { color:var(--error-color); border-color:color-mix(in srgb, var(--error-color) 45%, transparent); background:color-mix(in srgb, var(--error-color) 10%, transparent); }
      .alert.warning { color:var(--warning-color); border-color:color-mix(in srgb, var(--warning-color) 45%, transparent); background:color-mix(in srgb, var(--warning-color) 10%, transparent); }
      .alert.notice { color:var(--info-color, var(--primary-color)); border-color:color-mix(in srgb, var(--primary-color) 40%, transparent); background:color-mix(in srgb, var(--primary-color) 9%, transparent); }
      .empty { text-align:center; padding:48px 24px; }
      .entry-list { display:flex; flex-direction:column; gap:10px; }
      .entry-list button { display:flex; justify-content:space-between; padding:16px; border:1px solid var(--divider-color); border-radius:10px; background:var(--secondary-background-color); color:var(--primary-text-color); }
      footer { position:fixed; bottom:0; left:0; right:0; min-height:48px; padding:10px 24px; display:flex; justify-content:space-between; gap:16px; align-items:center; border-top:1px solid var(--divider-color); background:var(--card-background-color); color:var(--secondary-text-color); font-size:13px; z-index:5; }
      .center { min-height:100vh; display:grid; place-content:center; justify-items:center; color:var(--secondary-text-color); }
      .spinner { width:36px; height:36px; border:3px solid var(--divider-color); border-top-color:var(--primary-color); border-radius:50%; animation:spin .8s linear infinite; }
      @keyframes spin { to { transform:rotate(360deg); } }
      @media (max-width:1000px) { .topbar { flex-wrap:wrap; } .toolbar { width:100%; } .tabs { top:132px; } }
      @media (max-width:700px) { .topbar { padding:12px; position:relative; } .tabs { top:0; position:sticky; padding:8px 12px; } .content { padding:16px 12px 88px; } .toolbar { display:grid; grid-template-columns:1fr 1fr; } .field-grid { grid-template-columns:1fr; } .card { padding:18px; border-radius:13px; } .device-card { padding:0; } .diagnostics dl div { grid-template-columns:1fr; gap:5px; } footer span:last-child { display:none; } }
    </style>`;
  }
}

if (!customElements.get("shs-energy-config-panel-v3")) {
  customElements.define("shs-energy-config-panel-v3", ShsEnergyConfigPanel);
}
