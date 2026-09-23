const TABS = [["energy", "Energy"], ["devices", "Devices"], ["schedule", "Schedule"], ["status", "Status"]];
const DEVICE_MODES = [["control_verification", "Verification"], ["controlling", "Controlling"]];
const SCHEDULE_ACTIONS = {
  heating: { label: "Heating", colour: "#f5a38a" },
  cooling: { label: "Cooling", colour: "#a8cfe8" },
  charging: { label: "Charging", colour: "#9dc4ad" },
  discharging: { label: "Discharging", colour: "#f6c573" },
  general: { label: "On / automatic", colour: "#4a90c5" },
  idle: { label: "Idle / no request", colour: "var(--secondary-background-color)" },
};
const MAPPINGS_KEY = "device_control_mappings";
const FRONTEND_VERSION = new URL(import.meta.url).searchParams.get("v");
const PANEL_ELEMENT = `shs-energy-config-panel-${FRONTEND_VERSION.replaceAll(".", "-")}`;

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
    this._scheduleSearch = this._scheduleRoom = this._scheduleCategory = this._scheduleMode = "";
    this._showExcluded = false;
    this._expanded = new Set();
    this._added = new Set();
    this._fullHorizon = false;
    this._selectedSlot = null;
    this._loading = false;
    this._refreshing = false;
    this._saving = false;
    this._savingDeviceKey = "";
    this._deviceErrors = {};
    this._error = "";
    this._notice = "";
    this._entryId = new URLSearchParams(window.location.search).get("config_entry");
    this._boundClick = (event) => this._onClick(event);
    this._boundChange = (event) => this._onChange(event);
    this._boundInput = (event) => this._onChange(event);
    this._boundBeforeUnload = (event) => {
      if (!this._dirty) return;
      event.preventDefault();
      event.returnValue = "";
    };
  }

  set hass(value) {
    this._hass = value;
    // Home Assistant sets hass on every state change. A failed load waits for
    // Try again instead of retrying behind the spinner and hiding its error.
    if (this.isConnected && !this._data && !this._loading && !this._error) {
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
    this._poller = setInterval(() => {
      if (this._refreshing || Date.now() - (this._lastPollAt || 0) >= 30000) this._poll();
    }, 1000);
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

  _systemFields(device, section) {
    const planning = new Set((device?.planning_fields || []).map(f => f.key));
    return (device?.system_fields || []).filter(f => !section || (section === "planning" ? planning.has(f.key) : !planning.has(f.key)));
  }

  _deviceDirty(deviceKey, section) {
    const device = this._data?.devices?.find(d => d.key === deviceKey);
    return (section !== "planning" && JSON.stringify(this._draft?.[MAPPINGS_KEY]?.[deviceKey]) !== JSON.stringify(this._savedDraft?.[MAPPINGS_KEY]?.[deviceKey]))
      || Object.keys(this._patch(this._systemFields(device, section))).length > 0;
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

  _errorMessage(error) {
    if (typeof error === "string" && error.trim()) return error;
    for (const value of [error?.message, error?.error?.message, error?.error, error?.code]) {
      if (typeof value === "string" && value.trim()) return value;
    }
    return "Home Assistant could not complete the request. Please retry; if it continues, check the Home Assistant logs.";
  }

  async _load(refreshRoles) {
    if (!this._hass || this._loading) return;
    if (refreshRoles && !this._data) {
      // Paint the local configuration before waiting for website requests or
      // the planning lock. Reuse polling's editor and stale-response guards.
      await this._load(false);
      if (this._data && !this._data.requires_entry_selection) await this._poll(true);
      return;
    }
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
      if (this._acceptRefresh(data)) return;
      this._mergePanel(data);
      this._deviceErrors = {};
      if (!data.requires_entry_selection) {
        this._entryId = data.entry.entry_id;
      }
    } catch (error) {
      if (error?.code === "refresh_in_progress") this._refreshing = true;
      else { this._refreshing = false; this._error = this._errorMessage(error); }
    } finally {
      this._loading = false;
      this._renderBackground();
    }
  }

  _acceptRefresh(data) {
    this._refreshing = Boolean(data?.refreshing);
    if (data?.entry_id) this._entryId = data.entry_id;
    if (this._refreshing) this._refreshError = "";
    return this._refreshing;
  }

  _refreshBanner() {
    return this._refreshing ? `<div class="alert notice refresh-progress" role="status" aria-live="polite"><span class="spinner" aria-hidden="true"></span><span>Refresh in progress. Showing the previous plan and status. Saving is available when the refresh finishes.</span></div>` : "";
  }

  _recordSavedFields(configuration) {
    for (const [key, value] of Object.entries(configuration)) {
      this._savedDraft[key] = this._clone(value);
    }
  }

  _editing() {
    const active = this.shadowRoot?.activeElement;
    return active?.tagName === "INPUT" || active?.tagName === "SELECT";
  }

  _renderBackground() {
    // Async completions must not replace an editor mid-keystroke (including
    // incomplete numbers and entity searches that have not been added yet).
    if (this._editing()) this._updateSaveState();
    else this._render();
  }

  _updateSaveState() {
    if (!this.shadowRoot) return;
    const progress = this.shadowRoot.querySelector("[data-refresh-progress]");
    if (progress) progress.innerHTML = this._refreshBanner();
    for (const button of this.shadowRoot.querySelectorAll("button[data-action]")) {
      const { action, deviceKey, section } = button.dataset;
      if (action === "save") button.disabled = !this._configurationDirty || this._refreshing || this._saving || Boolean(this._savingDeviceKey);
      else if (action === "discard") button.disabled = !this._dirty;
      else if (action === "refresh") button.disabled = this._loading || this._refreshing;
      else if (action === "save-device" || action === "cancel-device") {
        button.disabled = !this._deviceDirty(deviceKey, section) || (action === "save-device" && (this._refreshing || this._saving || Boolean(this._savingDeviceKey)));
        button.closest(".device-save-row").querySelector("span").textContent = this._deviceDirty(deviceKey, section) ? "Unsaved changes" : "Saved";
      }
    }
    for (const input of this.shadowRoot.querySelectorAll("[data-permission], [data-share]")) {
      input.disabled = Boolean(this._refreshing || this._saving || this._savingDeviceKey);
    }
    const status = this.shadowRoot.querySelector("footer span");
    if (status) status.textContent = this._dirty ? "Unsaved changes" : "All changes saved";
  }

  async _save() {
    if (
      !this._configurationDirty || this._refreshing || this._saving || this._savingDeviceKey ||
      !this._entryId
    ) return;
    this._pollRevision = (this._pollRevision || 0) + 1;
    this._saving = true;
    this._error = "";
    this._notice = "";
    this._render();
    const configuration = this._clone(this._patch(this._generalFields()));
    try {
      const result = await this._hass.callWS({
        type: "shs_energy/config/save",
        config_entry: this._entryId,
        configuration,
      });
      this._acceptRefresh(result);
      const savedMappings = this._clone(this._savedDraft?.[MAPPINGS_KEY] || {});
      this._recordSavedFields(configuration);
      this._savedDraft[MAPPINGS_KEY] = savedMappings;
      this._discovery = null;
      this._notice =
        "Energy and planning settings saved. Device edits have their own Save button.";
    } catch (error) {
      if (error?.code === "refresh_in_progress") this._refreshing = true;
      else { this._refreshing = false; this._error = this._errorMessage(error); }
    } finally {
      this._saving = false;
      this._renderBackground();
    }
  }

  async _saveDevice(deviceKey, section) {
    if (
      !this._deviceDirty(deviceKey, section) ||
      this._savingDeviceKey ||
      this._refreshing || this._saving ||
      !this._entryId
    ) return;
    this._pollRevision = (this._pollRevision || 0) + 1;
    this._savingDeviceKey = deviceKey;
    this._clearDeviceError(deviceKey);
    this._error = "";
    this._notice = "";
    this._render();
    const device = this._data.devices.find(d => d.key === deviceKey);
    const fields = this._systemFields(device, section);
    const submittedMapping = this._clone(this._draft[MAPPINGS_KEY]?.[deviceKey]);
    const configuration = this._clone(this._patch(fields));
    const mapping = this._clone(
      (section === "planning" ? this._savedDraft : this._draft)?.[MAPPINGS_KEY]?.[deviceKey] || null
    );
    if (section !== "planning" && mapping && device.system === "pool" && device.control_type === "setpoint") {
      mapping.temperature_entity_id = this._draft.pool_water_temperature_entity;
    }
    try {
      const result = await this._hass.callWS({
        type: "shs_energy/config/save_device",
        config_entry: this._entryId,
        device_key: deviceKey,
        mapping,
        configuration,
      });
      this._acceptRefresh(result);
      const savedMapping = this._clone(
        result.configuration?.[MAPPINGS_KEY]?.[deviceKey] ?? mapping
      );
      if (result.configuration) {
        // Room sources are shared. Refresh saved views while retaining other drafts.
        const current = result.configuration[MAPPINGS_KEY] || {};
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
        if (section !== "planning" && JSON.stringify(this._draft[MAPPINGS_KEY][deviceKey]) === JSON.stringify(submittedMapping)) this._draft[MAPPINGS_KEY][deviceKey] = this._clone(savedMapping);
        this._savedDraft[MAPPINGS_KEY][deviceKey] = this._clone(savedMapping);
      } else {
        if (section !== "planning" && JSON.stringify(this._draft[MAPPINGS_KEY][deviceKey]) === JSON.stringify(submittedMapping)) delete this._draft[MAPPINGS_KEY][deviceKey];
        delete this._savedDraft[MAPPINGS_KEY][deviceKey];
      }
      const device = this._data.devices.find((item) => item.key === deviceKey);
      this._recordSavedFields(configuration);
      if (device) {
        device.mapping_status = result.mapping_status;
        device.mapping_error = result.mapping_error;
        device.mapping_summary = result.mapping_summary || {};
      }
      this._notice = section === "planning"
        ? `${device?.name || deviceKey} planning settings saved.`
        : mapping
        ? `${device?.name || deviceKey} is saved and ${result.mapping_status === "ready" ? "ready" : this._label(result.mapping_status)}.`
        : `${device?.name || deviceKey} setup was removed.`;
    } catch (error) {
      if (error?.code === "refresh_in_progress") this._refreshing = true;
      else this._deviceErrors[deviceKey] = this._errorMessage(error);
    } finally {
      this._savingDeviceKey = "";
      this._renderBackground();
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
      if (error?.code === "refresh_in_progress") this._refreshing = true;
      else { this._refreshing = false; this._error = this._errorMessage(error); }
    } finally {
      this._loading = false;
      this._renderBackground();
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
    if (field.kind === "number" || (field.kind === "quantity" && String(rawValue).trim() !== "" && Number.isFinite(Number(rawValue)))) {
      if (rawValue === "") {
        delete target[key];
        return;
      }
      const number = Number(rawValue);
      target[key] = field.scale ? number / field.scale : number;
      return;
    }
    const value = String(rawValue || "").trim();
    if (field.kind === "entities" && field.max_items === 1) {
      if (value) target[key] = [value];
      else delete target[key];
      return;
    }
    if (!value) delete target[key];
    else target[key] = value;
  }

  _removeField(scope, key, deviceKey = "") {
    const target = scope === "mapping" ? this._mapping(deviceKey, true) : this._draft;
    target[key] = null;
    if (scope === "configuration" && key === "battery_mode_entity") {
      for (const mode of ["charge", "discharge", "idle", "baseline"]) {
        target[`battery_mode_${mode}`] = null;
        this._added.delete(`${scope}:${deviceKey}:battery_mode_${mode}`);
      }
    }
    this._added.delete(`${scope}:${deviceKey}:${key}`);
    this._clearDeviceError(deviceKey);
    this._render();
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
      this["_" + element.dataset.filter] = element.type === "checkbox" ? element.checked : element.value; this._render(); return;
    }
    if (event.type === "input" && (element.type === "checkbox" || element instanceof HTMLSelectElement)) return;
    if (element.dataset.share) {
      const excluded = new Set(this._draft.excluded_device_readings || []);
      if (element.checked) excluded.delete(element.dataset.share); else excluded.add(element.dataset.share);
      this._draft.excluded_device_readings = [...excluded];
      this._saveInclusion(); return;
    }
    if (element.dataset.permission) {
      this._control(element.dataset.permission, element.value); return;
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
    if (event.type === "input") this._updateSaveState();
    else this._render();
  }

  _openDevice(key) {
    const device = this._data.devices.find(d => d.key === key);
    if (!device) return;
    this._tab = "devices";
    this._search = this._room = this._category = "";
    if (!device.included) this._showExcluded = true;
    const cardKey = "controls:" + key;
    this._expanded.add(cardKey);
    this._render();
    const card = [...this.shadowRoot.querySelectorAll("details[data-open-key]")]
      .find(node => node.dataset.openKey === cardKey);
    if (card) {
      card.open = true;
      this._expanded.add(cardKey);
      card.scrollIntoView({ behavior: "smooth", block: "start" });
      card.querySelector("summary").focus({ preventScroll: true });
    }
  }

  _deviceTitle(device, section) {
    const names = { pool: "Pool", ev: "Electric vehicle", battery: "Home battery" };
    return section === "planning" && device.system ? names[device.system] : device.name;
  }

  _sortedDevices(section = "controls") {
    return [...this._data.devices].sort((a, b) =>
      this._deviceTitle(a, section).localeCompare(this._deviceTitle(b, section), this._hass?.locale?.language, { sensitivity: "base", numeric: true }) || a.key.localeCompare(b.key));
  }

  _onClick(event) {
    const button = event.target.closest("button");
    if (!button) return;
    const action = button.dataset.action;
    if (!action) return;
    if (action === "edit-field") this._openField(button.dataset.fieldToken);
    if (action === "view-status") this._openStatus(button.dataset.issueKey);
    if (action === "inspect-entity") this.dispatchEvent(new CustomEvent("hass-more-info", { detail: { entityId: button.dataset.entityId }, bubbles: true, composed: true }));
    if (action === "download") this._download();
    if (action === "verification") this._downloadVerification();
    else if (action === "horizon") { this._fullHorizon = !this._fullHorizon; this._render(); }
    else if (action === "schedule-mode") { this._scheduleMode = button.dataset.mode; this._render(); }
    else if (action === "clear-schedule-filters") {
      this._scheduleSearch = this._scheduleRoom = this._scheduleCategory = this._scheduleMode = "";
      this._render();
    }
    else if (action === "slot") { this._selectedSlot = Number(button.dataset.index); this._render(); }
    else if (action === "edit-device") this._openDevice(button.dataset.deviceKey);
    else if (action === "add-field") { this._added.add(button.dataset.token); this._render(); }
    else if (action === "remove-field") this._removeField(button.dataset.scope, button.dataset.fieldKey, button.dataset.deviceKey);
    else if (action === "cancel-device") this._cancelDevice(button.dataset.deviceKey, button.dataset.section);
    else if (action === "back") this._goBack();
    else if (action === "save") this._save();
    else if (action === "save-device") this._saveDevice(button.dataset.deviceKey, button.dataset.section);
    else if (action === "discard") this._discard();
    else if (action === "refresh" && !this._refreshing) this._load(true);
    else if (action === "retry") this._load(false);
    else if (action === "discover") this._discover();
    else if (action === "tab") {
      if (button.dataset.tab === "status") { this._openStatus(); return; }
      this._tab = button.dataset.tab;
      if (this._tab === "devices") { this._search = this._room = this._category = ""; }
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

  _fieldLocations() {
    const locations = [];
    const add = (field, scope, deviceKey, tab, card, name = "") => locations.push({ field, scope, deviceKey, tab, card, name, token: `${scope}:${deviceKey}:${field.key}` });
    for (const device of this._data?.devices || []) {
      for (const field of device.fields || []) add(field, "mapping", device.key, "devices", "controls:" + device.key, device.name);
      for (const section of ["controls", "planning"]) for (const field of this._systemFields(device, section)) add(field, "configuration", device.key, "devices", section + ":" + device.key, this._deviceTitle(device, section));
    }
    const owned = new Set(locations.filter(l => l.scope === "configuration").map(l => l.field.key));
    for (const section of this._data?.sections || []) for (const field of section.fields) {
      if (!owned.has(field.key)) add(field, "configuration", "", section.id === "electrical_limits" ? "schedule" : section.tab, "section:" + section.id);
    }
    return locations;
  }

  _fieldTargets(item) {
    const fix = item.fix || {};
    const configuration = this._data.configuration || this._savedDraft || {};
    return this._fieldLocations().flatMap(location => {
      const match = fix.fields?.find(target => target.key === location.field.key && (target.scope || "configuration") === location.scope && (!target.device_key || target.device_key === location.deviceKey));
      const value = location.scope === "mapping" ? configuration.device_control_mappings?.[location.deviceKey]?.[location.field.key] : configuration[location.field.key];
      const entityMatches = fix.kind === "entity" && fix.entity_id && (value === fix.entity_id || (Array.isArray(value) && value.includes(fix.entity_id)));
      return match || entityMatches ? [{ ...location, message: match?.message || item.items?.join("; ") || item.detail, issue: item }] : [];
    });
  }

  _fieldProblems(key, scope = "configuration", deviceKey = "") {
    const token = `${scope}:${deviceKey}:${key}`;
    return this._attention().flatMap(item => this._fieldTargets(item)).filter(target => target.token === token);
  }

  _fieldProblemText(problems) {
    return [...new Set(problems.flatMap(problem => [problem.message, problem.issue.next_step]).filter(Boolean))].map(message => `<p>${this._escape(message)}</p>`).join("");
  }

  _fieldButtons(targets) {
    return targets.map(target => `<button class="text" data-action="edit-field" data-field-token="${this._escape(target.token)}">Fix ${this._escape([target.name, target.field.label].filter(Boolean).join(" · "))}</button>`).join("");
  }

  _openField(token) {
    const target = this._fieldLocations().find(field => field.token === token);
    if (!target) return;
    this._tab = target.tab;
    this._search = this._room = this._category = "";
    const device = this._data.devices.find(d => d.key === target.deviceKey);
    if (device && !device.included) this._showExcluded = true;
    this._expanded.add(target.card);
    this._added.add(token);
    this._render();
    const field = [...this.shadowRoot.querySelectorAll(".field[data-field-token]")].find(node => node.dataset.fieldToken === token);
    if (field) {
      field.scrollIntoView({ behavior: "smooth", block: "center" });
      field.querySelector("input,select")?.focus({ preventScroll: true });
    }
  }

  _deviceFieldButtons(device) {
    return this._fieldButtons(this._attention().flatMap(item => this._fieldTargets(item)).filter(target => target.deviceKey === device.key));
  }

  _updateAttentionUI() {
    this._updateSaveState();
    const button = this.shadowRoot?.querySelector('button[data-action="tab"][data-tab="status"]');
    if (button) {
      const count = this._attentionForTab("status").length;
      button.classList.toggle("needs-attention", count > 0);
      button.innerHTML = "Status" + this._attentionBadge(count);
    }
    for (const field of this.shadowRoot?.querySelectorAll('.field[data-field-token]') || []) {
      const problems = this._attention().flatMap(item => this._fieldTargets(item)).filter(target => target.token === field.dataset.fieldToken);
      field.classList.toggle("field-problem", problems.length > 0);
      field.querySelector('[data-field-problems]').innerHTML = this._fieldProblemText(problems);
      for (const input of field.querySelectorAll('input,select')) {
        if (problems.length) { input.setAttribute("aria-invalid", "true"); input.setAttribute("aria-describedby", field.querySelector('[data-field-problems]').id); }
        else { input.removeAttribute("aria-invalid"); input.removeAttribute("aria-describedby"); }
      }
    }
    for (const card of this.shadowRoot?.querySelectorAll('details.card[data-open-key]') || []) {
      const badge = card.querySelector('[data-field-count]');
      if (!badge) continue;
      const count = card.querySelectorAll('.field-problem').length;
      badge.hidden = !count;
      badge.textContent = `${count} field${count === 1 ? "" : "s"} need${count === 1 ? "s" : ""} attention`;
      const setup = card.querySelector('[data-setup-status]');
      if (setup) setup.hidden = count > 0;
    }
  }

  _attentionBadge(count) { return count ? `<span class="tab-badge" aria-label="${count} item${count === 1 ? "" : "s"} to fix">${count}</span>` : ""; }

  _renderField(field, value, scope = "configuration", deviceKey = "") {
    const key = this._escape(field.key);
    const label = this._escape(field.label);
    const sensor = typeof value === "string" && value.startsWith("sensor.");
    const entityUnit = sensor ? (this._hass?.states?.[value]?.attributes?.unit_of_measurement ?? this._data?.entities?.find(e => e.entity_id === value)?.unit ?? "") : null;
    const helpText = sensor && ["power", "quantity"].includes(field.kind) ? "Uses the selected sensor's reported unit." : field.help;
    const help = helpText ? `<div class="field-help">${this._escape(helpText)}</div>` : "";
    const token = `${scope}:${deviceKey}:${field.key}`;
    const problems = this._fieldProblems(field.key, scope, deviceKey);
    const issueId = "field-issue-" + encodeURIComponent(token);
    const problemAttributes = problems.length ? `aria-invalid="true" aria-describedby="${issueId}"` : "";
    const required = field.required ? '<span class="required">Required</span>' : "";
    const common = `${problemAttributes} aria-label="${label}" data-field-key="${key}" data-scope="${this._escape(scope)}" data-device-key="${this._escape(deviceKey)}"`;
    let control = "";
    if (field.kind === "toggle") {
      control = `<label class="switch"><input type="checkbox" ${common} ${value ? "checked" : ""}><span></span></label>`;
    } else if (field.kind === "battery_mode") {
      const modeEntity = this._draft?.battery_mode_entity;
      const options = this._hass?.states?.[modeEntity]?.attributes?.options || [];
      control = `<select ${common}><option value="">Select a mode…</option>${value && !options.includes(value) ? `<option selected disabled value="${this._escape(value)}">Unavailable option: ${this._escape(value)}</option>` : ""}${options.map(option => `<option value="${this._escape(option)}" ${option === value ? "selected" : ""}>${this._escape(option)}</option>`).join("")}</select>`;
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
    } else if (field.kind === "entities" && field.max_items === 1) {
      control = `<input type="text" list="shs-entity-list" ${common} value="${this._escape((value || []).join(", "))}" placeholder="Search or enter an entity">`;
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
        <div class="add-row"><input type="text" ${problemAttributes} aria-label="Add ${label}" list="shs-entity-list" placeholder="Search or enter an entity"><button type="button" class="secondary small" data-action="add-multi" data-scope="${this._escape(scope)}" data-device-key="${this._escape(deviceKey)}" data-field-key="${key}">Add</button></div>
      </div>`;
    } else if (field.kind === "number") {
      const displayed = value === undefined || value === null || value === ""
        ? ""
        : Number(value) * (field.scale || 1);
      control = `<div class="with-unit"><input type="number" ${common} value="${this._escape(displayed)}" ${field.step !== undefined ? `step="${field.step}"` : ""} ${field.minimum !== undefined ? `min="${field.minimum}"` : ""} ${field.maximum !== undefined ? `max="${field.maximum}"` : ""}><span>${this._escape(field.unit || "")}</span></div>`;
    } else if (field.kind === "time") {
      control = `<input type="time" ${common} value="${this._escape(value || "")}">`;
    } else if (["quantity", "power"].includes(field.kind)) {
      const unit = field.unit || "W";
      const displayed = typeof value === "number" ? value * (field.scale || 1) : (value ?? "");
      const list = unit === "W" ? "shs-power-list" : unit === "kWh" ? "shs-energy-list" : "shs-percent-list";
      const placeholder = field.kind === "power" ? "Power entity or watts" : `Search sensor or enter ${unit}`;
      control = `<div class="with-unit"><input type="text" list="${list}" ${common} value="${this._escape(displayed)}" placeholder="${this._escape(placeholder)}"><span>${this._escape(sensor ? entityUnit : unit)}</span></div>`;
    } else {
      control = `<input type="text" ${common} ${field.kind === "entity" ? 'list="shs-entity-list"' : ""} value="${this._escape(value || "")}" placeholder="${field.kind === "entity" ? "Search or enter an entity" : ""}">`;
    }
    return `<div data-field-token="${this._escape(token)}" class="field ${problems.length ? "field-problem" : ""} ${field.kind === "toggle" ? "toggle-field" : ""}">
      <div class="field-label"><label>${label}</label>${required}${!field.required ? `<button type="button" class="text" data-action="remove-field" data-scope="${this._escape(scope)}" data-device-key="${this._escape(deviceKey)}" data-field-key="${key}" aria-label="Remove ${label}">Remove</button>` : ""}</div>
      ${control}${help}<div id="${issueId}" data-field-problems class="field-problems">${this._fieldProblemText(problems)}</div>
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
    if (!this._draft && data.configuration) {
      this._draft = this._clone(data.configuration);
      this._savedDraft = this._clone(data.configuration);
      if (!data.configuration.configuration_reviewed_at) this._tab = "energy";
    }
  }

  _canPoll() {
    return this._entryId && !this._loading && !this._saving && !this._savingDeviceKey
      && !document.hidden;
  }

  async _poll(refreshRoles = false) {
    if (this._polling || !this._canPoll()) return;
    this._polling = true;
    this._lastPollAt = Date.now();
    const revision = this._pollRevision;
    const entryId = this._entryId;
    try {
      const data = await this._hass.callWS({ type: "shs_energy/config/get", config_entry: entryId, refresh_roles: refreshRoles });
      if (!this._canPoll() || revision !== this._pollRevision || entryId !== this._entryId) return;
      if (this._acceptRefresh(data)) {
        this._renderBackground();
        return;
      }
      if (this._editing() || this._dirty) this._data = { ...data, configuration: this._data.configuration };
      else this._mergePanel(data);
      this._refreshError = "";
      if (this._editing() || this._dirty) this._updateAttentionUI();
      else this._render();
    } catch (error) {
      if (!this._canPoll() || revision !== this._pollRevision || entryId !== this._entryId) return;
      this._refreshing = false;
      this._refreshError = this._errorMessage(error);
      if (this._editing() || this._dirty) this._updateAttentionUI();
      else this._render();
    } finally { this._polling = false; }
  }

  async _control(key, mode) {
    if (this._refreshing || this._saving || this._savingDeviceKey) return;
    this._pollRevision = (this._pollRevision || 0) + 1;
    this._savingDeviceKey = key; this._error = ""; this._render();
    try {
      const data = await this._hass.callWS({ type: "shs_energy/config/control", config_entry: this._entryId, device_key: key, mode });
      this._mergePanel(data);
      this._notice = "Device mode saved: " + this._label(mode) + ".";
    } catch (error) { this._error = this._errorMessage(error); }
    finally { this._savingDeviceKey = ""; this._renderBackground(); }
  }

  _cancelDevice(key, section) {
    if (section !== "planning" && this._draft[MAPPINGS_KEY]) this._draft[MAPPINGS_KEY][key] = this._clone(this._savedDraft[MAPPINGS_KEY]?.[key]);
    for (const field of this._systemFields(this._data.devices.find(d => d.key === key), section)) {
      if (field.key in this._savedDraft) this._draft[field.key] = this._clone(this._savedDraft[field.key]);
      else delete this._draft[field.key];
    }
    this._clearDeviceError(key); this._render();
  }

  async _downloadVerification() {
    try {
      // Home Assistant builds and compresses the file; the browser only saves it.
      const response = await this._hass.fetchWithAuth(`/api/shs_energy/controller_diagnostics/${encodeURIComponent(this._entryId)}`);
      if (!response.ok) throw new Error((await response.text()).trim() || `Controller diagnostics download failed (HTTP ${response.status}).`);
      const summary = JSON.parse(response.headers.get("X-SHS-Diagnostics-Summary") || "null");
      const url = URL.createObjectURL(await response.blob());
      const a = document.createElement("a"); a.href = url; a.download = "shs-controller-diagnostics.json.gz"; a.click(); URL.revokeObjectURL(url);
      this._notice = summary
        ? `Downloaded controller diagnostics for ${summary.devices} devices, with ${summary.runtime_evaluations} evaluations, ${summary.verification_checks} verification checks and ${summary.observation_samples} observation samples this session. Earlier history is included separately.`
        : "Downloaded controller diagnostics.";
    } catch (error) { this._error = this._errorMessage(error); }
    this._render();
  }

  _diagnosticSummary() {
    // Include the displayed failure and correction path; state alone cannot
    // explain a fault. Keep unrelated configuration and recorder rows out.
    return { version: 2, frontend_version: FRONTEND_VERSION, exported_at: new Date().toISOString(),
      attention: this._attention().map(item => ({ key: item.key, severity: item.severity,
        title: item.title, detail: item.detail, items: item.items, message: item.message, reason: item.reason, next_step: item.next_step, fix: item.fix })),
      plan: { state: this._data.operation.state, issued_at: this._data.operation.issued_at,
        binding_until: this._data.operation.binding_until, valid_until: this._data.operation.valid_until },
      devices: this._data.devices.map((d, index) => ({ device: index + 1, key: d.key, name: d.name, included: d.included,
        mode: d.mode, state: d.execution_status?.state, reason: d.execution_status?.reason,
        next_step: d.execution_status?.next_step, fix: d.execution_status?.fix,
        retry_automatically: d.execution_status?.retry_automatically,
        battery_runtime: d.battery_runtime })),
      delivery: { last_daily_push: this._data.diagnostics.last_daily_push,
        last_plan_push: this._data.readiness.last_plan_push, electrical_history_until: this._data.readiness.actuals_accepted_until,
        thermal_history_until: this._data.diagnostics.thermal_slots_accepted_until },
      upgrade: this._data.diagnostics.migration ? Object.fromEntries(["imported", "removed", "needs_attention"].map(k => [k, this._data.diagnostics.migration[k]?.length || 0])) : null };
  }

  _download() {
    const value = this._diagnosticSummary();
    const url = URL.createObjectURL(new Blob([JSON.stringify(value, null, 2)], { type: "application/json" }));
    const a = document.createElement("a"); a.href = url; a.download = "shs-diagnostics.json"; a.click(); URL.revokeObjectURL(url);
  }

  _present(value) { return value !== undefined && value !== null && value !== "" && (!Array.isArray(value) || value.length > 0); }

  _fields(fields, values, scope = "configuration", deviceKey = "", additional = []) {
    const visible = [], optional = [];
    const linked = {
      offset_entity_id: ["offset_minimum", "offset_maximum"],
    };
    for (const group of [{ fields, values, scope, deviceKey }, ...additional]) {
      const { fields, values, scope, deviceKey } = group;
      const dependent = new Set(Object.entries(linked).filter(([key]) => this._present(values[key])).flatMap(([, keys]) => keys));
      for (const field of fields) {
        const token = `${scope}:${deviceKey}:${field.key}`;
        const required = field.required || dependent.has(field.key);
        const populated = this._present(values[field.key]);
        const inheritedLocation = ["pv_forecast_latitude", "pv_forecast_longitude"].includes(field.key) && !this._data.configured_keys?.includes(field.key);
        if (required || this._fieldProblems(field.key, scope, deviceKey).length || (values[field.key] !== null && (["power", "quantity"].includes(field.kind) || (populated && !inheritedLocation))) || this._added.has(token)) visible.push(this._renderField({ ...field, required }, values[field.key], scope, deviceKey));
        else {
          optional.push(`<button class="text" data-action="add-field" data-token="${this._escape(token)}">Add ${this._escape(field.label.toLowerCase())}</button>`);
        }
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
    const issueCount = fields.filter(f => this._fieldProblems(f.key).length).length;
    const id = "section:" + section.id;
    return `<details class="card compact" data-open-key="${id}" ${this._expanded.has(id) ? "open" : ""}>
      <summary>${this._escape(section.title)}<span class="badge warning" data-field-count ${issueCount ? "" : "hidden"}>${issueCount} field${issueCount === 1 ? "" : "s"} need${issueCount === 1 ? "s" : ""} attention</span><span>${this._escape(summary)}</span></summary>
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
      <p class="muted">Solar location ${this._data.configured_keys?.some(k => ["pv_forecast_latitude", "pv_forecast_longitude"].includes(k)) ? "uses your configured override" : "comes from Home Assistant"}. An override can be added in Solar and electrical measurements.</p>
      <details class="card compact"><summary>Current readings · ${selected.length} selected sources</summary><div class="table-wrap"><table><thead><tr><th>Source</th><th>Used for</th><th>Reading</th><th>Last update</th><th>Selected in</th></tr></thead><tbody>${selected.map(({ field, entity, id }) => `<tr><td>${this._escape(entity?.name || id)}</td><td>${this._escape(field.label)}</td><td>${this._escape(entity ? `${entity.state} ${entity.unit || ""}` : "Unavailable")}</td><td>${this._time(entity?.last_updated)}</td><td>${this._data.configured_keys?.includes(field.key) ? "SHS configuration" : "HA Energy / discovery"}</td></tr>`).join("")}</tbody></table></div></details>`;
  }

  async _saveInclusion() {
    if (this._refreshing || this._saving || this._savingDeviceKey) return;
    const excluded = [...this._draft.excluded_device_readings];
    this._saving = true; this._render();
    try {
      await this._hass.callWS({ type: "shs_energy/config/save", config_entry: this._entryId,
        configuration: { excluded_device_readings: excluded } });
      this._savedDraft.excluded_device_readings = excluded;
      await this._load(false);
    } catch (error) {
      if (error?.code === "refresh_in_progress") this._refreshing = true;
      else { this._refreshing = false; this._error = this._errorMessage(error); }
      this._draft.excluded_device_readings = [...(this._savedDraft.excluded_device_readings || [])];
    } finally { this._saving = false; this._render(); }
  }

  _choices(device, section = "schedule") {
    const permission = device.permission;
    if (device.system_member) {
      // A member shares its system owner's grant; a second selector would imply its own.
      const owner = this._data.devices.find(d => d.system === device.system_member);
      const mode = Object.fromEntries(DEVICE_MODES)[device.mode] || this._label(device.mode);
      return section === "schedule" ? `<div class="choices"><div class="choice-row"><span>Device mode</span><span data-mode="${this._escape(device.mode)}">${this._escape(mode)}</span>
      <small>Runs with ${this._escape(owner?.name || "its system")}. Choose Verification or Controlling there.</small></div></div>` : `<div class="choices"></div>`;
    }
    const disabled = Boolean(this._refreshing || this._saving || this._savingDeviceKey);
    const blocked = value => value === "controlling" && (this._refreshError || this._deviceDirty(device.key) || permission.reason);
    return `<div class="choices">
      ${section === "schedule" ? `<div class="choice-row"><span>Device mode</span><select data-mode="${this._escape(device.mode)}" aria-label="Mode for ${this._escape(device.name)}" data-permission="${this._escape(device.key)}" ${disabled ? "disabled" : ""}>
        ${DEVICE_MODES.map(([value, label]) => `<option data-mode="${value}" value="${value}" ${device.mode === value ? "selected" : ""} ${blocked(value) && ["control_verification", "controlling"].includes(value) && device.mode !== value ? "disabled" : ""}>${label}</option>`).join("")}</select>
      <small>${this._escape(permission.reason || (this._deviceDirty(device.key) ? "Save setup changes first" : "Verification logs proposed commands. Controlling attempts them on the equipment."))}</small></div>` : ""}
    </div>`;
  }

  _renderDevice(device, section = "controls") {
    const planning = section === "planning";
    const included = !(this._draft.excluded_device_readings || []).includes(device.key);
    const mapping = this._mapping(device.key) || {};
    const dirty = this._deviceDirty(device.key, section);
    const id = section + ":" + device.key;
    const edit = this._expanded.has(id) || dirty;
    const systemFields = this._systemFields(device, section);
    const mappingFields = planning ? [] : device.fields || [];
    const issueCount = mappingFields.filter(f => this._fieldProblems(f.key, "mapping", device.key).length).length + systemFields.filter(f => this._fieldProblems(f.key, "configuration", device.key).length).length;
    const title = this._deviceTitle(device, section);
    const members = this._data.devices.filter(d => d.key === device.key || (device.system && d.planning_system === device.system));
    const description = planning
      ? "Model properties and planning participation"
      : `${device.room_name || "No room"} · ${this._label(device.category)}`;
    return `<details class="card device-card" data-open-key="${this._escape(id)}" ${edit ? "open" : ""}>
      <summary><div><strong>${this._escape(title)}</strong><small>${this._escape(description)}</small></div>
        <span class="badge warning" data-field-count ${issueCount ? "" : "hidden"}>${issueCount} field${issueCount === 1 ? "" : "s"} need${issueCount === 1 ? "s" : ""} attention</span><span data-setup-status ${issueCount ? "hidden" : ""}>${planning ? `<span class="badge">${device.planned ? "Planned" : device.included ? "Monitoring" : "Excluded"}</span>` : this._statusBadge(device.included ? device.mapping_status : "base_load", device.included && device.mapping_status === "ready" ? "Controls configured" : undefined)}</span></summary>
      <div class="device-body">
        ${!planning ? `<div class="choice-row"><span>${included ? "Included" : "Excluded"}</span><label class="switch"><input type="checkbox" aria-label="Include ${this._escape(device.name)}" data-share="${this._escape(device.key)}" ${this._refreshing || this._saving || this._savingDeviceKey ? "disabled" : ""} ${included ? "checked" : ""}><span></span></label></div>
        <p class="muted">${included ? "Shares device data with SHS and allows planning and control when configured." : "Sends no individual readings, profiles or metadata to SHS and cannot be planned or controlled. Consumption stays in household totals."}</p>` : ""}
        ${planning ? `<p>Connected equipment: ${members.map(d => this._escape(d.name)).join(", ")}</p>` : mappingFields.length ? `<p>${this._escape(device.name)} · ${this._escape(this._label(device.control_type))}</p>` : ""}
        ${!planning && device.system === "battery" ? this._fieldButtons(this._attention().filter(item => item.key === "battery_control").flatMap(item => this._fieldTargets(item))) : ""}
        ${!planning && device.mapping_error ? `<p class="inline-warning">${this._escape(device.mapping_error)}</p>` : ""}
        ${this._deviceErrors[device.key] ? `<p role="alert" class="inline-error">${this._escape(this._deviceErrors[device.key])}</p>` : ""}
        ${this._fields(mappingFields, mapping, "mapping", device.key, [
          { fields: systemFields, values: this._draft, scope: "configuration", deviceKey: device.key },
        ])}
        ${!planning && Object.keys(device.suggested_mapping || {}).some(k => k !== "control_type" && !this._present(mapping[k])) ? `<button class="text" data-action="use-suggestions" data-device-key="${this._escape(device.key)}">Review suggested setup</button>` : ""}
        ${mappingFields.length || systemFields.length ? `<div class="device-save-row"><span>${dirty ? "Unsaved changes" : "Saved"}</span><button class="text" data-action="cancel-device" data-section="${section}" data-device-key="${this._escape(device.key)}" ${dirty ? "" : "disabled"}>Cancel</button><button class="primary" data-action="save-device" data-section="${section}" data-device-key="${this._escape(device.key)}" ${dirty && !this._refreshing && !this._saving && !this._savingDeviceKey ? "" : "disabled"}>Save ${section}</button></div>` : `<p>Choose how this device runs on the website to set it up here.</p>`}
        ${this._choices(device, section)}
        ${!planning ? `<small class="muted">${this._escape(device.statistic_id || "Equipment settings")}</small>` : ""}
      </div></details>`;
  }

  _deviceFilterKey(key, schedule) {
    return "_" + (schedule ? `schedule${key[0].toUpperCase()}${key.slice(1)}` : key);
  }

  _filterDevices(devices, schedule = false) {
    const value = key => this[this._deviceFilterKey(key, schedule)] || "";
    return devices.filter(d => (!value("search") || `${d.name} ${d.room_name || ""}`.toLowerCase().includes(value("search").trim().toLowerCase()))
      && (!value("room") || (d.room_name || "No room") === value("room"))
      && (!value("category") || d.category === value("category"))
      && (!schedule || !this._scheduleMode || d.mode === this._scheduleMode));
  }

  _renderDeviceFilters(devices, schedule = false) {
    const key = name => this._deviceFilterKey(name, schedule);
    const select = (name, placeholder, values, label) => `<select aria-label="${placeholder}" data-filter="${key(name).slice(1)}"><option value="">${placeholder}</option>${values.map(v => `<option value="${this._escape(v)}" ${this[key(name)] === v ? "selected" : ""}>${this._escape(label(v))}</option>`).join("")}</select>`;
    return `<div class="filters"><input type="text" aria-label="Search devices" placeholder="Search devices" data-filter="${key("search").slice(1)}" value="${this._escape(this[key("search")] || "")}">${select("room", "All rooms", [...new Set(devices.map(d => d.room_name || "No room"))], v => v)}${select("category", "All types", [...new Set(devices.map(d => d.category).filter(Boolean))], v => this._label(v))}</div>`;
  }

  _renderScheduleFilters(devices) {
    const active = this._scheduleMode || this._scheduleSearch || this._scheduleRoom || this._scheduleCategory;
    return `<div class="schedule-filters">${this._renderDeviceFilters(devices, true)}
      <div class="mode-filters" role="group" aria-label="Filter by device mode">${[["", "All modes"], ...DEVICE_MODES].map(([mode, label]) => `<button type="button" class="${(this._scheduleMode || "") === mode ? "primary" : "secondary"} small" data-action="schedule-mode" data-mode="${mode}" aria-pressed="${(this._scheduleMode || "") === mode}">${label}</button>`).join("")}${active ? '<button type="button" class="text small" data-action="clear-schedule-filters">Clear filters</button>' : ""}</div></div>`;
  }

  _renderDevices() {
    const devices = this._sortedDevices();
    const visible = devices.filter(d => this._showExcluded || d.included);
    const filtered = this._filterDevices(visible);
    const equipment = this._data.sections.filter(s => s.toggle && ["battery_enabled", "pool_enabled", "ev_enabled"].includes(s.toggle.key));
    const excludedCount = devices.filter(d => !d.included).length;
    return `<div class="page-intro"><h2>Devices in your home</h2><p>Connect your equipment in Controls, then configure its model in Planning.</p>
      <div class="choice-row"><span>Show excluded devices${excludedCount ? ` (${excludedCount})` : ""}</span><label class="switch"><input type="checkbox" aria-label="Show excluded devices" data-filter="showExcluded" ${this._showExcluded ? "checked" : ""}><span></span></label></div></div>
      <section aria-labelledby="controls-heading"><h2 id="controls-heading">Controls</h2><p>Measurements, actions, operating limits and permission to operate your equipment.</p>
      ${this._renderDeviceFilters(visible)}
      ${filtered.map(d => this._renderDevice(d, "controls")).join("") || `<p>No matching devices.${!this._showExcluded && excludedCount ? " Turn on Show excluded devices to see excluded equipment." : ""}</p>`}</section>
      <section aria-labelledby="planning-heading"><h2 id="planning-heading">Planning</h2><p>Pool, vehicle and home battery properties. Set everyday comfort and charge preferences on the website.</p>
      ${this._sortedDevices("planning").filter(d => d.system && (this._showExcluded || d.included)).map(d => this._renderDevice(d, "planning")).join("") || '<p>No included pool, vehicle or home battery.</p>'}
      <details class="card compact"><summary>Equipment present in this home</summary><p>Choose what is installed. Planning participation is chosen separately on the website.</p>${equipment.map(s => this._renderField({ ...s.toggle, help: "Describes installed equipment; planning participation and control permission are configured separately." }, this._draft[s.toggle.key])).join("")}</details></section>`;
  }

  _scheduleSlot(device, slot) {
    const system = device.system || device.system_member;
    const owner = system ? "$" + system : device.key;
    return slot.execution_owners?.includes(owner) && slot.execution ? slot.execution : slot;
  }

  _controllerDetails(device) {
    return device.controller_explanation ? `<p class="muted">${this._escape(device.controller_explanation).replaceAll("\n", "<br>")}</p>` : "";
  }

  _batteryOutlook(device) {
    const explanation = device.battery_runtime?.explanation;
    if (!explanation || this._refreshError) return '<p class="muted battery-outlook">Waiting for a current battery plan and measurements.</p>';
    const locale = this._hass?.locale?.language || this._hass?.language;
    const timeZone = this._hass?.locale?.time_zone === "local" ? undefined : this._hass?.config?.time_zone;
    const deadline = explanation.deadline_ms == null ? "" : ` Aim to finish by ${new Date(explanation.deadline_ms).toLocaleString(locale, { timeZone, hour: "2-digit", minute: "2-digit", month: "short", day: "numeric" })}.`;
    return `<p class="muted battery-outlook">${this._escape(explanation.plan)}<br>${this._escape(explanation.difference)}<br>${this._escape(explanation.next + deadline)}</p>`;
  }

  _batteryLiveInputs(device) {
    const runtime = device.battery_runtime;
    if (!runtime) {
      const live = device.live_inputs;
      if (!live) return "";
      const labels = {house_consumption_power_entity: "House", solar_production_power_entity: "Solar", battery_power_measurement_entity: "Battery"};
      const readings = Object.entries(labels).map(([key, label]) => {
        const row = live.sources?.[key];
        return `${label}: ${!row || row.state === "unavailable" || live.capture_stale ? "unavailable" : `${(row.watts / 1000).toFixed(2)} kW${row.state === "stale_report" ? " (stale)" : ""}`}`;
      });
      return `<p class="muted">${this._escape(readings.join(" · "))}<br>Waiting for the battery plan and current equipment readings.</p>`;
    }
    const display = runtime.display;
    if (!display) return "";
    return `${display.plan_warning ? `<p class="warning battery-plan-rejection">${this._escape(display.plan_warning)}</p>` : ""}<p class="muted">${this._escape(display.status)}<br>${this._escape(display.now)}${display.loss ? `<br>${this._escape(display.loss)}` : ""}</p>`;
  }

  _scheduleCommand(device, slot) {
    slot = this._scheduleSlot(device, slot);
    const deviceAction = { heating: "heating", cooling: "cooling", hot_water: "heating", pool_heating: "heating", ev_charging: "charging" }[device.category] || "general";
    const result = (text, active = false, action = deviceAction) => ({ text, active, action: active ? action : "idle" });
    const system = device.system || device.system_member;
    if (system && !(slot.capabilities || this._data.timeline?.capabilities)?.[system]) return result("No instruction");
    // A member runs with its system; its own device command was never executable.
    if (device.system_member === "pool") return slot.pool_w > 0 ? result("Runs with pool heating", true, "heating") : result("No heating requested");
    if (device.system_member === "ev") return slot.ev_target_current_a > 0 ? result("Runs with vehicle charging", true, "charging") : result("Charging off");
    if (device.system === "battery") {
      const command = slot.battery_command;
      if (!command || ![2, 3].includes(command.schema_version)) return result("No supported instruction");
      const labels = this._batteryCommandLabels(command);
      // Neither hold nor idle requests anything of the pack, so both stay idle-coloured.
      return result(labels[command.operation] || "No supported instruction",
        Boolean(labels[command.operation]) && !["hold", "idle"].includes(command.operation),
        { solar_charge: "charging", grid_charge: "charging", supply_house: "discharging", export: "discharging" }[command.operation] || "general");
    }
    if (device.system === "ev") return slot.ev_target_current_a > 0 ? result(`Charge ${slot.ev_target_current_a} A`, true, "charging") : result("Charging off");
    if (device.system === "pool") return slot.pool_w > 0 ? result(`Heat · ${slot.pool_w} W planned`, true, "heating") : result("No heating requested");
    const command = slot.commands?.[device.key];
    if (!command) return result("No instruction");
    if (command.type === "setpoint") return result(`Hold ${command.target_c} °C`, true);
    if (command.type === "switch_schedule") return result(command.on_seconds ? "On" : "Off", command.on_seconds > 0);
    if (command.type === "permit_inhibit") return result(command.permitted ? "Allowed to run" : "Paused", command.permitted);
    if (command.type === "variable_power") return result(`${command.value} ${command.unit}`, command.value > 0);
    return result(command.reason || "No supported instruction");
  }

  _scheduleLegend() {
    return `<div class="schedule-legend" aria-label="Requested action colours"><span class="muted">Requested action:</span>${Object.entries(SCHEDULE_ACTIONS).map(([action, { label }]) => `<span><i class="action-swatch" data-schedule-action="${action}" aria-hidden="true"></i>${label}</span>`).join("")}</div>`;
  }

  _batteryCommandLabels(command) {
    const watts = value => `${Math.round(value)} W`;
    return {
      self_consumption: "Solar capture and house supply",
      solar_charge: "Capture surplus · preserve",
      grid_charge: `Charge up to ${watts(command.charge_limit_w)} · grid allowed`,
      supply_house: `Supply house up to ${watts(command.discharge_limit_w)}`,
      export: `Discharge up to ${watts(command.discharge_limit_w)} · export allowed`,
      // Schema 2 held the battery inert; schema 3 keeps absorbing surplus while it holds.
      hold: command.schema_version >= 3 ? "Preserve charge · capture surplus" : "Preserve battery",
      idle: "Preserve charge · export surplus",
    };
  }

  _sameScheduleNumber(left, right) {
    return Number.isFinite(left) && Number.isFinite(right) && Math.abs(left - right) < 0.01;
  }

  _decisionMatchesPlan(device, slot, decision) {
    if (decision.kind === "battery") {
      const command = slot.battery_command;
      return Boolean(command) && decision.operation === command.operation
        && this._sameScheduleNumber(decision.charge_limit_w, command.charge_limit_w)
        && this._sameScheduleNumber(decision.discharge_limit_w, command.discharge_limit_w);
    }
    if (decision.kind === "ev") {
      const current = Number(slot.ev_target_current_a);
      return Number.isFinite(current) && decision.charging === (current > 0)
        && this._sameScheduleNumber(decision.current_a, current);
    }
    if (decision.kind === "pool") return decision.heating === (Number(slot.pool_w) > 0);
    const command = slot.commands?.[device.key];
    if (!command || decision.kind !== command.type) return false;
    if (decision.kind === "setpoint") {
      return Array.isArray(decision.target_c) && decision.target_c.length > 0
        && decision.target_c.every(value => this._sameScheduleNumber(value, command.target_c));
    }
    if (decision.kind === "switch_schedule") return decision.on === (command.on_seconds > 0);
    if (decision.kind === "permit_inhibit") return decision.permitted === command.permitted;
    if (decision.kind === "variable_power") return this._sameScheduleNumber(decision.value, command.value) && decision.unit === command.unit;
    return false;
  }

  _controllerDecisionText(device, slot, decision) {
    const number = value => Number(value.toFixed(6));
    if (decision.kind === "battery") {
      const command = slot.battery_command;
      if (!command || decision.operation !== command.operation) {
        return this._batteryCommandLabels({ ...decision, schema_version: decision.schema_version || 3 })[decision.operation] || "Different battery request";
      }
      const changes = [];
      if (!this._sameScheduleNumber(decision.charge_limit_w, command.charge_limit_w)) changes.push(`Charge limit ${Math.round(decision.charge_limit_w)} W`);
      if (!this._sameScheduleNumber(decision.discharge_limit_w, command.discharge_limit_w)) changes.push(`Discharge limit ${Math.round(decision.discharge_limit_w)} W`);
      return changes.join(" · ");
    }
    if (decision.kind === "ev") return decision.charging ? `Charge ${number(decision.current_a)} A` : `Charging off${decision.reason && decision.reason !== "plan requests charging off" ? ` (${decision.reason})` : ""}`;
    if (decision.kind === "pool") return decision.heating ? "Heating on" : `Heating off${Number.isFinite(decision.water_temperature_c) ? ` at ${number(decision.water_temperature_c)} °C` : ""}`;
    if (decision.kind === "setpoint") return `Hold ${(decision.target_c || []).map(number).join("/")} °C`;
    if (decision.kind === "switch_schedule") return decision.on ? "On" : "Off";
    if (decision.kind === "permit_inhibit") return decision.permitted ? "Allowed to run" : "Paused";
    if (decision.kind === "variable_power") return `${number(decision.value)} ${decision.unit}`;
    return "";
  }

  _scheduleCommandDetail(device, slot) {
    slot = this._scheduleSlot(device, slot);
    const status = device.execution_status || {};
    const decision = device.system === "battery" ? device.battery_runtime?.decision || status.decision : status.decision;
    if (!decision) return "";
    const start = Date.parse(slot.start);
    const durationHours = Number(slot.duration_hours);
    const now = Date.parse(this._data.operation?.now);
    const statusStart = Date.parse(status.slot_start);
    const current = Number.isFinite(start) && Number.isFinite(durationHours) && durationHours > 0
      && Number.isFinite(now) && start <= now && now < start + durationHours * 3600000;
    if (Number.isFinite(statusStart) ? statusStart !== start : !current) return "";
    if (this._decisionMatchesPlan(device, slot, decision)) return "";
    const text = this._controllerDecisionText(device, slot, decision);
    if (!text) return "";
    const label = device.mode === "control_verification" ? "Controller test" : "Controller";
    return `<span class="command-detail muted"> · ${label}: ${this._escape(text)}</span>`;
  }

  _schedulePrices(slot) {
    // `|| 0` turns a rounded -0 into 0 so a near-zero sell price never reads as -0.00.
    const price = (label, value) => Number.isFinite(value) ? `${label} ${(Math.round(value * 100) / 100 || 0).toFixed(2)} SEK/kWh` : "";
    const prices = [price("Buy", slot.shadow_import_sek_per_kwh), price("Sell", slot.shadow_export_sek_per_kwh)].filter(Boolean);
    return prices.length ? `: ${prices.join(", ")}` : "";
  }

  _scheduleDemand(slot) {
    if (!Number.isFinite(slot.load_w) || slot.load_w < 0) return "";
    return ` · Expected house demand: ${(slot.load_w / 1000).toFixed(2)} kW`;
  }

  _renderSchedule() {
    const status = this._data.operation;
    const now = Date.parse(status.now);
    const available = (this._refreshError ? [] : this._data.timeline?.slots || []).filter(slot => Date.parse(slot.start) + 900000 > now);
    const slots = this._fullHorizon ? available : available.filter(slot => Date.parse(slot.start) < now + 86400000);
    const allDevices = this._sortedDevices().filter(d => d.planned);
    const devices = this._filterDevices(allDevices, true);
    const scheduledDevices = devices;
    const start = Date.parse(slots[0]?.start), end = Date.parse(slots.at(-1)?.start) + 900000;
    const position = (now - start) / (end - start) * 100;
    const selectedIndex = Number.isInteger(this._selectedSlot) && slots[this._selectedSlot] ? this._selectedSlot : 0;
    const selected = slots[selectedIndex];
    return `${this._renderScheduleFilters(allDevices)}<div class="card"><div class="status-heading"><h2>Your schedule</h2>${this._refreshError ? this._statusBadge("unavailable", "Status unconfirmed") : this._statusBadge(status.state, status.label)}</div>
      <p>${this._escape(this._refreshError ? "Current status could not be confirmed. The last received status may be stale." : status.reason)}</p>${status.plan_id ? `<p>Plan <code title="${this._escape(status.plan_id)}">${this._escape(status.plan_id.slice(0, 8))}</code> · Issued ${this._time(status.issued_at)}</p>` : ""}
      ${slots.length && scheduledDevices.length ? `<button class="text" data-action="horizon">${this._fullHorizon ? "Show next 24 hours" : "Show full available plan"}</button>${this._scheduleLegend()}<p class="muted">Solid: published prices · Striped: estimated prices · Red line: now. Select a quarter to inspect its requests.</p><div class="timeline-scroll"><div class="timeline"><div class="timeline-times"><span>${this._time(slots[0].start)}</span><span>${this._time(end)}</span></div>
      ${scheduledDevices.map(d => `<div class="timeline-row"><strong>${this._escape(d.name)}</strong><div class="timeline-track">${slots.map((slot, index) => {
        const { text, active, action } = this._scheduleCommand(d, slot);
        const description = `${SCHEDULE_ACTIONS[action].label} · ${text}`;
        return `<button class="slot ${active ? "running" : ""} ${slot.binding ? "" : "advisory"} ${index === selectedIndex ? "selected" : ""}" data-action="slot" data-index="${index}" data-schedule-action="${action}" aria-pressed="${index === selectedIndex}" aria-label="${this._escape(d.name + ', ' + this._time(slot.start) + ', ' + description + (slot.binding ? ', published prices' : ', estimated prices'))}" title="${this._escape(description)}"></button>`;
      }).join("")}${position >= 0 && position <= 100 ? `<span class="now-line" style="left:${position}%"></span>` : ""}</div></div>`).join("")}</div></div>${selected ? `<div aria-live="polite"><h3>${this._time(selected.start)} · ${selected.binding ? "Published prices" : "Estimated prices"}${this._schedulePrices(selected)}${this._scheduleDemand(selected)}</h3><ul>${scheduledDevices.map(d => `<li>${this._escape(d.name)}: Plan: ${this._escape(this._scheduleCommand(d, selected).text)}${this._scheduleCommandDetail(d, selected)}</li>`).join("")}</ul></div>` : ""}` : `<p>${slots.length ? "No Planned devices match these filters." : "No actionable schedule is available. Details are in Status."}</p>`}
      <p>Details show the plan first and add a differing controller decision. Battery requests follow live demand within their limits. Controller tests are hypothetical. Requests do not prove delivery.</p></div>
      <div class="card"><h2>Controller diagnostics</h2><p>Download every Included device, with its current mode, configuration, readings, plan and controller status. Retained runtime evaluations and real service calls are separate from simulated verification commands and coverage. Verification does not prove physical response. The file contains local entity IDs and configuration. Observations are sampled about once a minute in every mode, with up to 720 samples retained. Energy-counter differences are labelled as interval averages, with missing or stale readings identified. Current-session checks and coverage are summarised separately from older evidence. Repeated checks are grouped; up to 2,000 groups of each kind are retained. Battery execution includes its complete accounting journal and its latest 8,192 execution traces. Downloads are gzip-compressed JSON.</p><button class="secondary" data-action="verification">Download controller diagnostics</button></div>
      ${this._data.sections.filter(s => s.id === "electrical_limits").map(s => this._renderSection({ ...s, title: "House electrical limits", fields: s.fields.filter(f => f.key.startsWith("grid_")) })).join("")}
      <p class="muted">Website choices define the planning method. The website owns Monitoring or Planned. Execution here is Verification or Controlling. Website choices last received ${this._time(this._data.portal.refreshed_at)}.</p>
      ${!devices.length ? '<p role="status">No devices match these filters.</p>' : ""}
      ${devices.map(d => `<article class="card schedule-device"><div class="status-heading"><h2>${this._escape(d.name)}</h2><button class="text" data-action="edit-device" data-device-key="${this._escape(d.key)}">Edit setup</button></div>${this._choices(d)}<div class="device-field-issues">${this._deviceFieldButtons(d)}</div>${d.readings?.length ? `<p class="muted">Observed: ${d.readings.map(r => `<span title="${this._escape(r.name + ", updated " + this._time(r.updated_at))}">${this._escape(r.value + " " + r.unit)}</span>`).join(" · ")}</p>` : ""}<small class="muted">${this._escape(this._label(d.execution_status?.state))}${d.execution_status?.reason ? ` · ${this._escape(d.execution_status.reason)}` : ""}${slots[1] && d.included && d.system !== "battery" ? ` · Next quarter ${this._time(slots[1].start)}: ${this._escape(this._scheduleCommand(d, slots[1]).text)}` : ""}</small>${this._batteryLiveInputs(d)}${d.system === "battery" ? this._batteryOutlook(d) : this._controllerDetails(d)}</article>`).join("")}`;
  }

  _attention() {
    const items = [...(this._data?.attention || [])];
    for (const device of this._data?.devices || []) {
      if (device.included && device.planned && Object.keys(device.field_errors || {}).length) {
        const fields = Object.entries(device.field_errors).map(([key, messages]) => ({
          key, scope: key === "pool_water_temperature_entity" ? "configuration" : "mapping",
          device_key: device.key, message: messages.join("; "),
        })).filter(field => !items.some(item => item.fix?.fields?.some(existing =>
          existing.key === field.key && (existing.scope || "configuration") === field.scope &&
          (!existing.device_key || existing.device_key === field.device_key))));
        if (fields.length) items.push({ key: "setup:" + device.key, severity: "warning",
          title: `${device.name}: setup needs attention`, detail: "Complete the highlighted settings before enabling control.",
          fix: { kind: "fields", fields }, device_key: device.key });
      }
      const status = device.execution_status || {};
      if (device.system === "battery" && status.fix?.kind === "fields" && items.some(item => item.key === "battery_control")) continue;
      if (!["fault", "unsupported", "overridden", "limited"].includes(status.state) && !status.handover_pending) continue;
      const key = "controller:" + (device.permission?.controller_id || device.key);
      if (items.some(item => item.key === key)) continue;
      const verification = device.mode === "control_verification";
      items.push({ key, severity: status.state === "fault" ? "error" : "warning",
        title: `${device.name}: ${status.handover_pending ? "handover verification incomplete" : verification ? "control verification needs attention" : "device control needs attention"}`,
        detail: status.reason || "The controller did not provide a reason. Download its evidence and report this missing diagnostic.",
        next_step: (status.next_step || (status.state === "overridden"
          ? "Check the manual override and other automations controlling this device. Once it is ready, set it to Verification and back to Controlling on Schedule to resume."
          : "Review this device's mapped controls and operating settings. If they match the equipment, download the controller evidence and report the reason above for investigation.")) + (status.retry_automatically ? ` ${verification ? "Verification" : "Control"} retries automatically after the problem is corrected.` : ""),
        fix: status.fix || { kind: "device" }, device_key: device.key,
        verification, slot_start: status.slot_start, plan_id: status.plan_id,
      });
    }
    if (this._data?.portal?.error) items.push({ key: "website_refresh", severity: "warning", title: "Website choices could not be refreshed", detail: this._data.portal.error, next_step: "Refresh website choices to retry. The last received choices remain in use.", fix: { kind: "refresh_choices" } });
    if (this._refreshError) items.push({ key: "status_refresh", severity: "error", title: "Current status could not be refreshed",
      detail: this._refreshError, next_step: "Check the connection to Home Assistant, then retry the status refresh. Displayed states may be stale.", fix: { kind: "refresh" } });
    return items.sort((a, b) => ({error:0, warning:1, info:2}[a.severity] ?? 2) - ({error:0, warning:1, info:2}[b.severity] ?? 2));
  }

  _attentionForTab(tab) { return tab === "status" ? this._attention().filter(i => i.severity !== "info") : []; }

  _openStatus(key) {
    this._tab = "status";
    if (key && !this._attention().some(item => item.key === key)) this._notice = "This warning is no longer reported in the latest status.";
    this._render();
    const cards = [...this.shadowRoot.querySelectorAll("[data-attention-key]")];
    const card = key ? cards.find(node => node.dataset.attentionKey === key) : cards[0];
    if (card) { card.scrollIntoView({ behavior: "smooth", block: "start" }); card.focus({ preventScroll: true }); }
  }

  _attentionActions(item) {
    const fix = item.fix || {};
    const device = this._data.devices.find(d => d.key === (fix.device_key || item.device_key) || (fix.system && d.system === fix.system));
    const targets = this._fieldTargets(item);
    const edit = device && !targets.length && (!fix.kind || fix.kind === "device") ? `<button class="text" data-action="edit-device" data-device-key="${this._escape(device.key)}">Edit ${this._escape(device.name)} setup</button>` : "";
    const affected = this._sortedDevices().filter(d => fix.device_keys?.includes(d.key)).map(d => `<button class="text" data-action="edit-device" data-device-key="${this._escape(d.key)}">Edit ${this._escape(d.name)} setup</button>`).join("");
    let primary = "";
    if (fix.url) primary = `<a href="${this._escape(fix.url)}" target="_blank" rel="noreferrer">Open website settings</a>`;
    else if (fix.kind === "entity" && fix.entity_id) primary = `<button class="text" data-action="inspect-entity" data-entity-id="${this._escape(fix.entity_id)}">Inspect source entity</button>`;
    else if (fix.kind === "diagnostics") primary = `<button class="secondary" data-action="download">Download diagnostics</button>`;
    else if (fix.kind === "refresh_choices") primary = `<button class="secondary" data-action="refresh">Refresh website choices</button>`;
    else if (fix.kind === "refresh") primary = `<button class="secondary" data-action="retry">Retry status refresh</button>`;
    else if (!device) primary = TABS.filter(([id]) => id !== "status" && (fix.tabs || [fix.tab]).includes(id)).map(([id, name]) => `<button class="text" data-action="tab" data-tab="${id}">Open ${name}</button>`).join("");
    const evidence = item.device_key && fix.kind !== "diagnostics" ? `<button class="text" data-action="${item.verification ? "verification" : "download"}">${item.verification ? "Download verification log" : "Download diagnostics"}</button>` : "";
    return primary + this._fieldButtons(targets) + edit + affected + evidence;
  }

  _renderAttention() {
    return this._attention().map(item => `<article class="attention-item ${this._escape(item.severity)}" data-attention-key="${this._escape(item.key)}" tabindex="-1"><strong>${this._escape(item.title)}</strong><p>${this._escape(item.detail)}</p>${item.next_step ? `<p><strong>What to do:</strong> ${this._escape(item.next_step)}</p>` : ""}${item.items?.length ? `<ul>${item.items.map(i => `<li>${this._escape(i)}</li>`).join("")}</ul>` : ""}
      ${item.slot_start ? `<p class="muted">Plan quarter: ${this._time(item.slot_start)}${item.plan_id ? ` · Plan ${this._escape(item.plan_id)}` : ""}</p>` : ""}
      ${this._attentionActions(item)}</article>`).join("");
  }

  _renderStatus() {
    const status = this._data.operation, values = this._data.diagnostics, readiness = this._data.readiness;
    const rows = (items) => `<dl>${items.map(([label, value]) => `<div><dt>${this._escape(label)}</dt><dd>${this._escape(value ?? "Not yet")}</dd></div>`).join("")}</dl>`;
    const warningCount = this._attentionForTab("status").length;
    const controllers = this._sortedDevices().map(d => [d.name, `${this._label(d.execution_status?.state)}${d.execution_status?.reason ? ': ' + d.execution_status.reason : ''}`]);
    return `<section class="card"><div class="status-heading"><h2>Integration status</h2>${warningCount ? this._statusBadge("warning", "Needs attention") : this._statusBadge(status.state, status.label)}</div><p>Planning: ${this._escape(this._refreshError ? "Current status is unconfirmed." : status.reason)}</p>${warningCount ? `<p>${warningCount} item${warningCount === 1 ? "" : "s"} need${warningCount === 1 ? "s" : ""} attention below.</p>` : ""}${status.recovering ? `<p>Requesting a fresh plan automatically…</p>` : status.retry_at && !status.actionable ? `<p>Automatic recovery will retry at ${this._time(status.retry_at)}.</p>` : ""}${values.last_optimisation_error ? `<p role="alert">Latest planning error: ${this._escape(values.last_optimisation_error)}</p>` : ""}${values.last_runtime_error ? `<p role="alert">Website status delivery failed: ${this._escape(values.last_runtime_error)}</p>` : ""}${this._refreshError ? `<p role="alert">Status refresh failed: ${this._escape(this._refreshError)}. These readings may be stale.</p>` : ""}<button class="secondary" data-action="download">Download redacted diagnostics</button></section>
      ${this._attention().length ? `<div class="attention" aria-label="Status details">${this._renderAttention()}</div>` : ""}
      <details class="card diagnostics compact"><summary>Data delivery</summary>${rows([["Latest status delivered to website", this._time(values.last_runtime_report)], ["Latest daily delivery", this._time(values.last_daily_push)], ["Latest daily error", values.last_daily_push_error], ["Latest planning attempt", this._time(readiness.last_plan_attempt)], ["Latest successful exchange", this._time(readiness.last_plan_push)], ["Electrical readings received through", this._time(readiness.actuals_accepted_until)], ["New electrical quarters in latest exchange", readiness.actual_slots_accepted], ["Room readings received through", this._time(values.thermal_slots_accepted_until)], ["New room quarters in latest exchange", values.last_thermal_slots_accepted], ["Website choices received", this._time(this._data.portal.refreshed_at)]])}<p>Zero new quarters does not erase earlier history.</p></details>
      <details class="card diagnostics compact"><summary>Planner</summary>${rows([["Current plan", status.label], ["Plan identifier", status.plan_id], ["Issued", this._time(status.issued_at)], ["Published prices until", this._time(status.binding_until)], ["Valid until", this._time(status.valid_until)], ["Latest exchange error", values.last_optimisation_error], ["Tariff", this._label(values.tariff_status)], ["Subscription", values.subscription_active ? "Active" : "Inactive or unavailable"]])}${readiness.missing_inputs?.length ? `<ul>${readiness.missing_inputs.map(i => `<li>${this._escape(i)}</li>`).join("")}</ul>` : ""}</details>
      <details class="card diagnostics compact"><summary>Device execution</summary>${rows(controllers)}<p>A confirmed target means Home Assistant accepted the setting. Physical delivery requires a measurement.</p></details>
      ${values.migration ? `<details class="card compact"><summary>Configuration upgrade</summary>${[["imported", "Settings carried forward"], ["removed", "Old fields removed"], ["needs_attention", "Setup gaps found at upgrade"]].map(([key, label]) => `<h3>${label}</h3><ul>${(values.migration[key] || []).map(name => `<li>${this._escape(name)}</li>`).join("")}</ul>`).join("")}</details>` : ""}`;
  }

  _renderMeasurementIssues() {
    const issues = this._data.measurement_issues || [];
    if (!issues.length) return "";
    return `<aside class="card alert warning" role="status" data-measurement-issues><h2>Left out of this plan: sensor readings could not be used</h2><p>Only these devices are affected; the rest of the home is planned as usual. Once a sensor reports a real value again, a manual replan is recommended.</p><ul>${issues.map(issue => `<li>${this._escape(issue.reason)}${issue.entity_id ? ` <button class="text" data-action="inspect-entity" data-entity-id="${this._escape(issue.entity_id)}">Inspect source entity</button>` : ""}</li>`).join("")}</ul></aside>`;
  }

  _renderReplanRecommendations() {
    const reasons = this._data.replan_recommendations || [];
    if (!reasons.length) return "";
    return `<aside class="card alert warning" role="status"><h2>Manual replan recommended</h2><p>The current schedule is retained. Use Replan now on the website’s Plan tab to change it.</p><details><summary>Reasons and times</summary><ul>${reasons.map(r => `<li>${this._escape(r.reason)} · ${this._time(r.occurred_at)}</li>`).join("")}</ul></details></aside>`;
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
    return `<datalist id="shs-entity-list">${options(this._data.entities)}</datalist><datalist id="shs-power-list">${options(powerEntities)}</datalist><datalist id="shs-energy-list">${options(this._data.entities.filter(e => e.domain === "sensor" && ["Wh", "kWh", "MWh"].includes(e.unit)))}</datalist><datalist id="shs-percent-list">${options(this._data.entities.filter(e => e.domain === "sensor" && e.unit === "%"))}</datalist>`;
  }

  _render() {
    if (!this.shadowRoot) return;
    const active = this.shadowRoot.activeElement;
    const focusKey = active?.dataset.fieldKey;
    const focusDevice = active?.dataset.deviceKey;
    const focusFilter = active?.dataset.filter;
    const focusMode = active?.dataset.action === "schedule-mode" ? active.dataset.mode : null;
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
    if (!this._data || !this._draft) {
      if (this._refreshing) this.shadowRoot.innerHTML = `${this._styles()}<main class="shell">${this._refreshBanner()}</main>`;
      return;
    }

    this.shadowRoot.innerHTML = `${this._styles()}
      <main class="shell">
        <header class="topbar">
          <button type="button" class="icon-button" data-action="back" aria-label="Back">←</button>
          <div class="title"><h1>SHS Energy configuration</h1><p>${this._escape(this._data.entry.title)} · ${this._escape(this._data.entry.state)}</p></div>
          <div class="toolbar">
            <button type="button" class="secondary" data-action="refresh" ${this._loading || this._refreshing ? "disabled" : ""}>${this._loading ? "Refreshing…" : "Refresh website choices"}</button>
            <button type="button" class="secondary" data-action="discover" ${this._loading ? "disabled" : ""}>Review sources from HA Energy</button>
            <button type="button" class="text" data-action="discard" ${this._dirty ? "" : "disabled"}>Discard</button>
            <button type="button" class="primary" data-action="save" ${this._configurationDirty && !this._refreshing && !this._saving && !this._savingDeviceKey ? "" : "disabled"}>${this._saving ? "Saving…" : "Save changes"}</button>
          </div>
        </header>
        <nav class="tabs" aria-label="Configuration sections">${TABS.map(([id, label]) => { const count = this._attentionForTab(id).length; return `<button type="button" data-action="tab" data-tab="${id}" class="${this._tab === id ? "active" : ""}${count ? " needs-attention" : ""}">${label}${this._attentionBadge(count)}</button>`; }).join("")}</nav>
        <section class="content">
          <div data-refresh-progress>${this._refreshBanner()}</div>
          ${this._error ? `<div class="alert error"><strong>Could not save or refresh</strong><span>${this._escape(this._error)}</span></div>` : ""}
          ${this._notice ? `<div class="alert notice"><span>${this._escape(this._notice)}</span></div>` : ""}
          ${this._renderMeasurementIssues()}${this._renderReplanRecommendations()}${this._renderBody()}
        </section>
        <footer aria-live="polite"><span>${this._dirty ? "Unsaved changes" : "All changes saved"}</span><span>Website choices define the planning method. The website owns Monitoring or Planned. Execution here is Verification or Controlling.</span></footer>
        ${this._renderDatalist()}
      </main>`;
    const fields = [...this.shadowRoot.querySelectorAll("input,select")];
    const target = fields.find(el => focusFilter ? el.dataset.filter === focusFilter : focusKey && el.dataset.fieldKey === focusKey && el.dataset.deviceKey === focusDevice);
    if (focusMode !== null) this.shadowRoot.querySelector(`[data-action="schedule-mode"][data-mode="${focusMode}"]`)?.focus();
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
      .schedule-filters { margin-bottom:18px; }
      .schedule-filters .filters { margin-bottom:10px; }
      .mode-filters { display:flex; gap:8px; flex-wrap:wrap; }
      .compact summary { cursor:pointer; font-weight:600; padding:8px 0; }
      .compact summary span { display:block; font-size:13px; font-weight:400; color:var(--secondary-text-color); margin-top:6px; }
      .source-list { display:grid; gap:8px; margin:15px 0; }
      .source-list small { color:var(--secondary-text-color); }
      .command-detail { overflow-wrap:anywhere; }
      .timeline-scroll { overflow-x:auto; }
      .timeline { min-width:720px; }
      .timeline-row { display:grid; grid-template-columns:160px 1fr; gap:12px; padding:10px 0; align-items:center; }
      .timeline-track { display:flex; height:30px; position:relative; background:var(--secondary-background-color); }
      .slot { flex:1; min-width:2px; padding:0; border:0; border-right:1px solid var(--card-background-color); background:transparent; }
      ${Object.entries(SCHEDULE_ACTIONS).map(([action, { colour }]) => `[data-schedule-action="${action}"] { --schedule-action-colour:${colour}; }`).join("\n")}
      .slot.running { background-color:var(--schedule-action-colour); }
      .slot.advisory { background-image:repeating-linear-gradient(45deg,transparent,transparent 2px,#4a556855 2px,#4a556855 4px); }
      .slot.selected { box-shadow:inset 0 0 0 2px var(--primary-color); z-index:1; }
      .schedule-legend { display:flex; flex-wrap:wrap; gap:8px 16px; margin:12px 0; font-size:13px; }
      .schedule-legend > span { display:inline-flex; align-items:center; gap:6px; }
      .action-swatch { width:12px; height:12px; border-radius:3px; background:var(--schedule-action-colour); border:1px solid var(--divider-color); }
      .now-line { position:absolute; height:100%; width:2px; background:var(--error-color); pointer-events:none; }
      .timeline-times { display:flex; justify-content:space-between; margin-left:172px; color:var(--secondary-text-color); font-size:12px; }
      .status-heading { display:flex; justify-content:space-between; gap:18px; align-items:center; }
      .info { border-color:var(--primary-color) !important; }
      .table-wrap { overflow-x:auto; }
      .muted { color:var(--secondary-text-color); }
      [hidden] { display:none !important; }
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
      .attention-item { min-width:0; overflow-wrap:anywhere; scroll-margin-top:150px; margin:0; border:1px solid var(--warning-color, #ff9800); border-left-width:4px; border-radius:12px; padding:14px 16px; background:var(--card-background-color); }
      .attention-item button { max-width:100%; overflow-wrap:anywhere; }
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
      .field-problem { border:2px solid var(--warning-color, #ff9800); border-radius:10px; padding:12px; scroll-margin-top:180px; }
      .field-problems { grid-column:1/-1; color:var(--warning-color, #ff9800); font-size:13px; overflow-wrap:anywhere; }
      .field-problems:empty { display:none; }
      .field-problems p { margin:8px 0 0; }
      .device-field-issues button { border:1px solid var(--warning-color, #ff9800); margin:4px; }
      .field-grid { display:grid; grid-template-columns:repeat(2, minmax(0,1fr)); gap:18px 24px; }
      .field { min-width:0; }
      .field-label { display:flex; justify-content:space-between; gap:8px; align-items:center; min-height:22px; margin-bottom:7px; }
      .field-label label { font-weight:600; }
      .required { color:var(--error-color); font-size:11px; text-transform:uppercase; letter-spacing:.04em; }
      .field-help { margin-top:7px; color:var(--secondary-text-color); font-size:12px; line-height:1.4; }
      input[type=text], input[type=number], input[type=time], select { width:100%; min-height:48px; padding:10px 12px; border:1px solid var(--divider-color); border-radius:10px; color:var(--primary-text-color); background:var(--secondary-background-color); outline:none; }
      /* SHS Silver/Slate, Sky/Nordic Blue, Amber, and Sage/Forest.
         Amber and Forest are darkened for readable text on light surfaces. */
      [data-mode="monitoring"] { color:${this._hass?.themes?.darkMode ? "#a2aec0" : "#4a5568"}; }
      [data-mode="planning"] { color:${this._hass?.themes?.darkMode ? "#a8cfe8" : "#2c5f8d"}; }
      [data-mode="control_verification"] { color:${this._hass?.themes?.darkMode ? "#f6c573" : "#855e20"}; }
      [data-mode="controlling"] { color:${this._hass?.themes?.darkMode ? "#9dc4ad" : "#416853"}; }
      option[data-mode] { background:var(--secondary-background-color); }
      option[data-mode]:disabled { color:var(--disabled-text-color); }
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
      .device-card { padding:0; overflow:hidden; scroll-margin-top:150px; }
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
      .refresh-progress .spinner { width:16px; height:16px; flex:none; border-width:2px; }
      @media (prefers-reduced-motion: reduce) { .spinner { animation:none !important; } }
      .spinner { width:36px; height:36px; border:3px solid var(--divider-color); border-top-color:var(--primary-color); border-radius:50%; animation:spin .8s linear infinite; }
      @keyframes spin { to { transform:rotate(360deg); } }
      @media (max-width:1000px) { .topbar { flex-wrap:wrap; } .toolbar { width:100%; } .tabs { top:132px; } }
      @media (max-width:700px) { .topbar { padding:12px; position:relative; } .tabs { top:0; position:sticky; padding:8px 12px; } .content { padding:16px 12px 88px; } .toolbar { display:grid; grid-template-columns:1fr 1fr; } .field-grid { grid-template-columns:1fr; } .card { padding:18px; border-radius:13px; } .device-card { padding:0; } .diagnostics dl div { grid-template-columns:1fr; gap:5px; } footer span:last-child { display:none; } }
    </style>`;
  }
}

if (!customElements.get(PANEL_ELEMENT)) {
  customElements.define(PANEL_ELEMENT, ShsEnergyConfigPanel);
}
