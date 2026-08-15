const TABS = [
  ["overview", "Overview"],
  ["inputs", "Energy inputs"],
  ["devices", "Devices"],
  ["thermal", "Thermal"],
  ["storage", "Storage & EV"],
  ["diagnostics", "Diagnostics"],
];

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
    this._tab = "overview";
    this._loading = false;
    this._saving = false;
    this._savingDeviceKey = "";
    this._deviceErrors = {};
    this._error = "";
    this._notice = "";
    this._entryId = new URLSearchParams(window.location.search).get("config_entry");
    this._boundClick = (event) => this._onClick(event);
    this._boundChange = (event) => this._onChange(event);
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
    window.addEventListener("beforeunload", this._boundBeforeUnload);
    this._render();
    if (this._hass && !this._data && !this._loading) {
      this._load(true);
    }
  }

  disconnectedCallback() {
    this.shadowRoot.removeEventListener("click", this._boundClick);
    this.shadowRoot.removeEventListener("change", this._boundChange);
    window.removeEventListener("beforeunload", this._boundBeforeUnload);
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

  _human(value) {
    return String(value ?? "")
      .replaceAll("_", " ")
      .replace(/\b\w/g, (character) => character.toUpperCase());
  }

  get _dirty() {
    return JSON.stringify(this._draft) !== JSON.stringify(this._savedDraft);
  }

  get _configurationDirty() {
    const draft = this._clone(this._draft || {});
    const saved = this._clone(this._savedDraft || {});
    delete draft[MAPPINGS_KEY];
    delete saved[MAPPINGS_KEY];
    return JSON.stringify(draft) !== JSON.stringify(saved);
  }

  _deviceDirty(deviceKey) {
    const draft = this._draft?.[MAPPINGS_KEY]?.[deviceKey];
    const saved = this._savedDraft?.[MAPPINGS_KEY]?.[deviceKey];
    return JSON.stringify(draft) !== JSON.stringify(saved);
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
      this._data = data;
      this._deviceErrors = {};
      if (!data.requires_entry_selection) {
        this._entryId = data.entry.entry_id;
        this._draft = this._clone(data.configuration);
        this._savedDraft = this._clone(data.configuration);
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
    const configuration = this._clone(this._draft);
    delete configuration[MAPPINGS_KEY];
    try {
      await this._hass.callWS({
        type: "shs_energy/config/save",
        config_entry: this._entryId,
        configuration,
      });
      const savedMappings = this._clone(this._savedDraft?.[MAPPINGS_KEY] || {});
      this._savedDraft = this._clone(this._draft);
      this._savedDraft[MAPPINGS_KEY] = savedMappings;
      this._notice =
        "General configuration saved. Device-card drafts still need their own Save configuration button.";
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
      });
      const savedMapping = this._clone(
        result.panel?.configuration?.[MAPPINGS_KEY]?.[deviceKey] ?? mapping
      );
      if (result.panel) this._data = result.panel;
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
      if (device) {
        device.mapping_status = result.mapping_status;
        device.mapping_error = result.mapping_error;
        device.mapping_summary = result.mapping_summary || {};
      }
      this._notice = mapping
        ? `${device?.name || deviceKey} is saved and ${result.mapping_status === "ready" ? "ready" : this._human(result.mapping_status)} on the website.`
        : `${device?.name || deviceKey} is no longer locally mapped.`;
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
      const mappings = this._clone(this._draft?.[MAPPINGS_KEY] || {});
      this._draft = this._clone(result.configuration);
      this._draft[MAPPINGS_KEY] = mappings;
      this._draft.automatic_setup = true;
      this._notice =
        "Automatic discovery produced a reviewable draft. Nothing has been saved yet.";
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
      "The local mapping was removed from the draft. Until mapped again, this device stays in measured base load.";
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
        : this._data.sections.flatMap((section) => section.fields);
    return { key, scope, deviceKey, field: fields.find((item) => item.key === key) };
  }

  _onChange(event) {
    const element = event.target;
    if (!(element instanceof HTMLInputElement || element instanceof HTMLSelectElement)) {
      return;
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
    if (action === "back") this._goBack();
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
    return `<span class="badge ${this._escape(status)}">${this._escape(label || this._human(status))}</span>`;
  }

  _renderField(field, value, scope = "configuration", deviceKey = "") {
    const key = this._escape(field.key);
    const label = this._escape(field.label);
    const help = field.help ? `<div class="field-help">${this._escape(field.help)}</div>` : "";
    const isRequired = field.required || (
      field.required_when && Boolean(this._draft?.[field.required_when])
    );
    const required = isRequired ? '<span class="required">Required</span>' : "";
    const common = `data-field-key="${key}" data-scope="${this._escape(scope)}" data-device-key="${this._escape(deviceKey)}"`;
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
        <div class="add-row"><input type="text" list="shs-entity-list" placeholder="Search or enter an entity"><button type="button" class="secondary small" data-action="add-multi" data-scope="${this._escape(scope)}" data-device-key="${this._escape(deviceKey)}" data-field-key="${key}">Add</button></div>
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

  _renderSection(section) {
    return `<section class="card form-card">
      <h2>${this._escape(section.title)}</h2>
      ${section.description ? `<p class="description">${this._escape(section.description)}</p>` : ""}
      <div class="field-grid">
        ${section.fields
          .map((field) => this._renderField(field, this._draft[field.key]))
          .join("")}
      </div>
    </section>`;
  }

  _renderSections(tab) {
    return this._data.sections
      .filter((section) => section.tab === tab)
      .map((section) => this._renderSection(section))
      .join("");
  }

  _readinessCard(title, state, detail, items = []) {
    return `<article class="summary-card ${this._escape(state)}">
      <div class="summary-top"><h3>${this._escape(title)}</h3>${this._statusBadge(state)}</div>
      <p>${this._escape(detail)}</p>
      ${items.length ? `<ul>${items.map((item) => `<li>${this._escape(item)}</li>`).join("")}</ul>` : ""}
    </article>`;
  }

  _renderOverview() {
    const readiness = this._data.readiness;
    const thermal = this._data.thermal;
    const portal = this._data.portal;
    const mappingState = readiness.ready_devices === readiness.requested_devices ? "ready" : "warning";
    const inputState = readiness.missing_inputs.length ? "warning" : "ready";
    const thermalState = thermal.status === "observations_published" || thermal.status === "not_requested" ? "ready" : "warning";
    return `
      <div class="safety-note"><strong>Configuration and visualization only.</strong> This panel does not call, replace or enable any heater, charger, relay, climate entity or Node-RED flow.</div>
      <div class="summary-grid">
        ${this._readinessCard(
          "Website roles",
          portal.status === "synchronised" ? "ready" : "error",
          portal.status === "synchronised"
            ? `${portal.requested_devices} controllable device request${portal.requested_devices === 1 ? "" : "s"} received.`
            : "The last saved website roles are shown because refresh failed.",
          portal.error ? [portal.error] : []
        )}
        ${this._readinessCard(
          "Local device mappings",
          mappingState,
          `${readiness.ready_devices} of ${readiness.requested_devices} requested devices are ready.`,
          readiness.device_mapping_gaps
        )}
        ${this._readinessCard(
          "Electrical planner",
          inputState,
          readiness.missing_inputs.length
            ? "The electrical plan is waiting for the inputs below."
            : "No currently reported electrical input gaps.",
          readiness.missing_inputs
        )}
        ${this._readinessCard(
          "Thermal observations",
          thermalState,
          this._thermalStatusText(thermal),
          thermal.zones.filter((zone) => zone.mapping_status !== "ready").map((zone) => `${zone.name}: ${zone.mapping_error || this._human(zone.mapping_status)}`)
        )}
      </div>
      <section class="card workflow">
        <div><span>1</span><strong>Choose roles on the website</strong><small>Base-load devices need no local setup.</small></div>
        <div><span>2</span><strong>Refresh website roles</strong><small>The latest request is fetched whenever this panel opens.</small></div>
        <div><span>3</span><strong>Map local entities</strong><small>Review suggestions; incomplete controls stay in base load.</small></div>
        <div><span>4</span><strong>Observe readiness</strong><small>History and plans become visible without changing existing control ownership.</small></div>
      </section>
      ${this._renderSections("overview")}
    `;
  }

  _thermalStatusText(thermal) {
    const messages = {
      not_requested: "No setpoint-controlled thermal zones are requested by the website.",
      device_mappings_required: `${thermal.mapped_zones} of ${thermal.requested_zones} heating-device mappings are complete.`,
      outdoor_sources_required: "Zone mappings are ready; measured outdoor temperature and weather forecast still need confirmation.",
      waiting_for_history: "Inputs are mapped. Waiting for complete recorder quarters to be accepted by the website.",
      observations_published: thermal.accepted_until
        ? `Thermal observations have been accepted through ${thermal.accepted_until}. ${thermal.last_slots_accepted} new or repeated slots were accepted in the latest exchange.`
        : `${thermal.last_slots_accepted} thermal observation slots were accepted in the latest exchange.`,
    };
    return messages[thermal.status] || this._human(thermal.status);
  }

  _renderDevice(device) {
    const mapping = this._mapping(device.key) || {};
    const dirty = this._deviceDirty(device.key);
    const saving = this._savingDeviceKey === device.key;
    const deviceError = this._deviceErrors[device.key] || "";
    const saveStatus = deviceError
      ? "Save failed — correct the message above"
      : dirty
      ? "Unsaved changes in this card"
      : device.mapping_status === "ready"
        ? "Saved and ready"
        : "Complete the required fields, then save this card";
    const open = device.mapping_status !== "ready" || dirty ? "open" : "";
    const suggestionCount = Object.keys(device.suggested_mapping || {}).filter(
      (key) => key !== "control_type" && mapping[key] === undefined
    ).length;
    return `<details class="card device-card" ${open}>
      <summary>
        <div><strong>${this._escape(device.name)}</strong><small>${this._escape(device.statistic_id)}</small></div>
        <div class="device-summary">${this._statusBadge(device.mapping_status)}<span>${this._escape(this._human(device.control_type))}</span></div>
      </summary>
      <div class="device-body">
        <div class="device-meta"><span>${this._escape(this._human(device.category))}</span><span>${this._escape(this._human(device.load_type))}</span><span>Website: controllable</span></div>
        ${device.stale_mapping_control_type ? `<div class="inline-warning">The website changed this device from ${this._escape(this._human(device.stale_mapping_control_type))} to ${this._escape(this._human(device.control_type))}. The old mapping is ignored.</div>` : ""}
        ${device.mapping_error ? `<div class="inline-warning"><strong>Currently saved configuration:</strong> ${this._escape(device.mapping_error)}</div>` : ""}
        ${deviceError ? `<div class="inline-error"><strong>Could not save this configuration</strong><span>${this._escape(deviceError)}</span></div>` : ""}
        <div class="device-actions">
          <div class="device-action-group"><button type="button" class="secondary" data-action="use-suggestions" data-device-key="${this._escape(device.key)}" ${suggestionCount ? "" : "disabled"}>Use ${suggestionCount} suggestion${suggestionCount === 1 ? "" : "s"}</button>
          <button type="button" class="text danger" data-action="clear-mapping" data-device-key="${this._escape(device.key)}">Remove local mapping</button></div>
        </div>
        <div class="field-grid">
          ${device.fields
            .map((field) => this._renderField(field, mapping[field.key], "mapping", device.key))
            .join("")}
        </div>
        <div class="device-save-row">
          <span>${this._escape(saveStatus)}</span>
          <button type="button" class="primary" data-action="save-device" data-device-key="${this._escape(device.key)}" ${dirty && !this._savingDeviceKey && !this._saving ? "" : "disabled"}>${saving ? "Saving…" : "Save configuration"}</button>
        </div>
      </div>
    </details>`;
  }

  _renderDevices() {
    const devices = this._data.devices;
    return `
      <section class="page-intro">
        <h2>Website-requested controllable devices</h2>
        <p>Only these devices are separated from measured base load. A request becomes effective only after its local mapping is complete and saved.</p>
      </section>
      ${devices.length ? devices.map((device) => this._renderDevice(device)).join("") : `<section class="card empty"><h2>No controllable devices requested</h2><p>Choose a device and its control method on the SHS website, then refresh website roles here.</p></section>`}
      ${this._renderSections("devices")}
    `;
  }

  _renderThermal() {
    const thermal = this._data.thermal;
    return `
      <section class="card thermal-status">
        <div class="summary-top"><div><h2>Thermal model input status</h2><p>${this._escape(this._thermalStatusText(thermal))}</p></div>${this._statusBadge(thermal.status === "observations_published" || thermal.status === "not_requested" ? "ready" : "warning", this._human(thermal.status))}</div>
        <div class="thermal-grid">
          <div><strong>${thermal.mapped_rooms}</strong><span>rooms from ${thermal.mapped_zones}/${thermal.requested_zones} mapped heaters</span></div>
          <div><strong>${thermal.outdoor_temperature_ready ? "Ready" : "Missing"}</strong><span>measured outdoor temperature</span></div>
          <div><strong>${thermal.weather_forecast_ready ? "Ready" : "Missing"}</strong><span>outdoor forecast</span></div>
          <div><strong>${this._escape(thermal.accepted_until || "—")}</strong><span>observations accepted through</span></div>
        </div>
        ${thermal.zones.length ? `<table><thead><tr><th>Heating meter</th><th>Room</th><th>Mapping</th><th>Reason</th></tr></thead><tbody>${thermal.zones.map((zone) => `<tr><td>${this._escape(zone.name)}</td><td>${this._escape(zone.room_name || "—")}</td><td>${this._statusBadge(zone.mapping_status)}</td><td>${this._escape(zone.mapping_error || "Complete")}</td></tr>`).join("")}</tbody></table>` : ""}
      </section>
      ${this._renderSections("thermal")}
      <section class="card explanation"><h2>What happens next?</h2><p>After complete 15-minute recorder intervals exist, the integration publishes room temperature, actual heating/cooling duty and outdoor conditions. The website joins those observations to its room-owned Comfort schedule and learns one response model per room. Energy history by itself is not treated as a room-temperature model.</p></section>
    `;
  }

  _renderDiagnostics() {
    const values = this._data.diagnostics;
    const readiness = this._data.readiness;
    const rows = [
      ["Integration entry", `${this._data.entry.title} (${this._data.entry.state})`],
      ["Subscription", values.subscription_active ? "Active" : "Inactive or unavailable"],
      ["Tariff", values.tariff_status],
      ["Last tariff error", values.last_tariff_error],
      ["Last daily push", values.last_daily_push],
      ["Last daily push error", values.last_daily_push_error],
      ["Last planning push", readiness.last_plan_push],
      ["Plan status", readiness.plan_status],
      ["Plan model", readiness.plan_model_version],
      ["Last planning error", values.last_optimisation_error],
      ["Accepted electrical slots", readiness.actual_slots_accepted],
      ["Electrical history accepted until", readiness.actuals_accepted_until],
      ["Accepted thermal slots", values.last_thermal_slots_accepted],
      ["Thermal history accepted through", values.thermal_slots_accepted_until],
    ];
    return `<section class="card diagnostics">
      <h2>Configuration diagnostics</h2>
      <p class="description">These are the concrete states used by the readiness cards. Secrets and raw recorder rows are never shown here.</p>
      <dl>${rows.map(([label, value]) => `<div><dt>${this._escape(label)}</dt><dd>${this._escape(value ?? "—")}</dd></div>`).join("")}</dl>
      ${readiness.missing_inputs.length ? `<h3>Planner input gaps</h3><ul>${readiness.missing_inputs.map((item) => `<li>${this._escape(item)}</li>`).join("")}</ul>` : ""}
    </section>`;
  }

  _renderBody() {
    if (this._tab === "overview") return this._renderOverview();
    if (this._tab === "inputs") return this._renderSections("inputs");
    if (this._tab === "devices") return this._renderDevices();
    if (this._tab === "thermal") return this._renderThermal();
    if (this._tab === "storage") return this._renderSections("storage");
    return this._renderDiagnostics();
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
            <button type="button" class="secondary" data-action="refresh" ${this._loading ? "disabled" : ""}>${this._loading ? "Refreshing…" : "Refresh website roles"}</button>
            <button type="button" class="secondary" data-action="discover" ${this._loading ? "disabled" : ""}>Run automatic discovery</button>
            <button type="button" class="text" data-action="discard" ${this._dirty ? "" : "disabled"}>Discard</button>
            <button type="button" class="primary" data-action="save" ${this._configurationDirty && !this._saving && !this._savingDeviceKey ? "" : "disabled"}>${this._saving ? "Saving…" : "Save general changes"}</button>
          </div>
        </header>
        <nav class="tabs" aria-label="Configuration sections">${TABS.map(([id, label]) => `<button type="button" data-action="tab" data-tab="${id}" class="${this._tab === id ? "active" : ""}">${label}</button>`).join("")}</nav>
        <section class="content">
          ${this._error ? `<div class="alert error"><strong>Could not save or refresh</strong><span>${this._escape(this._error)}</span></div>` : ""}
          ${this._notice ? `<div class="alert notice"><span>${this._escape(this._notice)}</span></div>` : ""}
          ${this._data.portal.error ? `<div class="alert warning"><strong>Website role refresh failed</strong><span>${this._escape(this._data.portal.error)} The last saved website request is shown.</span></div>` : ""}
          ${this._renderBody()}
        </section>
        <footer><span>${this._dirty ? "Unsaved changes" : "All changes saved"}</span><span>Existing local controllers retain ownership.</span></footer>
        ${this._renderDatalist()}
      </main>`;
  }

  _styles() {
    return `<style>
      :host { display:block; min-height:100%; color:var(--primary-text-color); background:var(--primary-background-color); font-family:var(--paper-font-body1_-_font-family, system-ui, sans-serif); }
      * { box-sizing:border-box; }
      button, input, select { font:inherit; }
      button { cursor:pointer; }
      button:disabled { cursor:default; opacity:.48; }
      .shell { min-height:100vh; }
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
      .content { width:min(1280px, 100%); margin:0 auto; padding:24px 24px 96px; }
      .card { background:var(--card-background-color); border:1px solid var(--divider-color); border-radius:16px; padding:24px; margin-bottom:20px; box-shadow:var(--ha-card-box-shadow, none); }
      .card h2 { margin:0 0 8px; font-size:20px; }
      .description, .page-intro p, .explanation p { color:var(--secondary-text-color); margin:0 0 22px; line-height:1.55; }
      .page-intro { margin:4px 0 20px; }
      .page-intro h2 { margin:0 0 6px; }
      .safety-note { padding:15px 18px; margin-bottom:20px; border-radius:12px; color:var(--primary-text-color); background:color-mix(in srgb, var(--info-color, #039be5) 12%, var(--card-background-color)); border:1px solid color-mix(in srgb, var(--info-color, #039be5) 45%, transparent); }
      .summary-grid { display:grid; grid-template-columns:repeat(4, minmax(0,1fr)); gap:14px; margin-bottom:20px; }
      .summary-card { margin:0; min-height:170px; background:var(--card-background-color); border:1px solid var(--divider-color); border-top:4px solid var(--divider-color); border-radius:14px; padding:18px; }
      .summary-card.ready { border-top-color:var(--success-color, #43a047); }
      .summary-card.warning { border-top-color:var(--warning-color, #ff9800); }
      .summary-card.error { border-top-color:var(--error-color, #db4437); }
      .summary-card h3 { margin:0; font-size:16px; }
      .summary-card p { color:var(--secondary-text-color); line-height:1.45; margin:14px 0 0; }
      .summary-card ul { margin:12px 0 0; padding-left:18px; color:var(--secondary-text-color); font-size:13px; }
      .summary-top { display:flex; align-items:flex-start; justify-content:space-between; gap:12px; }
      .badge { display:inline-flex; align-items:center; padding:4px 9px; border-radius:999px; white-space:nowrap; font-size:12px; font-weight:700; color:var(--secondary-text-color); background:var(--secondary-background-color); }
      .badge.ready, .badge.synchronised, .badge.observations_published { color:var(--success-color, #2e7d32); background:color-mix(in srgb, var(--success-color, #43a047) 14%, transparent); }
      .badge.warning, .badge.not_configured, .badge.device_mappings_required, .badge.outdoor_sources_required, .badge.waiting_for_history { color:var(--warning-color, #ef6c00); background:color-mix(in srgb, var(--warning-color, #ff9800) 14%, transparent); }
      .badge.error, .badge.invalid { color:var(--error-color, #c62828); background:color-mix(in srgb, var(--error-color, #db4437) 12%, transparent); }
      .workflow { display:grid; grid-template-columns:repeat(4,1fr); gap:18px; }
      .workflow div { display:grid; grid-template-columns:32px 1fr; column-gap:10px; }
      .workflow span { width:30px; height:30px; display:grid; place-items:center; border-radius:50%; color:var(--primary-color); background:color-mix(in srgb, var(--primary-color) 12%, transparent); font-weight:700; grid-row:span 2; }
      .workflow small { color:var(--secondary-text-color); line-height:1.4; margin-top:4px; }
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
      .device-summary { display:flex; gap:12px; align-items:center; color:var(--secondary-text-color); white-space:nowrap; }
      .device-body { border-top:1px solid var(--divider-color); padding:22px; }
      .device-meta { display:flex; flex-wrap:wrap; gap:8px; margin-bottom:16px; }
      .device-meta span { padding:5px 9px; border-radius:8px; background:var(--secondary-background-color); color:var(--secondary-text-color); font-size:12px; }
      .device-actions { display:flex; justify-content:space-between; gap:10px; align-items:center; margin:14px 0 20px; }
      .device-action-group { display:flex; flex-wrap:wrap; gap:8px; }
      .device-save-row { display:flex; justify-content:flex-end; align-items:center; gap:16px; margin-top:24px; padding-top:18px; border-top:1px solid var(--divider-color); }
      .device-save-row span { color:var(--secondary-text-color); font-size:13px; }
      .inline-warning { padding:11px 13px; margin:10px 0; border-radius:9px; color:var(--warning-color); background:color-mix(in srgb, var(--warning-color) 10%, transparent); }
      .inline-error { display:flex; gap:10px; align-items:center; padding:11px 13px; margin:10px 0; border-radius:9px; color:var(--error-color); background:color-mix(in srgb, var(--error-color) 10%, transparent); }
      .inline-error span { flex:1; }
      .thermal-grid { display:grid; grid-template-columns:repeat(4,1fr); gap:12px; margin:22px 0; }
      .thermal-grid div { padding:16px; border-radius:12px; background:var(--secondary-background-color); display:flex; flex-direction:column; gap:5px; }
      .thermal-grid strong { font-size:21px; }
      .thermal-grid span { color:var(--secondary-text-color); font-size:12px; }
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
      @media (max-width:1000px) { .summary-grid, .workflow { grid-template-columns:repeat(2,1fr); } .topbar { flex-wrap:wrap; } .toolbar { width:100%; } .tabs { top:132px; } }
      @media (max-width:700px) { .topbar { padding:12px; position:relative; } .tabs { top:0; position:sticky; padding:8px 12px; } .content { padding:16px 12px 88px; } .toolbar { display:grid; grid-template-columns:1fr 1fr; } .field-grid, .summary-grid, .workflow, .thermal-grid { grid-template-columns:1fr; } .card { padding:18px; border-radius:13px; } .device-card { padding:0; } .device-summary > span:last-child { display:none; } .diagnostics dl div { grid-template-columns:1fr; gap:5px; } footer span:last-child { display:none; } }
    </style>`;
  }
}

if (!customElements.get("shs-energy-config-panel-v3")) {
  customElements.define("shs-energy-config-panel-v3", ShsEnergyConfigPanel);
}
