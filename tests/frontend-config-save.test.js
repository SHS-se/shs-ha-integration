const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { test } = require('node:test');
const vm = require('node:vm');
const frontendVersion = JSON.parse(readFileSync('custom_components/shs_energy/manifest.json', 'utf8')).version;
const frontendSource = readFileSync('custom_components/shs_energy/frontend/shs-energy-config-panel.js', 'utf8');
const loadPanel = (registry, version = frontendVersion) => {
  const context = vm.createContext({ URL, HTMLElement: class {}, customElements: registry });
  const moduleUrl = `https://ha.example/shs_energy_frontend/shs-energy-config-panel.js?v=${version}`;
  vm.runInContext(frontendSource.replaceAll('import.meta.url', JSON.stringify(moduleUrl)) + '\nglobalThis.Panel = ShsEnergyConfigPanel;', context);
  return context;
};
const context = loadPanel({ define() {}, get() {} });

test('an open session loads the new panel instead of reusing a previously registered class', () => {
  const oldPanel = class {};
  const elements = new Map([['shs-energy-config-panel-v3', oldPanel]]);
  const registry = {
    get: name => elements.get(name),
    define: (name, element) => { assert.equal(elements.has(name), false); elements.set(name, element); },
  };
  const previous = loadPanel(registry, '0.8.0-beta.29');
  const updated = loadPanel(registry);
  const name = `shs-energy-config-panel-${frontendVersion.replaceAll('.', '-')}`;
  assert.equal(elements.get(name), updated.Panel);
  assert.notEqual(elements.get(name), previous.Panel);
  assert.equal(elements.get('shs-energy-config-panel-v3'), oldPanel);
  loadPanel(registry); // Re-importing the same release does not redefine it.
  assert.equal(elements.get(name), updated.Panel);
});

test('saving retains disabled equipment settings and excludes metadata and mappings', async () => {
  const panel = Object.create(context.Panel.prototype);
  panel._data = { sections: [{ toggle: { key: 'ev_enabled' }, fields: [{ key: 'ev_charge_efficiency' }] }] };
  panel._draft = { ev_enabled: false, ev_charge_efficiency: 0.95, _migration_report: { removed: ['old'] }, device_control_mappings: { car: { power: 1000 } } };
  panel._savedDraft = { ...panel._draft, ev_enabled: true, ev_charge_efficiency: 0.92 };
  panel._entryId = 'entry';
  panel._render = () => {};
  let sent;
  panel._hass = { callWS: async (payload) => { sent = JSON.parse(JSON.stringify(payload)); } };
  await panel._save();
  assert.deepEqual(sent.configuration, { ev_enabled: false, ev_charge_efficiency: 0.95 });
  assert.equal(panel._draft.device_control_mappings.car.power, 1000);
  assert.equal(panel._error, '');
});

test('general saves omit unchanged resolved defaults', async () => {
  const panel = Object.create(context.Panel.prototype);
  panel._data = { sections: [{ fields: [{ key: 'ev_charge_efficiency' }, { key: 'ev_enabled' }] }] };
  panel._draft = { ev_charge_efficiency: 0.92, ev_enabled: false };
  panel._savedDraft = { ev_charge_efficiency: 0.92, ev_enabled: true };
  panel._entryId = 'entry';
  panel._render = () => {};
  let sent;
  panel._hass = { callWS: async (payload) => { sent = payload; } };
  await panel._save();
  assert.deepEqual(JSON.parse(JSON.stringify(sent.configuration)), { ev_enabled: false });
});

test('device save refreshes shared sources and preserves unrelated unsaved edits', async () => {
  const panel = Object.create(context.Panel.prototype);
  const mapping = { control_type: 'setpoint', temperature_entity_id: 'sensor.old', power: 1000 };
  panel._savedDraft = { device_control_mappings: { a: { ...mapping }, b: { ...mapping }, c: { ...mapping } } };
  panel._draft = JSON.parse(JSON.stringify(panel._savedDraft));
  panel._draft.device_control_mappings.a.temperature_entity_id = 'sensor.new';
  panel._draft.device_control_mappings.b.power = 2000;
  panel._entryId = 'entry';
  panel._deviceErrors = {};
  panel._render = () => {};
  const updated = { ...mapping, temperature_entity_id: 'sensor.new' };
  panel._data = { devices: [{ key: 'a', name: 'Heater' }] };
  const response = { mapping_status: 'ready',
    configuration: { device_control_mappings: { a: updated, b: updated, c: updated } } };
  panel._hass = { callWS: async () => response };
  await panel._saveDevice('a');
  assert.equal(panel._error, '');
  assert.deepEqual(Object.keys(panel._deviceErrors), []);
  for (const key of ['a', 'b', 'c']) {
    assert.equal(panel._draft.device_control_mappings[key].temperature_entity_id, 'sensor.new');
  }
  assert.equal(panel._draft.device_control_mappings.b.power, 2000);
  assert.equal(panel._savedDraft.device_control_mappings.b.power, 1000);
  assert.equal(panel._deviceDirty('a'), false);
  assert.equal(panel._deviceDirty('b'), true);
  assert.equal(panel._deviceDirty('c'), false);
});

const makePanel = () => {
  const panel = Object.create(context.Panel.prototype);
  panel._data = { sections: [], devices: [], configured_keys: [], labels: { ready: 'Ready', base_load: 'Excluded' }, attention: [] };
  panel._draft = { device_control_mappings: {} }; panel._savedDraft = JSON.parse(JSON.stringify(panel._draft));
  panel._added = new Set(); panel._expanded = new Set(); panel._deviceErrors = {}; panel._render = () => {};
  return panel;
};

test('clearing a populated source sends an explicit deletion', async () => {
  const panel = makePanel(); panel._entryId = 'entry';
  panel._data.sections = [{ fields: [{ key: 'weather' }] }];
  panel._savedDraft.weather = 'weather.home';
  let sent; panel._hass = { callWS: async message => { sent = message; } };
  await panel._save(); assert.equal(sent.configuration.weather, null);
});

test('refresh retains a pending edit and updates untouched sources', () => {
  const panel = makePanel(); panel._savedDraft = { a: 1, b: 2 }; panel._draft = { a: 3, b: 2 };
  panel._mergePanel({ configuration: { a: 4, b: 5 }, devices: [] });
  assert.equal(panel._draft.a, 3); assert.equal(panel._draft.b, 5); assert.equal(panel._savedDraft.a, 4);
});

test('laundry editor hides empty alternatives but retains populated fields and required dependencies', () => {
  const panel = makePanel();
  const html = panel._fields([{ key: 'temperature', label: 'Room temperature', kind: 'entity', required: true },
    { key: 'permit_entity_id', label: 'Heating permission', kind: 'entity' },
    { key: 'offset_entity_id', label: 'Offset', kind: 'entity' },
    { key: 'offset_minimum', label: 'Minimum offset', kind: 'number' }], { temperature: 'sensor.room' });
  assert.match(html, /sensor.room/); assert.doesNotMatch(html, /Heating permission|data-field-key="offset_entity_id"/);
  assert.match(html, /aria-label="Room temperature"/);
});

test('one permission row serves every device and always permits stopping', () => {
  const panel = makePanel(); panel._data.website_url = 'https://example.test/settings';
  for (const key of ['heater', 'ev', 'pool', '$battery']) {
    const html = panel._choices({ key, name: key, choice_label: 'Excluded', mode: 'controlling', permission: { enabled: true, reason: 'Excluded on website', verification_reason: 'Excluded on website' } });
    assert.doesNotMatch(html, /Include in the plan|not reviewed|>Website</); assert.match(html, /Device mode/);
    assert.match(html, /value="controlling" selected/);
    assert.doesNotMatch(html, /value="monitoring"|value="planning"/);
    assert.match(html, /value="control_verification"\s*>/);
  }
});

test('all attention is shown on Status and an informational item has no warning badge or fake fix', () => {
  const panel = makePanel(); panel._data.attention = [
    { title: 'Learning', severity: 'info', detail: 'Collecting', fix: { kind: 'none' } },
    { title: 'Plan failed', severity: 'error', detail: 'Failure', fix: { kind: 'panel', tab: 'energy' } }];
  const html = panel._renderAttention();
  assert.match(html, /Learning/); assert.match(html, /Plan failed/); assert.match(html, /data-tab="energy"/);
  assert.equal((html.match(/<button/g) || []).length, 1);
  assert.equal(panel._attentionForTab('status').length, 1); assert.equal(panel._attentionForTab('devices').length, 0);
});

test('Status renders counted warnings and affected fields above collapsed diagnostics even when ready', () => {
  const panel = makePanel();
  panel._tab = 'status';
  Object.assign(panel._data, {
    operation: { state: 'ready', label: 'Ready', reason: 'A validated plan is available' },
    diagnostics: {}, readiness: {}, portal: {},
    attention: [
      { title: 'Battery setup incomplete', severity: 'warning', detail: 'Choose the missing control entity.',
        items: ['Battery authority entity <missing>'], fix: { kind: 'panel', tab: 'devices' } },
      { title: 'Tariff answers needed', severity: 'warning', detail: 'Complete the tariff settings.',
        items: ['Electricity supplier'], fix: { kind: 'website', url: 'https://example.com/settings' } },
      { title: 'Learning', severity: 'info', detail: 'Collecting history', fix: { kind: 'none' } },
    ],
  });
  const html = panel._renderBody();
  assert.match(html, /A validated plan is available/);
  assert.equal((html.match(/class="attention-item warning"/g) || []).length, panel._attentionForTab('status').length);
  assert.match(html, /Choose the missing control entity\./);
  assert.match(html, /Battery authority entity &lt;missing&gt;/);
  assert.match(html, /data-tab="devices"/);
  assert.match(html, /Electricity supplier/);
  assert.match(html, /href="https:\/\/example.com\/settings"/);
  assert.match(html, /Collecting history/);
  assert.ok(html.indexOf('Battery setup incomplete') < html.indexOf('<details'));
  assert.doesNotMatch(html, /View status/);

  // Exercise the full panel, including its tab badge, rather than only the
  // attention helper: the original regression left that helper disconnected.
  panel._hass = {};
  panel._data.entry = { title: 'Test home', state: 'loaded' };
  panel._data.entities = [];
  panel.shadowRoot = { querySelectorAll: () => [], innerHTML: '' };
  context.Panel.prototype._render.call(panel);
  assert.match(panel.shadowRoot.innerHTML, /aria-label="2 items to fix">2<\/span>/);
  assert.equal((panel.shadowRoot.innerHTML.match(/class="attention-item warning"/g) || []).length, 2);

  panel._data.attention = [];
  assert.doesNotMatch(panel._renderBody(), /class="attention"|attention-item/);
});

test('system fields are saved with their device and excluded from general saves', async () => {
  const panel = makePanel(); panel._entryId = 'entry';
  panel._data.devices = [{ key: '$battery', name: 'Battery', system_fields: [{ key: 'battery_capacity_kwh' }] }];
  panel._data.sections = [{ fields: [{ key: 'battery_capacity_kwh' }, { key: 'grid_import_limit_w' }] }];
  panel._savedDraft.battery_capacity_kwh = 10; panel._draft.battery_capacity_kwh = 12;
  assert.equal(panel._configurationDirty, false); assert.equal(panel._deviceDirty('$battery'), true);
  let sent; panel._hass = { callWS: async m => { sent = m; return { mapping_status: 'ready' }; } };
  await panel._saveDevice('$battery');
  assert.equal(sent.configuration.battery_capacity_kwh, 12); assert.equal(sent.mapping, null);
  assert.equal(panel._deviceDirty('$battery'), false);
});

test('discovery changes sources only, retaining pending source and device edits', async () => {
  const panel = makePanel(); panel._entryId = 'entry';
  panel._data.sections = [{ tab: 'energy', fields: [{ key: 'weather' }, { key: 'grid' }] }];
  panel._savedDraft = { weather: 'weather.old', grid: ['sensor.old'], battery_capacity_kwh: 10, device_control_mappings: {} };
  panel._draft = { ...panel._savedDraft, weather: 'weather.pending', battery_capacity_kwh: 12 };
  panel._hass = { callWS: async () => ({ configuration: { weather: 'weather.suggested', grid: ['sensor.new'], battery_capacity_kwh: 18 } }) };
  await panel._discover();
  assert.equal(panel._draft.weather, 'weather.pending');
  assert.equal(panel._draft.battery_capacity_kwh, 12);
  assert.deepEqual(JSON.parse(JSON.stringify(panel._draft.grid)), ['sensor.new']);
});

test('status shows automatic recovery and delivery failures without claiming readiness', () => {
  const panel = Object.create(context.Panel.prototype);
  panel._hass = {};
  panel._data = { operation: { state: 'unavailable', label: 'Unavailable', reason: 'Waiting for a plan', recovering: true },
    diagnostics: { last_optimisation_error: 'Planner unavailable', last_runtime_error: 'offline' },
    readiness: {}, portal: {}, devices: [], attention: [], labels: {} };
  let html = panel._renderStatus();
  assert.match(html, /Requesting a fresh plan automatically/);
  assert.match(html, /Latest planning error: Planner unavailable/);
  assert.match(html, /Website status delivery failed: offline/);
  panel._data.operation = { state: 'ready', label: 'Ready', reason: 'A validated plan is available' };
  panel._refreshError = 'connection lost';
  html = panel._renderStatus();
  assert.match(html, /Current status is unconfirmed/);
  assert.match(html, /Retry status refresh/);
  assert.equal(panel._attentionForTab("status").length, 1);
  assert.doesNotMatch(html, />Ready<\/span>/);
});

const splitPanel = () => {
  const panel = Object.create(context.Panel.prototype);
  const volume = { key: 'pool_volume_m3', kind: 'number', label: 'Pool volume' };
  const temperature = { key: 'pool_water_temperature_entity', kind: 'entity', label: 'Pool water temperature' };
  const permission = { key: 'pool_permission_entity', kind: 'entity', label: 'Pool permission switch' };
  const mapping = { control_type: 'switch_schedule', actuator_entity_ids: ['switch.pool'] };
  panel._data = { devices: [{ key: 'pool', name: 'Pool pump', system: 'pool', category: 'pool_heating', included: true, planned: true,
    mapping_status: 'ready', permission: { enabled: false }, fields: [], system_fields: [permission, temperature, volume], planning_fields: [volume] },
    { key: 'excluded', name: 'Excluded microwave', included: false, planned: false, category: 'household', fields: [], permission: { enabled: false } }],
    sections: [], labels: { ready: 'Ready', base_load: 'Excluded', pool_heating: 'Pool' } };
  panel._draft = { pool_permission_entity: 'switch.permit', pool_volume_m3: 55, pool_water_temperature_entity: 'sensor.water', device_control_mappings: { pool: mapping } };
  panel._savedDraft = JSON.parse(JSON.stringify(panel._draft));
  panel._expanded = new Set(); panel._added = new Set(); panel._deviceErrors = {};
  panel._search = ''; panel._room = ''; panel._category = ''; panel._render = () => {};
  panel._entryId = 'entry';
  return panel;
};

test('Devices stacks Controls above Planning and excludes equipment until explicitly shown', () => {
  const panel = splitPanel();
  const html = panel._renderDevices();
  assert.ok(html.indexOf('id="controls-heading"') < html.indexOf('id="planning-heading"'));
  const [controls, planning] = html.split('<section aria-labelledby="planning-heading">');
  assert.match(controls, /<strong>Pool pump<\/strong>/);
  assert.doesNotMatch(controls, /<strong>Pool<\/strong>/);
  assert.match(controls, /Pool water temperature/);
  assert.doesNotMatch(controls, /Pool volume/);
  assert.match(planning, /Pool volume/);
  assert.doesNotMatch(planning, /Pool water temperature/);
  assert.match(planning, /<strong>Pool<\/strong>/);
  assert.doesNotMatch(planning, /Let SHS operate/);
  assert.doesNotMatch(html, /Excluded microwave/);
  assert.match(html, /aria-label="Show excluded devices"/);
  panel._showExcluded = true;
  assert.match(panel._renderDevices(), /Excluded microwave/);
  panel._showExcluded = false;
  assert.doesNotMatch(panel._renderDevices(), /Excluded microwave/);
});

test('saving Planning preserves unsaved Controls and sends only planning properties', async () => {
  const panel = splitPanel();
  panel._draft.pool_volume_m3 = 60;
  panel._draft.pool_water_temperature_entity = 'sensor.other';
  panel._draft.device_control_mappings.pool.actuator_entity_ids = ['switch.other'];
  let sent;
  panel._hass = { callWS: async payload => { sent = payload; return { mapping_status: 'ready' }; } };
  await panel._saveDevice('pool', 'planning');
  assert.deepEqual(JSON.parse(JSON.stringify(sent.configuration)), { pool_volume_m3: 60 });
  assert.deepEqual(JSON.parse(JSON.stringify(sent.mapping.actuator_entity_ids)), ['switch.pool']);
  assert.equal(panel._draft.device_control_mappings.pool.actuator_entity_ids[0], 'switch.other');
  assert.equal(panel._draft.pool_water_temperature_entity, 'sensor.other');
  assert.equal(panel._deviceDirty('pool', 'planning'), false);
  assert.equal(panel._deviceDirty('pool', 'controls'), true);
});

test('saving Controls and cancelling Planning retain edits in the other section', async () => {
  const panel = splitPanel();
  panel._draft.pool_volume_m3 = 60;
  panel._draft.pool_permission_entity = 'switch.other';
  let sent;
  panel._hass = { callWS: async payload => { sent = payload; return { mapping_status: 'ready' }; } };
  await panel._saveDevice('pool', 'controls');
  assert.deepEqual(JSON.parse(JSON.stringify(sent.configuration)), { pool_permission_entity: 'switch.other' });
  assert.equal(panel._deviceDirty('pool', 'planning'), true);
  panel._draft.pool_permission_entity = 'switch.third';
  panel._cancelDevice('pool', 'planning');
  assert.equal(panel._draft.pool_volume_m3, 55);
  assert.equal(panel._draft.pool_permission_entity, 'switch.third');
});

test('one pool water selector updates the heater mapping when Controls is saved', async () => {
  const panel = splitPanel();
  const device = panel._data.devices.find(d => d.key === 'pool');
  device.control_type = 'setpoint';
  for (const draft of [panel._draft, panel._savedDraft]) {
    draft.device_control_mappings.pool.control_type = 'setpoint';
    draft.device_control_mappings.pool.temperature_entity_id = 'sensor.water';
  }
  panel._draft.pool_water_temperature_entity = 'sensor.new_water';
  let sent;
  panel._hass = { callWS: async payload => { sent = payload; return { mapping_status: 'ready' }; } };
  await panel._saveDevice('pool', 'controls');
  assert.equal(sent.configuration.pool_water_temperature_entity, 'sensor.new_water');
  assert.equal(sent.mapping.temperature_entity_id, 'sensor.new_water');
  assert.equal(panel._draft.device_control_mappings.pool.temperature_entity_id, 'sensor.new_water');
  assert.equal(panel._deviceDirty('pool', 'controls'), false);
});

context.document = { hidden: false };
context.HTMLInputElement = class {};
context.HTMLSelectElement = class {};
const deferred = () => {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
};

for (const outcome of ['success', 'error']) {
  test(`opening renders local configuration before website refresh ${outcome}`, async () => {
    const panel = makePanel();
    const local = { ...panel._data, entry: { entry_id: 'entry' }, configuration: { volume: 55 } };
    panel._data = panel._draft = panel._savedDraft = undefined;
    const website = deferred();
    const started = deferred();
    let renders = 0;
    panel._render = () => { if (panel._data) renders++; };
    panel._updateAttentionUI = () => {};
    const requests = [];
    panel._hass = { callWS: message => {
      requests.push(message.refresh_roles);
      if (!message.refresh_roles) return Promise.resolve(local);
      assert.equal(panel._loading, false);
      assert.equal(panel._draft.volume, 55);
      assert.ok(renders > 0, 'local configuration renders while website request is pending');
      started.resolve();
      return website.promise;
    } };
    const opening = panel._load(true);
    await started.promise;
    panel._draft.volume = 60;
    const before = renders;
    if (outcome === 'success') website.resolve({ ...local, configuration: { volume: 70 } });
    else website.reject(new Error('Website unavailable'));
    await opening;
    assert.deepEqual(requests, [false, true]);
    assert.equal(panel._draft.volume, 60);
    assert.equal(renders, before, 'background completion must not rebuild an edited form');
    assert.equal(panel._refreshError, outcome === 'error' ? 'Website unavailable' : '');
    assert.equal(panel._polling, false);
  });
}

test('a failed load stays on screen instead of retrying behind the spinner on each hass update', async () => {
  const panel = makePanel(); delete panel._render;
  panel._data = panel._draft = panel._savedDraft = undefined;
  panel.isConnected = true;
  panel.shadowRoot = { innerHTML: '', activeElement: null, querySelectorAll: () => [] };
  let requests = 0;
  const hass = { callWS: async () => { requests++; throw new Error("'control_type'"); } };
  panel.hass = hass;
  await new Promise(resolve => setTimeout(resolve));
  panel.hass = hass; panel.hass = hass;
  assert.match(panel.shadowRoot.innerHTML, /Configuration could not be loaded/);
  assert.match(panel.shadowRoot.innerHTML, /control_type/);
  await new Promise(resolve => setTimeout(resolve));
  assert.equal(requests, 1);
});

test('opening waits for entry selection before refreshing website choices', async () => {
  const panel = makePanel(); panel._data = undefined;
  const requests = [];
  panel._hass = { callWS: async message => {
    requests.push(message.refresh_roles);
    return { requires_entry_selection: true, entries: [] };
  } };
  await panel._load(true);
  assert.deepEqual(requests, [false]);
});

test('explicit website refresh still requests fresh choices', async () => {
  const panel = makePanel();
  panel._data.entry = { entry_id: 'entry' };
  panel._data.configuration = panel._draft;
  const requests = [];
  panel._hass = { callWS: async message => { requests.push(message.refresh_roles); return panel._data; } };
  await panel._load(true);
  assert.deepEqual(requests, [true]);
});

test('typing records the draft before blur without rebuilding the input', () => {
  const panel = splitPanel();
  panel._data.sections = [{ fields: panel._data.devices[0].system_fields }];
  let renders = 0;
  panel._render = () => renders++;
  const input = Object.assign(new context.HTMLInputElement(), {
    dataset: { fieldKey: 'pool_volume_m3', scope: 'configuration', deviceKey: 'pool' },
    value: '62', type: 'number',
  });
  panel._onChange({ type: 'input', target: input });
  assert.equal(panel._draft.pool_volume_m3, 62);
  assert.equal(panel._deviceDirty('pool', 'planning'), true);
  assert.equal(renders, 0);
});

test('polling refreshes every tab and retains drafts and focused editors', async () => {
  const panel = splitPanel();
  let requests = 0;
  panel._hass = { callWS: async () => { requests++; return panel._data; } };
  for (const tab of ['energy', 'devices']) { panel._tab = tab; await panel._poll(); }
  panel._tab = 'schedule';
  panel._draft.pool_volume_m3 = 60;
  await panel._poll();
  panel._draft.pool_volume_m3 = 55;
  panel.shadowRoot = { activeElement: { tagName: 'INPUT' }, querySelector: () => null, querySelectorAll: () => [] };
  await panel._poll();
  assert.equal(requests, 4);
  assert.equal(panel._draft.pool_volume_m3, 55);
  panel.shadowRoot.activeElement = null;
  await panel._poll();
  assert.equal(requests, 5);
});

for (const outcome of ['success', 'error']) {
  test(`in-flight poll ${outcome} cannot rebuild a newly opened editor`, async () => {
    const panel = splitPanel(); panel._tab = 'status';
    const request = deferred();
    let renders = 0;
    panel._render = () => renders++;
    panel._hass = { callWS: () => request.promise };
    const polling = panel._poll();
    panel._tab = 'devices';
    panel.shadowRoot = { activeElement: { tagName: 'INPUT', value: 'unsaved typing' }, querySelector: () => null, querySelectorAll: () => [] };
    if (outcome === 'success') request.resolve({ configuration: { pool_volume_m3: 99 } });
    else request.reject(new Error('offline'));
    await polling;
    assert.equal(renders, 0);
    assert.equal(panel._draft.pool_volume_m3, 55);
    assert.equal(panel._polling, false);
  });
}

test('general save acknowledges only submitted values, retaining subsequent edits', async () => {
  const panel = makePanel(); panel._entryId = 'entry';
  panel._data.sections = [{ fields: [{ key: 'weather' }] }];
  panel._savedDraft.weather = 'weather.old'; panel._draft.weather = 'weather.sent';
  const request = deferred(); panel._hass = { callWS: () => request.promise };
  const saving = panel._save();
  panel._draft.weather = 'weather.newer';
  request.resolve({}); await saving;
  assert.equal(panel._savedDraft.weather, 'weather.sent');
  assert.equal(panel._draft.weather, 'weather.newer');
  assert.equal(panel._configurationDirty, true);
});

test('device save retains newer field and mapping edits while acknowledging the submitted snapshot', async () => {
  const panel = splitPanel();
  panel._draft.pool_permission_entity = 'switch.sent';
  panel._draft.device_control_mappings.pool.actuator_entity_ids = ['switch.sent'];
  const request = deferred(); panel._hass = { callWS: () => request.promise };
  const saving = panel._saveDevice('pool', 'controls');
  panel._draft.pool_permission_entity = 'switch.newer';
  panel._draft.device_control_mappings.pool.actuator_entity_ids = ['switch.newer'];
  request.resolve({ mapping_status: 'ready' }); await saving;
  assert.equal(panel._savedDraft.pool_permission_entity, 'switch.sent');
  assert.equal(panel._draft.pool_permission_entity, 'switch.newer');
  assert.equal(panel._savedDraft.device_control_mappings.pool.actuator_entity_ids[0], 'switch.sent');
  assert.equal(panel._draft.device_control_mappings.pool.actuator_entity_ids[0], 'switch.newer');
  assert.equal(panel._deviceDirty('pool', 'controls'), true);
});

test('a poll started before a save cannot restore the old saved configuration', async () => {
  const panel = splitPanel(); panel._tab = 'status';
  const request = deferred();
  panel._hass = { callWS: message => message.type.endsWith('/get') ? request.promise : Promise.resolve({ mapping_status: 'ready' }) };
  const polling = panel._poll();
  panel._draft.pool_volume_m3 = 60;
  await panel._saveDevice('pool', 'planning');
  request.resolve({ configuration: { pool_volume_m3: 55 } }); await polling;
  assert.equal(panel._draft.pool_volume_m3, 60);
  assert.equal(panel._savedDraft.pool_volume_m3, 60);
});

test('async completion does not replace a focused uncommitted entity search', () => {
  const panel = makePanel();
  let renders = 0;
  panel._render = () => renders++;
  panel.shadowRoot = { activeElement: { tagName: 'INPUT', value: 'sensor.part' }, querySelectorAll: () => [], querySelector: () => null };
  panel._renderBackground();
  assert.equal(renders, 0);
  assert.equal(panel.shadowRoot.activeElement.value, 'sensor.part');
});

for (const [name, controlType] of [['Pool pump', 'switch_schedule'], ['Pool heater', 'setpoint']]) {
  test(`${name} exposes an empty optional Power field in Controls and saves its sensor`, async () => {
    const panel = splitPanel();
    const device = panel._data.devices[0];
    Object.assign(device, { name, control_type: controlType, fields: [
      { key: 'actuator_entity_ids', label: 'Actuators', kind: 'entities', required: true },
      { key: 'power', label: 'Power', kind: 'power', help: 'Choose a power sensor, or enter reviewed watts directly.' },
    ] });
    for (const draft of [panel._draft, panel._savedDraft]) draft.device_control_mappings.pool.control_type = controlType;
    const [controls, planning] = panel._renderDevices().split('<section aria-labelledby="planning-heading">');
    assert.match(controls, /data-field-key="power" data-scope="mapping" data-device-key="pool" value=""/);
    assert.match(controls, /Power entity or watts/);
    assert.doesNotMatch(controls, /Add power/);
    assert.doesNotMatch(planning, /data-field-key="power"/);
    const input = Object.assign(new context.HTMLInputElement(), {
      dataset: { fieldKey: 'power', scope: 'mapping', deviceKey: 'pool' },
      value: 'sensor.pool_power', type: 'text',
    });
    panel._onChange({ type: 'input', target: input });
    assert.equal(panel._deviceDirty('pool', 'controls'), true);
    let sent;
    panel._hass = { callWS: async payload => { sent = payload; return { mapping_status: 'ready' }; } };
    await panel._saveDevice('pool', 'controls');
    assert.equal(sent.mapping.power, 'sensor.pool_power');
    assert.equal(panel._savedDraft.device_control_mappings.pool.power, 'sensor.pool_power');
    assert.equal(panel._deviceDirty('pool', 'controls'), false);
  });
}

for (const controlType of ['setpoint', 'switch_schedule', 'permit_inhibit']) {
  test(`${controlType} edits one actuator in the shared card layout`, async () => {
    const panel = splitPanel();
    const device = panel._data.devices[0];
    device.control_type = controlType;
    device.fields = [
      { key: 'actuator_entity_ids', label: 'Control entity', kind: 'entities', max_items: 1, required: true },
      { key: 'power', label: 'Power', kind: 'power' },
      { key: 'temperature_entity_id', label: 'Room temperature', kind: 'entity' },
    ];
    for (const draft of [panel._draft, panel._savedDraft]) draft.device_control_mappings.pool.control_type = controlType;
    const html = panel._renderDevice(device);
    assert.match(html, /data-field-key="actuator_entity_ids"[^>]*value="switch.pool"/);
    assert.doesNotMatch(html, /multi-editor|add-multi|remove-multi|measurements and controls/);
    assert.equal((html.match(/class="field-grid"/g) || []).length, 1);
    assert.equal((html.match(/<summary>Add a setting<\/summary>/g) || []).length, 1);
    assert.ok(html.indexOf('data-field-key="actuator_entity_ids"') < html.indexOf('data-field-key="power"'));
    const input = Object.assign(new context.HTMLInputElement(), {
      dataset: { fieldKey: 'actuator_entity_ids', scope: 'mapping', deviceKey: 'pool' },
      value: 'switch.combined', type: 'text',
    });
    panel._onChange({ type: 'input', target: input });
    let sent;
    panel._hass = { callWS: async payload => { sent = payload; return { mapping_status: 'ready' }; } };
    await panel._saveDevice('pool', 'controls');
    assert.deepEqual(Array.from(sent.mapping.actuator_entity_ids), ['switch.combined']);
    input.value = '';
    panel._onChange({ type: 'input', target: input });
    assert.equal(panel._draft.device_control_mappings.pool.actuator_entity_ids, undefined);
  });
}

test('battery quantities autocomplete sensor names and preserve sensor or numeric values', () => {
  const panel = Object.create(context.Panel.prototype);
  panel._draft = {};
  panel._data = { entities: [
    { entity_id: 'sensor.capacity', name: 'Sigen Plant Rated Energy Capacity', domain: 'sensor', unit: 'kWh' },
    { entity_id: 'sensor.charge', name: 'Sigen Plant ESS Rated Charging Power', domain: 'sensor', unit: 'kW' },
    { entity_id: 'sensor.floor', name: 'Sigen Plant Discharge Cut-Off SOC', domain: 'sensor', unit: '%' },
  ] };
  for (const [key, unit, scale, entity, literal, stored, list] of [
    ['battery_capacity_kwh', 'kWh', 1, 'sensor.capacity', '18.08', 18.08, 'shs-energy-list'],
    ['battery_charge_max_w', 'W', 1, 'sensor.charge', '8800', 8800, 'shs-power-list'],
    ['battery_min_soc', '%', 100, 'sensor.floor', '0.5', .005, 'shs-percent-list'],
  ]) {
    const field = { key, label: key, kind: 'quantity', unit, scale };
    panel._setField('configuration', key, entity, field);
    assert.equal(panel._draft[key], entity);
    assert.match(panel._renderField(field, entity), new RegExp(`list="${list}"`));
    assert.match(panel._renderField(field, entity), new RegExp(`value="${entity}"`));
    panel._setField('configuration', key, literal, field);
    assert.equal(panel._draft[key], stored);
    assert.match(panel._renderField(field, stored), new RegExp(`value="${literal}"`));
    panel._setField('configuration', key, '', field);
    assert.equal(key in panel._draft, false);
  }
  const lists = panel._renderDatalist();
  assert.match(lists, /value="sensor.capacity">Sigen Plant Rated Energy Capacity/);
  assert.match(lists, /value="sensor.charge">Sigen Plant ESS Rated Charging Power/);
  assert.match(lists, /value="sensor.floor">Sigen Plant Discharge Cut-Off SOC/);
});

test('sensor quantities display sensor units and literal quantities display input units', () => {
  const panel = Object.create(context.Panel.prototype);
  panel._data = { entities: [{ entity_id: 'sensor.rating', unit: 'kW' }] };
  const field = { key: 'battery_charge_max_w', label: 'Maximum charge power', kind: 'quantity', unit: 'W', help: 'Enter W' };
  assert.match(panel._renderField(field, 'sensor.rating'), /<span>kW<\/span>/);
  assert.doesNotMatch(panel._renderField(field, 'sensor.rating'), /Enter W/);
  assert.match(panel._renderField(field, 8800), /<span>W<\/span>/);
  assert.doesNotMatch(panel._renderField(field, 'sensor.missing'), /<span>W<\/span>/);
});

test('battery modes come from actual selector options and keep invalid saved values visible', () => {
  const panel = Object.create(context.Panel.prototype);
  panel._draft = { battery_mode_entity: 'select.ems' };
  panel._hass = { states: { 'select.ems': { attributes: { options: ['Standby', 'Maximum Self Consumption'] } } } };
  const field = { key: 'battery_mode_baseline', label: 'Baseline mode', kind: 'battery_mode' };
  const html = panel._renderField(field, 'Maximum Self Consumption');
  assert.match(html, /<select/);
  assert.match(html, /value="Maximum Self Consumption" selected/);
  assert.match(html, /value="Standby"/);
  assert.match(panel._renderField(field, 'bad mode'), /Unavailable option: bad mode/);
});

test('removing settings clears their values, hides them and sends null for save', () => {
  const panel = Object.create(context.Panel.prototype);
  const field = { key: 'battery_charge_efficiency', kind: 'number', label: 'Charge efficiency', unit: '%', scale: 100 };
  panel._data = { configured_keys: [field.key] };
  panel._draft = { [field.key]: .95 };
  panel._savedDraft = { ...panel._draft };
  panel._added = new Set([`configuration::${field.key}`]);
  panel._clearDeviceError = () => {};
  panel._render = () => {};
  assert.match(panel._fields([field], panel._draft), /data-action="remove-field"/);
  panel._removeField('configuration', field.key);
  assert.equal(panel._draft[field.key], null);
  assert.deepEqual(JSON.parse(JSON.stringify(panel._patch([field]))), { battery_charge_efficiency: null });
  assert.doesNotMatch(panel._fields([field], panel._draft), /data-field-key="battery_charge_efficiency"/);
  assert.match(panel._fields([field], panel._draft), /Add charge efficiency/);
  panel._draft.battery_mode_entity = 'select.ems';
  panel._draft.battery_mode_charge = 'Charge';
  panel._draft.battery_mode_baseline = 'Baseline';
  panel._removeField('configuration', 'battery_mode_entity');
  assert.equal(panel._draft.battery_mode_charge, null);
  assert.equal(panel._draft.battery_mode_baseline, null);
});


const poolFaultPanel = () => {
  const panel = makePanel();
  panel._tab = 'schedule';
  Object.assign(panel._data, {
    operation: { state: 'ready', label: 'Ready', reason: 'A validated plan is available' },
    diagnostics: {}, readiness: {}, portal: {},
    devices: [{ key: 'sensor.pool', name: 'Pool heater', system: 'pool', mode: 'control_verification',
      permission: { controller_id: 'pool' }, execution_status: { state: 'fault',
        reason: 'sensor.water is stale (last reported 2026-09-11T15:00:00+00:00; maximum age 120 seconds)',
        next_step: 'Check the sensor reporting interval.', retry_automatically: true, fix: { kind: 'entity', entity_id: 'sensor.water' },
        plan_id: 'plan-123', slot_start: '2026-09-11T15:00:00+00:00' } }],
    attention: [{ key: 'power', title: 'Running power needed', severity: 'warning', detail: 'Configure Power.',
      fix: { kind: 'devices', device_keys: ['sensor.pool'] } }],
  });
  return panel;
};

test('controller warnings share counts, destinations and visible evidence with integration warnings', () => {
  const panel = poolFaultPanel();
  const html = panel._renderStatus();
  assert.match(html, /badge warning">Needs attention/);
  assert.doesNotMatch(html, /badge ready">Ready/);
  for (const key of ['controller:pool', 'power']) {
    assert.ok(html.indexOf(`data-attention-key="${key}"`) < html.indexOf('<details'));
  }
  assert.match(html, /sensor.water is stale/);
  assert.match(html, /What to do:/);
  assert.match(html, /Check the sensor reporting interval/);
  assert.match(html, /Verification retries automatically/);
  assert.match(html, /data-action="inspect-entity" data-entity-id="sensor.water"/);
  assert.match(html, /data-action="edit-device" data-device-key="sensor.pool"/);
  assert.match(html, /Download verification log/);
  assert.match(html, /Plan plan-123/);
  assert.equal(panel._attentionForTab('status').length, 2);
});

test('View status focuses the named warning instead of leaving it hidden in diagnostics', () => {
  const panel = poolFaultPanel();
  let focused, scrolled, renderedTab;
  panel._render = () => { renderedTab = panel._tab; };
  panel.shadowRoot = { querySelectorAll: () => ['power', 'controller:pool'].map(key => ({
    dataset: { attentionKey: key }, focus: () => { focused = key; }, scrollIntoView: () => { scrolled = key; },
  })) };
  panel._onClick({ target: { closest: () => ({ dataset: { action: 'view-status', issueKey: 'controller:pool' } }) } });
  assert.equal(renderedTab, 'status');
  assert.equal(focused, 'controller:pool');
  assert.equal(scrolled, 'controller:pool');
  focused = scrolled = null;
  panel._openStatus('resolved-issue');
  assert.equal(focused, null, 'a removed issue must not focus an unrelated warning');
  assert.match(panel._notice, /no longer reported/);
});

test('a recovered device loses its warning, link and badge without losing other warnings', () => {
  const panel = poolFaultPanel();
  const devices = JSON.parse(JSON.stringify(panel._data.devices));
  devices[0].execution_status = { state: 'verified', reason: 'Commands logged' };
  panel._mergePanel({ ...panel._data, devices });
  assert.equal(panel._attentionForTab('status').length, 1);
  assert.doesNotMatch(panel._renderAttention(), /sensor.water is stale/);
  panel._data.attention = [];
  assert.equal(panel._attentionForTab('status').length, 0);
  assert.match(panel._renderStatus(), /badge ready">Ready/);
});

test('unsupported, overridden, limited and pending handovers retain visible reasons and evidence', () => {
  const panel = poolFaultPanel();
  panel._data.attention = [];
  for (const state of ['fault', 'unsupported', 'overridden', 'limited', 'verified']) {
    panel._data.devices[0].execution_status = { state, reason: `Specific ${state} reason`, handover_pending: state === 'verified' };
    const html = panel._renderAttention();
    assert.match(html, new RegExp(`Specific ${state} reason`));
    assert.match(html, /Download verification log/);
    assert.equal(panel._attentionForTab('status').length, 1);
  }
  panel._data.devices[0].mode = 'controlling';
  assert.match(panel._renderAttention(), /Download diagnostics/);
  assert.doesNotMatch(panel._renderAttention(), /Verification retries automatically/);
});

test('every supported fix names a real action; retired tabs create no dead-end button', () => {
  const panel = poolFaultPanel();
  const render = fix => panel._attentionActions({ fix });
  assert.match(render({ kind: 'device', system: 'pool' }), /edit-device.*sensor.pool/);
  assert.match(render({ kind: 'devices', device_keys: ['sensor.pool'] }), /Edit Pool heater setup/);
  assert.match(render({ kind: 'panel', tabs: ['energy', 'devices'] }), /data-tab="energy".*data-tab="devices"/);
  assert.match(render({ kind: 'website', url: 'https://example.test/settings' }), /href="https:\/\/example.test\/settings"/);
  assert.match(render({ kind: 'diagnostics' }), /data-action="download"/);
  assert.match(render({ kind: 'refresh' }), /data-action="retry"/);
  assert.equal(render({ kind: 'panel', tab: 'diagnostics' }), '');
  assert.equal(render({ kind: 'none' }), '');
});


test('website issues never highlight local price or solar fields or offer local navigation', () => {
  const panel = splitPanel();
  panel._data.attention = [{ key: 'prices', severity: 'warning', detail: 'Supplier and price area are required',
    fix: { kind: 'website', url: 'https://example.test/portal/settings/energy-tariffs' } }];
  panel._data.sections = [{ id: 'prices_forecasts', tab: 'energy', fields: [{ key: 'pv_forecast_latitude', label: 'Solar forecast latitude', kind: 'number' }] }];
  const html = panel._renderAttention();
  assert.match(html, /energy-tariffs/);
  assert.doesNotMatch(html, /Open Energy|Open Devices|edit-field/);
  assert.equal(panel._fieldProblems('pv_forecast_latitude').length, 0);
});

test('a field issue shows its hidden optional input, exact message and direct fix destination', () => {
  const panel = splitPanel();
  panel._data.devices[0].fields = [{ key: 'power', label: 'Power', kind: 'power' }];
  panel._draft.device_control_mappings.pool.power = null;
  panel._data.attention = [{ key: 'power', severity: 'warning', detail: 'Power needed',
    fix: { kind: 'fields', fields: [{ key: 'power', scope: 'mapping', device_key: 'pool', message: 'Set running watts or select a reliable power sensor.' }] } }];
  const html = panel._renderDevices();
  assert.match(html, /data-field-token="mapping:pool:power" class="field field-problem/);
  assert.match(html, /aria-invalid="true"/);
  assert.match(html, /Set running watts or select a reliable power sensor/);
  assert.equal(panel._fieldProblems('pool_volume_m3', 'configuration', 'pool').length, 0);
  assert.match(panel._renderAttention(), /data-action="edit-field" data-field-token="mapping:pool:power"/);
  let focused = false, scrolled = false;
  panel.shadowRoot = { querySelectorAll: () => [{ dataset: { fieldToken: 'mapping:pool:power' },
    scrollIntoView: () => { scrolled = true; }, querySelector: () => ({ focus: () => { focused = true; } }) }] };
  panel._openField('mapping:pool:power');
  assert.equal(panel._tab, 'devices');
  assert.ok(panel._expanded.has('controls:pool'));
  assert.ok(panel._added.has('mapping:pool:power'));
  assert.ok(focused && scrolled);
});

test('entity faults highlight only fields that reference that exact source', () => {
  const panel = splitPanel();
  panel._data.attention = [{ key: 'source', severity: 'warning', detail: 'sensor.water is stale', fix: { kind: 'entity', entity_id: 'sensor.water' } }];
  assert.equal(panel._fieldProblems('pool_water_temperature_entity', 'configuration', 'pool').length, 1);
  assert.equal(panel._fieldProblems('pool_volume_m3', 'configuration', 'pool').length, 0);
  panel._data.attention = [];
  assert.equal(panel._fieldProblems('pool_water_temperature_entity', 'configuration', 'pool').length, 0);
});

test('subscription recovery clears the badge during an edit without replacing the input or draft', async () => {
  const panel = splitPanel();
  panel._tab = 'devices'; panel._draft.pool_volume_m3 = 61;
  panel._data.attention = [{ key: 'subscription', severity: 'warning', detail: 'Inactive' }];
  let renders = 0;
  panel._render = () => renders++;
  const input = { tagName: 'INPUT', value: 'sensor.partially_typed' };
  const badge = { innerHTML: '', classList: { toggle() {} } };
  panel.shadowRoot = { activeElement: input, querySelector: () => badge, querySelectorAll: () => [] };
  panel._hass = { callWS: async () => ({ ...panel._data, attention: [], configuration: { pool_volume_m3: 80 } }) };
  await panel._poll();
  assert.equal(panel._attentionForTab('status').length, 0);
  assert.equal(badge.innerHTML, 'Status');
  assert.equal(panel.shadowRoot.activeElement, input);
  assert.equal(input.value, 'sensor.partially_typed');
  assert.equal(panel._draft.pool_volume_m3, 61);
  assert.equal(renders, 0);
});

test('controller diagnostics download saves the gzip file Home Assistant built without parsing it', async () => {
  const { gzipSync } = require('node:zlib');
  const file = gzipSync(Buffer.from(JSON.stringify({ schema_version: 4, current: { devices: [] } })));
  const summary = { devices: 2, runtime_evaluations: 0, verification_checks: 9, observation_samples: 1 };
  let blob;
  let clicked = false;
  let revoked;
  let requested;
  const anchor = { click() { clicked = true; } };
  const downloadContext = loadPanel({ define() {}, get() {} });
  Object.assign(downloadContext, {
    URL: { createObjectURL(value) { blob = value; return 'blob:verification'; }, revokeObjectURL(value) { revoked = value; } },
    document: { createElement(tag) { assert.equal(tag, 'a'); return anchor; } },
  });
  const panel = Object.create(downloadContext.Panel.prototype);
  panel._entryId = 'entry/1';
  panel._render = () => {};
  panel._hass = {
    callWS: async () => { throw new Error('the download does not use the websocket'); },
    fetchWithAuth: async path => {
      requested = path;
      return new Response(file, { headers: { 'Content-Type': 'application/gzip', 'X-SHS-Diagnostics-Summary': JSON.stringify(summary) } });
    },
  };
  await panel._downloadVerification();
  assert.equal(panel._error, undefined);
  assert.equal(requested, '/api/shs_energy/controller_diagnostics/entry%2F1');
  assert.equal(anchor.download, 'shs-controller-diagnostics.json.gz');
  assert.match(panel._notice, /2 devices.*0 evaluations.*9 verification checks.*1 observation samples this session/);
  assert.equal(blob.type, 'application/gzip');
  assert.deepEqual(Buffer.from(await blob.arrayBuffer()), file);
  assert.equal(clicked, true);
  assert.equal(revoked, 'blob:verification');

  // A proxy that drops the summary header does not turn a saved file into an error.
  panel._hass.fetchWithAuth = async () => new Response(file, { headers: { 'Content-Type': 'application/gzip' } });
  await panel._downloadVerification();
  assert.equal(panel._error, undefined);
  assert.equal(panel._notice, 'Downloaded controller diagnostics.');

  panel._notice = '';
  clicked = false;
  panel._hass.fetchWithAuth = async () => new Response('The integration is not loaded', { status: 404 });
  await panel._downloadVerification();
  assert.equal(panel._error, 'The integration is not loaded');
  assert.equal(panel._notice, '');
  assert.equal(clicked, false);
});


test('live sensor faults explain automatic recovery without promising retries for permanent faults', () => {
  const panel = poolFaultPanel();
  panel._data.devices[0].mode = 'controlling';
  assert.match(panel._renderStatus(), /Control retries automatically after the problem is corrected/);
  panel._data.devices[0].execution_status.retry_automatically = false;
  assert.doesNotMatch(panel._renderStatus(), /retries automatically/);
});

test('schedule uses the same system instruction for its colour and detail', () => {
  const panel = makePanel();
  panel._data.timeline = { capabilities: { pool: true, battery: true, ev: true } };
  const pool = { key: 'pool-heater', system: 'pool' };
  const slot = { pool_w: 1952, commands: { 'pool-heater': { type: 'unavailable', reason: 'No executable planning model for this device' } } };
  assert.equal(panel._scheduleCommand(pool, slot).text, 'Heat · 1952 W planned');
  assert.equal(panel._scheduleCommand(pool, slot).active, true);
  assert.equal(panel._scheduleCommand(pool, { ...slot, pool_w: 0 }).active, false);
  assert.equal(panel._scheduleCommand({ key: 'pool-heater' }, slot).active, false);
  assert.equal(panel._scheduleCommand({ system: 'battery' }, { battery_command: { schema_version: 2, operation: 'supply_house', discharge_limit_w: 1000 } }).active, true);
  assert.equal(panel._scheduleCommand({ system: 'ev' }, { ev_target_current_a: 0 }).active, false);
  assert.equal(panel._scheduleCommand({ key: 'variable' }, { commands: { variable: { type: 'variable_power', value: 0, unit: 'W' } } }).active, false);
  panel._data.timeline.capabilities.pool = false;
  assert.equal(panel._scheduleCommand(pool, slot).active, false);
});

test('a pool pump runs with the pool: no selector of its own, pool schedule, one attention item', () => {
  const panel = makePanel();
  panel._data.timeline = { capabilities: { pool: true } };
  const fault = { state: 'fault', reason: 'Water temperature is stale', retry_automatically: true };
  const heater = { key: 'sensor.pool_heater_energy', name: 'Pool heater', system: 'pool', included: true, planned: true,
    mode: 'control_verification', execution_status: fault, permission: { enabled: false, reason: null, controller_id: 'pool' } };
  const pump = { key: 'sensor.pool_pump_energy', name: 'Pool pump', system_member: 'pool', included: true, planned: true,
    mode: 'control_verification', execution_status: fault, permission: { enabled: false, reason: null, controller_id: 'pool' } };
  panel._data.devices = [heater, pump];
  const choices = panel._choices(pump);
  assert.doesNotMatch(choices, /<select/);
  assert.match(choices, /Verification/);
  assert.match(choices, /Runs with Pool heater\. Choose Verification or Controlling there\./);
  assert.match(panel._choices(heater), /<select/);
  assert.equal(panel._choices(pump, 'controls'), '<div class="choices"></div>');
  // Its own command was never executable; the pool's request is what it follows.
  const slot = { pool_w: 2072, commands: { 'sensor.pool_pump_energy': { type: 'unavailable', reason: 'No executable planning model for this device' } } };
  const running = panel._scheduleCommand(pump, slot);
  assert.equal(running.text, 'Runs with pool heating');
  assert.equal(running.active, true);
  assert.equal(running.action, 'heating');
  assert.equal(panel._scheduleCommand(pump, { ...slot, pool_w: 0 }).text, 'No heating requested');
  assert.equal(panel._scheduleCommand(pump, { pool_w: 0, execution_owners: ['$pool'], execution: { pool_w: 2072 } }).text, 'Runs with pool heating');
  panel._data.timeline.capabilities.pool = false;
  assert.equal(panel._scheduleCommand(pump, slot).text, 'No instruction');
  const items = panel._attention().filter(item => item.key === 'controller:pool');
  assert.equal(items.length, 1);
  assert.match(items[0].title, /^Pool heater: /);
});

test('schedule keeps a compact plan ID without duplicated plan details', () => {
  const panel = makePanel();
  panel._data.operation = {
    plan_id: '88eaa262-2201-431f-9c5e-3fe83c5b16d1', state: 'ready', label: 'Ready', reason: 'A validated plan is available',
    now: '2026-09-14T10:15:00Z', issued_at: '2026-09-14T06:33:08Z',
    binding_until: '2026-09-14T22:00:00Z', valid_until: '2026-09-17T06:30:00Z',
  };
  panel._data.portal = {};
  panel._data.timeline = { capabilities: { pool: true }, slots: [{
    start: '2026-09-14T10:15:00Z', binding: false, pool_w: 1952,
    commands: { pool: { type: 'unavailable', reason: 'No executable planning model for this device' } },
  }] };
  panel._sortedDevices = () => [{ key: 'pool', name: 'Pool heater', system: 'pool', included: true, planned: true }];
  panel._choices = () => ''; panel._deviceFieldButtons = () => '';
  panel._selectedSlot = 0;
  const html = panel._renderSchedule();
  assert.match(html, /title="88eaa262-2201-431f-9c5e-3fe83c5b16d1">88eaa262<\/code>/);
  assert.doesNotMatch(html, /Plan details|Full plan ID|Published prices until|Plan valid until|every 15 minutes/);
  assert.match(html, /class="slot running advisory selected"/);
  assert.match(html, /Pool heater: Plan: Heat · 1952 W planned/);
  assert.doesNotMatch(html, /Advice only|future advice|Instructions until/);
});


test('battery schedule describes source permission and intent instead of forecast watts', () => {
  const panel = makePanel();
  panel._data.timeline = { capabilities: { battery: true } };
  const device = { system: 'battery' };
  const command = { schema_version: 2, operation: 'solar_charge', charge_limit_w: 8800, discharge_limit_w: 0 };
  const slot = { battery_charge_w: 523.94, battery_discharge_w: 0, battery_command: command };
  assert.equal(panel._scheduleCommand(device, slot).text, 'Capture surplus · preserve');
  command.operation = 'grid_charge';
  command.charge_limit_w = 523.94;
  assert.equal(panel._scheduleCommand(device, slot).text, 'Charge up to 524 W · grid allowed');
  command.operation = 'supply_house';
  command.discharge_limit_w = 2712;
  assert.equal(panel._scheduleCommand(device, slot).text, 'Supply house up to 2712 W');
  command.operation = 'hold';
  assert.equal(panel._scheduleCommand(device, slot).active, false);
  delete slot.battery_command;
  assert.equal(panel._scheduleCommand(device, slot).text, 'No supported instruction');
});

test('schema 3 battery slots colour by what the pack is asked to do', () => {
  const panel = makePanel();
  panel._data.timeline = { capabilities: { battery: true } };
  const device = { system: 'battery' };
  const command = { schema_version: 3, operation: 'hold', charge_limit_w: 8800, discharge_limit_w: 0 };
  const slot = { battery_charge_w: 0, battery_discharge_w: 0, battery_command: command };
  // Holding and forgoing the surplus both leave the pack alone: idle colour.
  assert.equal(panel._scheduleCommand(device, slot).action, 'idle');
  assert.equal(panel._scheduleCommand(device, slot).text, 'Preserve charge · capture surplus');
  command.operation = 'idle';
  command.charge_limit_w = 0;
  assert.equal(panel._scheduleCommand(device, slot).action, 'idle');
  assert.equal(panel._scheduleCommand(device, slot).text, 'Preserve charge · export surplus');
  for (const [operation, action] of [['solar_charge', 'charging'], ['grid_charge', 'charging'],
                                     ['supply_house', 'discharging'], ['export', 'discharging']]) {
    command.operation = operation;
    command.charge_limit_w = 8800;
    command.discharge_limit_w = 9600;
    assert.equal(panel._scheduleCommand(device, slot).action, action, operation);
    assert.equal(panel._scheduleCommand(device, slot).active, true, operation);
  }
  // A schema-2 hold kept the pack inert, and says so.
  assert.equal(panel._scheduleCommand(device, { battery_command: { ...command, schema_version: 2, operation: 'hold' } }).text,
    'Preserve battery');
  assert.equal(panel._scheduleCommand(device, { battery_command: { ...command, schema_version: 4 } }).text,
    'No supported instruction');
});

const scheduleFilterPanel = () => {
  const panel = makePanel();
  panel._data.operation = { state: 'ready', label: 'Ready', reason: 'Plan available', now: '2026-09-14T10:15:00Z' };
  panel._data.portal = {};
  panel._data.devices = [
    { key: 'a', name: 'Living heater', room_name: 'Living room', category: 'heating', mode: 'controlling', included: true, planned: true },
    { key: 'b', name: 'Bedroom heater', room_name: 'Bedroom', category: 'heating', mode: 'control_verification', included: true, planned: true },
    { key: 'c', name: 'Test charger', category: 'ev_charging', mode: 'control_verification', included: true, planned: true },
    { key: 'd', name: 'Observed meter', category: 'household', mode: 'monitoring', included: false, planned: false },
  ];
  panel._data.timeline = { slots: [{ start: '2026-09-14T10:15:00Z', duration_hours: .25,
    load_w: 10800, binding: true, commands: {} }] };
  panel._choices = () => ''; panel._deviceFieldButtons = () => '';
  panel._selectedSlot = 0;
  return panel;
};

test('first quarter is selected by default and its heading includes prices and expected demand', () => {
  const panel = scheduleFilterPanel();
  const slot = panel._data.timeline.slots[0];
  panel._selectedSlot = null;
  Object.assign(slot, { shadow_import_sek_per_kwh: 1.23456, shadow_export_sek_per_kwh: -0.004 });
  let html = panel._renderSchedule();
  assert.match(html, /<h3>[^<]* · Published prices: Buy 1\.23 SEK\/kWh, Sell 0\.00 SEK\/kWh · Expected house demand: 10\.80 kW<\/h3>/);
  assert.match(html, /class="slot [^"]*selected"[^>]*aria-pressed="true"/);
  assert.match(html, /Living heater: Plan: No instruction/);
  Object.assign(slot, { binding: false, shadow_export_sek_per_kwh: null });
  assert.match(panel._renderSchedule(), /<h3>[^<]* · Estimated prices: Buy 1\.23 SEK\/kWh · Expected house demand: 10\.80 kW<\/h3>/);
  delete slot.shadow_import_sek_per_kwh;
  assert.match(panel._renderSchedule(), /<h3>[^<]* · Estimated prices · Expected house demand: 10\.80 kW<\/h3>/);
});

test('expected house demand uses the plan total instead of base load or partial-slot energy', () => {
  const panel = scheduleFilterPanel();
  Object.assign(panel._data.timeline.slots[0], {
    duration_hours: 0.12280722222222222,
    base_w: 1101.06,
    load_w: 1133.32,
    device_loads_w: { 'sensor.hot_water_energy': 32.26 },
  });
  const html = panel._renderSchedule();
  assert.match(html, /Expected house demand: 1\.13 kW/);
  assert.doesNotMatch(html, /Expected house demand: (?:1\.10 kW|0\.1kWh)/);
});

test('schedule combines search, room, category and mode across timeline, details and cards', () => {
  const panel = scheduleFilterPanel();
  panel._scheduleSearch = ' HEATER ';
  panel._scheduleRoom = 'Living room';
  panel._scheduleCategory = 'heating';
  panel._scheduleMode = 'controlling';
  const html = panel._renderSchedule();
  assert.match(html, /<strong>Living heater<\/strong>/);
  assert.match(html, /<li>Living heater:/);
  assert.match(html, /<h2>Living heater<\/h2>/);
  assert.doesNotMatch(html, /Bedroom heater|Test charger|Observed meter/);
  assert.match(html, /data-mode="controlling" aria-pressed="true"/);
  assert.match(html, /data-filter="scheduleRoom"/);
  assert.match(html, /data-filter="scheduleCategory"/);
  assert.match(html, /Clear filters/);
});

test('Schedule contains only Planned devices and the two execution modes', () => {
  const panel = scheduleFilterPanel();
  const before = JSON.stringify(panel._data.devices);
  let html = panel._renderSchedule();
  assert.doesNotMatch(html, /<h2>Observed meter<\/h2>|data-mode="monitoring"|data-mode="planning"/);
  for (const [mode, name] of [['control_verification', 'Test charger'], ['controlling', 'Living heater']]) {
    panel._onClick({ target: { closest: () => ({ dataset: { action: 'schedule-mode', mode } }) } });
    assert.match(panel._renderSchedule(), new RegExp(`<h2>${name}</h2>`));
  }
  panel._scheduleSearch = 'not found';
  assert.match(panel._renderSchedule(), /No devices match these filters/);
  panel._onClick({ target: { closest: () => ({ dataset: { action: 'clear-schedule-filters' } }) } });
  html = panel._renderSchedule();
  assert.match(html, /data-mode="" aria-pressed="true"/);
  assert.doesNotMatch(html, /<h2>Observed meter<\/h2>/);
  assert.equal(JSON.stringify(panel._data.devices), before);
});

test('shared device filters keep Schedule and Devices selections independent', () => {
  const panel = scheduleFilterPanel();
  panel._search = 'Bedroom';
  panel._scheduleMode = 'controlling';
  assert.equal(panel._filterDevices(panel._data.devices)[0].key, 'b');
  assert.equal(panel._filterDevices(panel._data.devices, true)[0].key, 'a');
  panel._scheduleRoom = 'No room'; panel._scheduleMode = '';
  assert.equal(panel._filterDevices(panel._data.devices, true).length, 2);
  assert.match(panel._renderDeviceFilters(panel._data.devices), /data-filter="search" value="Bedroom"/);
  assert.match(panel._renderDeviceFilters(panel._data.devices, true), /value="No room" selected/);
});

test('selected details append only controller decisions that differ from the plan', () => {
  const panel = scheduleFilterPanel();
  panel._data.timeline.capabilities = { battery: true, pool: true, ev: true };
  const current = { start: '2026-09-14T10:15:00Z', duration_hours: .25 };
  const batterySlot = { ...current, battery_command: { schema_version: 3, operation: 'supply_house',
    charge_limit_w: 8800, discharge_limit_w: 9600, allow_grid_charge: false, allow_battery_export: false } };
  const battery = { system: 'battery', mode: 'controlling', battery_runtime: { decision: {
    kind: 'battery', operation: 'supply_house', charge_limit_w: 8800, discharge_limit_w: 2700 } } };
  assert.match(panel._scheduleCommandDetail(battery, batterySlot), /Controller: Discharge limit 2700 W/);
  battery.battery_runtime.decision.discharge_limit_w = 9600;
  assert.equal(panel._scheduleCommandDetail(battery, batterySlot), '');

  const pool = { system: 'pool', mode: 'control_verification', execution_status: {
    slot_start: current.start, decision: { kind: 'pool', heating: false, water_temperature_c: 32 } } };
  assert.match(panel._scheduleCommandDetail(pool, { ...current, pool_w: 1952 }), /Controller test: Heating off at 32 °C/);

  const ev = { system: 'ev', mode: 'controlling', execution_status: {
    slot_start: current.start, decision: { kind: 'ev', charging: false, current_a: 0, reason: 'vehicle disconnected' } } };
  assert.match(panel._scheduleCommandDetail(ev, { ...current, ev_target_current_a: 10 }), /Controller: Charging off \(vehicle disconnected\)/);

  const heater = { key: 'heater', mode: 'controlling', execution_status: {
    slot_start: current.start, decision: { kind: 'setpoint', target_c: [21.5] } } };
  assert.match(panel._scheduleCommandDetail(heater, { ...current, commands: {
    heater: { type: 'setpoint', target_c: 21.4 } } }), /Controller: Hold 21.5 °C/);
  assert.equal(panel._scheduleCommandDetail(heater, { ...current, start: '2026-09-14T10:30:00Z', commands: {
    heater: { type: 'setpoint', target_c: 21.4 } } }), '');
});

test('schedule colours follow requested actions independently of device mode and forecast power', () => {
  const panel = makePanel();
  panel._data.timeline = { capabilities: { battery: true, pool: true, ev: true } };
  for (const mode of ['monitoring', 'planning', 'control_verification', 'controlling']) {
    for (const [operation, action] of [['solar_charge', 'charging'], ['grid_charge', 'charging'],
      ['supply_house', 'discharging'], ['export', 'discharging'], ['self_consumption', 'general'], ['hold', 'idle']]) {
      assert.equal(panel._scheduleCommand({ system: 'battery', mode }, {
        battery_charge_w: 0, battery_discharge_w: 0,
        battery_command: { schema_version: 2, operation, charge_limit_w: 8800, discharge_limit_w: 9600 },
      }).action, action);
    }
  }
  assert.equal(panel._scheduleCommand({ system: 'pool' }, { pool_w: 1952 }).action, 'heating');
  assert.equal(panel._scheduleCommand({ system: 'ev' }, { ev_target_current_a: 10 }).action, 'charging');
  const slot = { commands: { device: { type: 'switch_schedule', on_seconds: 900 } } };
  for (const [category, action] of [['heating', 'heating'], ['cooling', 'cooling'], ['pool_pump', 'general']]) {
    assert.equal(panel._scheduleCommand({ key: 'device', category }, slot).action, action);
  }
  slot.commands.device = { type: 'permit_inhibit', permitted: true };
  assert.equal(panel._scheduleCommand({ key: 'device', category: 'hot_water' }, slot).action, 'heating');
  slot.commands.device.permitted = false;
  assert.equal(panel._scheduleCommand({ key: 'device', category: 'hot_water' }, slot).action, 'idle');
  slot.commands.device = { type: 'unavailable', reason: 'No supported instruction' };
  assert.equal(panel._scheduleCommand({ key: 'device', category: 'heating' }, slot).action, 'idle');
});

test('schedule action legend shares palette colours with slots and retains price hatching', () => {
  const panel = scheduleFilterPanel();
  panel._data.timeline.slots[0].commands.a = { type: 'switch_schedule', on_seconds: 900 };
  panel._data.timeline.slots[0].binding = false;
  const html = panel._renderSchedule();
  assert.match(html, /aria-label="Requested action colours"/);
  assert.match(html, /data-schedule-action="heating"[^>]*aria-label="Living heater, .*Heating · On/);
  assert.match(html, /class="slot running advisory selected"/);
  assert.match(html, /Striped: estimated prices/);
  const css = panel._styles();
  for (const [action, colour] of [['heating', '#f5a38a'], ['cooling', '#a8cfe8'], ['charging', '#9dc4ad'],
    ['discharging', '#f6c573'], ['general', '#4a90c5']]) {
    assert.ok(css.includes(`[data-schedule-action="${action}"] { --schedule-action-colour:${colour}; }`));
  }
  assert.match(css, /\.slot\.advisory \{ background-image:repeating-linear-gradient/);
});

test('mixed-mode schedule keeps retained targets across permission changes', () => {
  const panel = makePanel();
  panel._data.timeline = { capabilities: { battery: true, pool: true } };
  const slot = { battery_command: { schema_version: 2, operation: 'solar_charge', charge_limit_w: 8800, discharge_limit_w: 0 },
    pool_w: 0, execution_owners: ['$battery'], execution: { capabilities: { battery: true, pool: false },
      battery_command: { schema_version: 2, operation: 'grid_charge', charge_limit_w: 500, discharge_limit_w: 0 },
      command_previews: { battery: { fields: [{ label: 'Charge limit', value: .5, unit: 'kW' }] } } } };
  assert.match(panel._scheduleCommand({ system: 'battery', mode: 'controlling' }, slot).text, /Charge up to 500 W/);
  assert.match(panel._scheduleCommand({ system: 'battery', mode: 'control_verification' }, slot).text, /Charge up to 500 W/);
  assert.equal(panel._scheduleCommandDetail({ system: 'battery', mode: 'controlling' }, slot), '');
  assert.equal(panel._scheduleCommand({ system: 'pool', mode: 'control_verification' }, slot).text, 'No heating requested');
  assert.equal(panel._scheduleCommand({ system: 'pool', mode: 'controlling' }, slot).text, 'No heating requested');
});

test('battery live inputs distinguish stale zero from unavailable and do not imply commissioning', () => {
  const panel = makePanel();
  const device = { live_inputs: { sources: {
    house_consumption_power_entity: { state: 'reported', watts: 3000 },
    solar_production_power_entity: { state: 'stale_report', watts: 0 },
    battery_power_measurement_entity: { state: 'unavailable' },
  } }, battery_writer: { owner: 'legacy' } };
  let html = panel._batteryLiveInputs(device);
  assert.match(html, /House: 3.00 kW/);
  assert.match(html, /Solar: 0.00 kW \(stale\)/);
  assert.match(html, /Battery: unavailable/);
  assert.match(html, /Waiting for the battery plan/);
  device.live_inputs.capture_stale = true;
  device.battery_writer.owner = 'fenced';
  html = panel._batteryLiveInputs(device);
  assert.match(html, /House: unavailable/);
  assert.match(html, /Waiting for the battery plan/);
  assert.doesNotMatch(html, /3.00 kW/);
});


test('device inclusion lives in Controls cards with state-dependent explanations', () => {
  const panel = splitPanel();
  panel._draft.excluded_device_readings = ['excluded'];
  panel._showExcluded = true;
  let html = panel._renderDevices();
  assert.doesNotMatch(html, /<summary>Included devices/);
  assert.equal((html.match(/data-share="pool"/g) || []).length, 1);
  assert.match(html, /data-share="pool"[^>]*checked/);
  assert.match(html, /Shares device data with SHS/);
  assert.match(html, /Sends no individual readings, profiles or metadata/);
  panel._draft.excluded_device_readings.push('pool');
  html = panel._renderDevice(panel._data.devices[0]);
  assert.doesNotMatch(html, /data-share="pool"[^>]*checked/);
  assert.doesNotMatch(html, /Shares device data with SHS/);
  assert.match(html, /Consumption stays in household totals/);
});

test('failed inclusion saves restore the toggle and explanation', async () => {
  const panel = splitPanel();
  panel._savedDraft.excluded_device_readings = [];
  panel._draft.excluded_device_readings = ['pool'];
  panel._hass = { callWS: async () => { throw new Error('Save failed'); } };
  await panel._saveInclusion();
  assert.equal(panel._error, 'Save failed');
  assert.match(panel._renderDevice(panel._data.devices[0]), /data-share="pool"[^>]*checked/);
});


test('battery measurement errors open and highlight the Energy fields, not the device editor', () => {
  const panel = splitPanel();
  const fields = [
    ['house_consumption_power_entity', 'Instantaneous house consumption'],
    ['solar_production_power_entity', 'Instantaneous solar production'],
    ['grid_power_entity', 'Signed grid power'],
  ].map(([key, label]) => ({ key, label, kind: 'entity', required: true }));
  const section = { id: 'prices_forecasts', tab: 'energy', title: 'Solar and electrical measurements', fields };
  panel._data.sections = [section];
  const fix = { kind: 'fields', fields: fields.map(f => ({ key: f.key, message: f.label + ' is required for battery control' })) };
  panel._data.attention = [{ key: 'battery_control', severity: 'warning', title: 'House battery setup needs attention', fix }];
  const battery = { key: '$battery', name: 'House battery', system: 'battery', included: true, fields: [], system_fields: [],
    permission: {}, mapping_status: 'not_configured', execution_status: { state: 'fault', fix } };
  panel._data.devices = [battery];
  panel._draft.device_control_mappings = {};
  const status = panel._renderAttention();
  assert.equal(panel._attention().length, 1);
  assert.doesNotMatch(status, /Edit House battery setup/);
  for (const field of fields) {
    assert.match(status, new RegExp('data-field-token="configuration::' + field.key + '"'));
    assert.equal(panel._fieldProblems(field.key).length, 1);
  }
  const sectionHtml = panel._renderSection(section);
  assert.match(sectionHtml, /3 fields need attention/);
  assert.equal((sectionHtml.match(/aria-invalid="true"/g) || []).length, 3);
  assert.equal((sectionHtml.match(/Required/g) || []).length, 3);
  assert.doesNotMatch(sectionHtml, /Add a setting/);
  battery.battery_runtime = { state: 'fault', reason: 'Measurement settings need attention', fix,
    display: {status: 'Measurement settings need attention', now: 'Complete the highlighted measurement settings.', loss: ''} };
  assert.doesNotMatch(panel._batteryLiveInputs(battery), /Waiting for current measurements/);
  assert.match(panel._batteryLiveInputs(battery), /Complete the highlighted measurement settings/);
  const card = panel._renderDevice(battery);
  assert.doesNotMatch(card, /Controls configured/);
  assert.match(card, /data-field-token="configuration::grid_power_entity"/);
  panel.shadowRoot = { querySelectorAll: () => [] };
  panel._openField('configuration::house_consumption_power_entity');
  assert.equal(panel._tab, 'energy');
  assert.ok(panel._expanded.has('section:prices_forecasts'));
  panel._data.attention = [];
  battery.execution_status = {};
  assert.equal(panel._fieldProblems('house_consumption_power_entity').length, 0);
});


test('battery policy service failure offers diagnostics without an actuator setup link', () => {
  const panel = splitPanel();
  panel._data.devices[0].execution_status = { state: 'fault', reason: 'Battery policy service returned an invalid response [request_id=request-123]',
    next_step: 'The service response needs investigation.', fix: { kind: 'diagnostics' } };
  const html = panel._renderAttention();
  assert.match(html, /request-123/);
  assert.match(html, /service response needs investigation/);
  assert.doesNotMatch(html, /Edit .* setup|data-action="edit-device"|mapped controls/);
  assert.equal((html.match(/Download diagnostics/g) || []).length, 1);
});

test('stale battery power sensor identifies its source and warning clears on recovery', () => {
  const panel = splitPanel();
  const battery = panel._data.devices[0];
  battery.execution_status = { state: 'fault', reason: 'sensor.grid: power source is invalid or stale',
    next_step: 'Check that the named power sensor is reporting current measurements. Control retries automatically when fresh readings arrive.',
    fix: { kind: 'diagnostics' }, retry_automatically: true };
  const html = panel._renderAttention();
  assert.match(html, /sensor.grid/);
  assert.match(html, /retries automatically when fresh readings arrive/);
  assert.doesNotMatch(html, /Edit .* setup|data-action="edit-device"|mapped controls/);
  assert.equal((html.match(/Download diagnostics/g) || []).length, 1);
  battery.execution_status = { state: 'controlling', reason: 'Live battery policy connected' };
  assert.doesNotMatch(panel._renderAttention(), /sensor.grid|power source is invalid or stale/);
});

test('diagnostic summary retains fault reasons, correction steps and recovered history', () => {
  const panel = splitPanel();
  panel._data.operation = { state: 'ready' };
  panel._data.diagnostics = {};
  panel._data.readiness = {};
  const battery = panel._data.devices[0];
  const reason = 'Write uncertain: number.charge=73: RuntimeError: Modbus read failed';
  battery.execution_status = { state: 'fault', reason, next_step: 'Check the inverter connection.',
    fix: { kind: 'diagnostics' }, retry_automatically: true };
  battery.battery_runtime = { state: 'fault', reason, fault_history: [{ at: '2026-09-16T19:15:01Z', reason }],
    pending_commands: [{ entity_id: 'number.charge', value: 73, stage: 'ambiguous' }] };
  let summary = panel._diagnosticSummary();
  assert.equal(summary.devices[0].reason, reason);
  assert.equal(summary.devices[0].next_step, 'Check the inverter connection.');
  assert.equal(summary.attention.find(x => x.detail === reason).severity, 'error');
  battery.execution_status = { state: 'controlling', reason: 'Battery settings confirmed' };
  summary = panel._diagnosticSummary();
  assert.equal(summary.devices[0].battery_runtime.fault_history[0].reason, reason);
});

test('battery status explains pending settings and never labels a known command state unavailable', () => {
  const panel = makePanel();
  const html = panel._batteryLiveInputs({ battery_runtime: {
    state: 'pending', reason: 'Waiting for physical confirmation of battery settings', command_state: 'reconciling',
    display: {status: 'Waiting for physical confirmation of battery settings', now: 'Waiting for the battery to confirm its settings.', loss: ''},
    requested_settings: { mode: 'Command Charging (PV First)', charge_limit_w: 73, discharge_limit_w: 0 },
    pending_writes: 1, pending_commands: [{ entity_id: 'number.charge', value: 73, stage: 'accepted' }],
  } });
  assert.match(html, /Waiting for physical confirmation/);
  assert.match(html, /Waiting for the battery to confirm its settings/);
  assert.doesNotMatch(html, /number.charge|Charge limit|PV First/);
  assert.doesNotMatch(html, /Not available|Live battery policy connected/);
});

const controllerExplanation = () => ({
  status: "Testing the plan; battery settings are not being changed",
  plan: "The plan is to charge the battery now for later use.",
  now: "The battery is charging at 1.50 kW.",
  difference: "The battery has 0.50 kWh less stored than the plan expected.",
  next: "In control mode, SHS would request extra charging to catch up.",
  deadline_ms: Date.parse('2026-09-17T10:15:00Z'),
});

test('battery card explains plan, difference and next action using the home timezone', () => {
  const panel = Object.create(context.Panel.prototype);
  panel._hass = {language: 'en-GB', config: {time_zone: 'Europe/Stockholm'}};
  const html = panel._batteryOutlook({battery_runtime: {explanation: controllerExplanation()}});
  assert.equal((html.match(/<p /g) || []).length, 1);
  assert.match(html, /charge the battery now for later use/);
  assert.match(html, /0.50 kWh less/);
  assert.match(html, /In control mode, SHS would request/);
  assert.match(html, /12:15/);
  assert.doesNotMatch(html, /SEK|benefit|continuation|DC|Next quarter/);
});

test('stale battery status hides predictions and escapes explanation text', () => {
  const panel = makePanel();
  const explanation = {...controllerExplanation(), next: '<script>untrusted</script>', deadline_ms:null};
  assert.match(panel._batteryOutlook({battery_runtime:{explanation}}), /&lt;script&gt;/);
  panel._refreshError='offline';
  assert.match(panel._batteryOutlook({battery_runtime:{explanation}}), /Waiting for a current battery plan/);
  assert.doesNotMatch(panel._batteryOutlook({battery_runtime:{explanation}}), /0.50/);
});

test('battery card uses controller outlook while other devices keep planner next-quarter text', () => {
  const panel = makePanel();
  panel._data.operation = {state: 'ready', label: 'Ready', now: '2026-09-17T10:00:00Z'};
  panel._data.portal = {};
  panel._data.timeline = {capabilities: {battery: true, pool: true}, slots: [
    {start: '2026-09-17T10:00:00Z', pool_w: 2000},
    {start: '2026-09-17T10:15:00Z', pool_w: 2000,
      battery_command: {schema_version: 2, operation: 'grid_charge', charge_limit_w: 9999}},
  ]};
  panel._sortedDevices = () => [
    {key: '$battery', name: 'House battery', system: 'battery', included: true, planned: true,
      battery_runtime: {reason: 'Live', explanation: controllerExplanation()}},
    {key: 'pool', name: 'Pool heater', system: 'pool', included: true, planned: true},
  ];
  panel._choices = () => ''; panel._deviceFieldButtons = () => '';
  panel._selectedSlot = 0;
  const html = panel._renderSchedule();
  const cards = html.match(/<article class="card schedule-device">.*?<\/article>/gs);
  assert.match(cards[0], /The plan is to charge/);
  assert.doesNotMatch(cards[0], /Next quarter|9999 W/);
  assert.match(cards[1], /Next quarter/);
});


test('battery card renders shared sensor wording without interpreting it again', () => {
  const panel = makePanel();
  const html = panel._batteryLiveInputs({battery_runtime: {display: {
    status: 'Testing the plan; battery settings are not being changed',
    now: 'Your home is using 2.67 kW. Solar is providing 0.00 kW.',
    loss: 'Energy-loss estimates use measurements from your system.',
  }}});
  assert.match(html, /Testing the plan/);
  assert.match(html, /2.67 kW/);
  assert.match(html, /Energy-loss estimates use measurements/);
  assert.doesNotMatch(html, /Waiting/);
});

test('battery card exposes persistent rejection without measurements and escapes text', () => {
  const panel = makePanel();
  const display = {status: 'A new battery plan could not be accepted.', now: 'Waiting for readings.',
    plan_warning: 'The previously accepted plan remains in use while it is valid. Waiting for a corrected plan. <script>'};
  let html = panel._batteryLiveInputs({battery_runtime: {display}});
  assert.match(html, /class="warning battery-plan-rejection"/);
  assert.match(html, /previously accepted plan/);
  assert.match(html, /Waiting for a corrected plan/);
  assert.match(html, /&lt;script&gt;/);
  assert.doesNotMatch(html, /<script>/);
  html = panel._batteryLiveInputs({battery_runtime: {display: {...display, plan_warning: ''}}});
  assert.doesNotMatch(html, /battery-plan-rejection/);
});

test('rejected plan appears on Status with diagnostics and clears after acceptance', () => {
  const panel = makePanel();
  panel._data.devices = [{key: '$battery', name: 'House battery', system: 'battery',
    mode: 'control_verification', execution_status: {state: 'fault', plan_status: 'rejected',
      reason: 'A new battery plan could not be accepted.', fix: {kind: 'diagnostics'},
      next_step: 'Waiting for a corrected plan.', retry_automatically: true}}];
  const issue = panel._attention().find(row => row.key === 'controller:$battery');
  assert.equal(issue.fix.kind, 'diagnostics');
  assert.match(issue.detail, /new battery plan could not be accepted/);
  assert.match(issue.next_step, /Waiting for a corrected plan/);
  panel._data.devices[0].execution_status = {state: 'verified', plan_status: 'accepted'};
  assert.equal(panel._attention().some(row => row.key === 'controller:$battery'), false);
});


test('pool setup links highlight switch and water editors and clear after correction', () => {
  const panel = makePanel();
  panel._data.devices = [{ key: 'pool', name: 'Pool heater', system: 'pool', included: true, planned: true,
    fields: [{key: 'actuator_entity_ids', label: 'Control entity', kind: 'entities', required: true}],
    system_fields: [{key: 'pool_water_temperature_entity', label: 'Pool water temperature', kind: 'entity', required: true}],
    planning_fields: [], field_errors: {actuator_entity_ids: ['Choose a switch'], pool_water_temperature_entity: ['Choose water sensor']} }];
  const issues = panel._attention();
  assert.equal(issues.length, 1);
  const targets = panel._fieldTargets(issues[0]);
  assert.deepEqual(Array.from(targets, t => t.token).sort(), ['configuration:pool:pool_water_temperature_entity', 'mapping:pool:actuator_entity_ids']);
  for (const target of targets) {
    assert.equal(target.tab, 'devices'); assert.equal(target.card, 'controls:pool');
    assert.equal(panel._fieldProblems(target.field.key, target.scope, 'pool').length, 1);
  }
  const html = panel._renderDevice(panel._data.devices[0]);
  assert.match(html, /aria-invalid="true"/);
  panel._data.devices[0].field_errors = {};
  assert.equal(panel._attention().length, 0);
});


test('pool card renders the shared sensor explanation and escapes entity content', () => {
  const panel = makePanel();
  const html = panel._controllerDetails({ controller_explanation: 'Testing the plan\nWater 29 °C; Stop at 32 °C\n<switch.pool> would be on' });
  assert.match(html, /Testing the plan<br>Water 29 °C; Stop at 32 °C/);
  assert.match(html, /&lt;switch.pool&gt; would be on/);
  assert.doesNotMatch(html, /<switch.pool>/);
});

test('HA object errors remain readable when loading and polling configuration', async () => {
  const panel = makePanel();
  assert.equal(panel._errorMessage({ error: { message: 'Unable to build configuration', code: 'configuration_error' } }), 'Unable to build configuration');
  assert.equal(panel._errorMessage({ code: 'timeout' }), 'timeout');
  assert.doesNotMatch(panel._errorMessage({}), /object Object/);
  panel._data = panel._draft = panel._savedDraft = undefined;
  panel._hass = { callWS: async () => { throw { error: { message: 'The integration is restarting' } }; } };
  await panel._load(false);
  assert.equal(panel._error, 'The integration is restarting');
  assert.equal(panel._loading, false);
  panel._entryId = 'entry';
  panel._hass = { callWS: async () => { throw { code: 'timeout' }; } };
  await panel._poll();
  assert.equal(panel._refreshError, 'timeout');
  panel._hass = { callWS: async () => ({ entry: { entry_id: 'entry' }, configuration: {}, devices: [] }) };
  await panel._load(false);
  await panel._poll();
  assert.equal(panel._error, '');
  assert.equal(panel._refreshError, '');
});

test('save, reload and replan retain the displayed schedule and drafts until fresh data arrives', async () => {
  const panel = splitPanel();
  panel._data.sections = [{ fields: [{ key: 'pool_heating_energy_entities' }] }];
  panel._data.timeline = { plan_id: 'old-plan' };
  panel._data.operation = { state: 'ready' };
  panel._data.attention = [];
  panel._savedDraft.pool_heating_energy_entities = ['sensor.pool_heater_energy'];
  panel._draft.pool_heating_energy_entities = ['sensor.pool_heater_energy', 'sensor.pool_pump_energy'];
  panel._draft.pool_volume_m3 = 60; // unrelated device draft
  const oldData = panel._data;
  const calls = [];
  panel._hass = { callWS: async message => {
    calls.push(message.type);
    return message.type.endsWith('/save') ? { saved: true, refreshing: true } : { refreshing: true };
  } };
  await panel._save();
  assert.equal(panel._refreshing, true);
  assert.equal(panel._data, oldData);
  assert.match(panel._refreshBanner(), /role="status".*spinner.*Refresh in progress/);
  assert.match(panel._renderDevice(panel._data.devices[0], 'planning'), /data-action="save-device"[^>]*disabled/);
  await panel._saveDevice('pool', 'planning');
  await panel._save();
  assert.deepEqual(calls, ['shs_energy/config/save']);
  await panel._poll();
  assert.equal(panel._data, oldData);
  assert.equal(panel._draft.pool_volume_m3, 60);
  assert.equal(panel._refreshError, '');
  panel._hass.callWS = async () => ({ ...oldData, timeline: { plan_id: 'new-plan' }, configuration: panel._savedDraft });
  await panel._poll();
  assert.equal(panel._refreshing, false);
  assert.equal(panel._data.timeline.plan_id, 'new-plan');
  assert.equal(panel._draft.pool_volume_m3, 60);
  assert.equal(panel._refreshBanner(), '');
  assert.doesNotMatch(panel._renderDevice(panel._data.devices[0], 'planning'), /data-action="save-device"[^>]*disabled/);
});

test('a real reload failure ends progress and provides a readable correction', async () => {
  const panel = splitPanel();
  panel._refreshing = true;
  const previous = panel._data;
  panel._hass = { callWS: async () => { throw { code: 'not_loaded', message: 'The integration is not loaded' }; } };
  await panel._poll();
  assert.equal(panel._refreshing, false);
  assert.equal(panel._refreshError, 'The integration is not loaded');
  assert.equal(panel._data, previous);
});

test('polling updates the progress banner and save locks without replacing the active editor', async () => {
  const panel = splitPanel();
  panel._draft.pool_volume_m3 = 60;
  const progress = { innerHTML: '' };
  const save = { dataset: { action: 'save-device', deviceKey: 'pool', section: 'planning' },
    closest: () => ({ querySelector: () => ({ textContent: '' }) }) };
  panel.shadowRoot = { activeElement: { tagName: 'INPUT' },
    querySelector: selector => selector === '[data-refresh-progress]' ? progress : null,
    querySelectorAll: selector => selector === 'button[data-action]' ? [save] : [] };
  panel._render = () => { throw new Error('Must not replace the editor'); };
  panel._hass = { callWS: async () => ({ refreshing: true }) };
  await panel._poll();
  assert.equal(save.disabled, true);
  assert.match(progress.innerHTML, /Refresh in progress/);
  panel._hass.callWS = async () => panel._data;
  await panel._poll();
  assert.equal(save.disabled, false);
  assert.equal(progress.innerHTML, '');
});


test('shared replan reasons are expandable, timestamped, escaped, and removed on server clear', () => {
  const panel = Object.create(context.Panel.prototype);
  panel._time = value => value;
  panel._data = {replan_recommendations: [{key: 'integration_change', reason: 'Battery <changed>', occurred_at: '2026-09-23T09:00:00Z'}]};
  const html = panel._renderReplanRecommendations();
  assert.match(html, /Manual replan recommended/);
  assert.match(html, /<details><summary>Reasons and times/);
  assert.match(html, /Battery &lt;changed&gt;/);
  assert.match(html, /2026-09-23T09:00:00Z/);
  panel._data.replan_recommendations = [];
  assert.equal(panel._renderReplanRecommendations(), '');
});
