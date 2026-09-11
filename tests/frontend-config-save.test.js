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
  panel._data = { sections: [{ toggle: { key: 'ev_enabled' }, fields: [{ key: 'ev_phase_count' }] }] };
  panel._draft = { ev_enabled: false, ev_phase_count: 1, _migration_report: { removed: ['old'] }, device_control_mappings: { car: { power: 1000 } } };
  panel._savedDraft = { ...panel._draft, ev_enabled: true, ev_phase_count: 3 };
  panel._entryId = 'entry';
  panel._render = () => {};
  let sent;
  panel._hass = { callWS: async (payload) => { sent = JSON.parse(JSON.stringify(payload)); } };
  await panel._save();
  assert.deepEqual(sent.configuration, { ev_enabled: false, ev_phase_count: 1 });
  assert.equal(panel._draft.device_control_mappings.car.power, 1000);
  assert.equal(panel._error, '');
});

test('general saves omit unchanged resolved defaults', async () => {
  const panel = Object.create(context.Panel.prototype);
  panel._data = { sections: [{ fields: [{ key: 'ev_phase_count' }, { key: 'ev_enabled' }] }] };
  panel._draft = { ev_phase_count: 3, ev_enabled: false };
  panel._savedDraft = { ev_phase_count: 3, ev_enabled: true };
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
  const response = { mapping_status: 'ready', panel: { devices: [{ key: 'a', name: 'Heater' }],
    configuration: { device_control_mappings: { a: updated, b: updated, c: updated } } } };
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
    { key: 'pool_temperature_minimum', label: 'Lowest temperature', kind: 'number' }], { temperature: 'sensor.room', pool_start_temperature_entity: 'number.start' });
  assert.match(html, /sensor.room/); assert.doesNotMatch(html, /Heating permission|data-field-key="offset_entity_id"/);
  assert.match(html, /data-field-key="pool_temperature_minimum"/); assert.match(html, /aria-label="Room temperature"/);
});

test('one permission row serves every device and always permits stopping', () => {
  const panel = makePanel(); panel._data.website_url = 'https://example.test/settings';
  for (const key of ['heater', 'ev', 'pool', '$battery']) {
    const html = panel._choices({ key, name: key, choice_label: 'Excluded', permission: { enabled: true, reason: 'Excluded on website' } });
    assert.match(html, /Include in the plan/); assert.match(html, /Let SHS operate it/);
    assert.match(html, /checked/); assert.doesNotMatch(html, / disabled/);
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
  assert.match(html, /Status unconfirmed/);
  assert.doesNotMatch(html, />Ready<\/span>/);
});

const splitPanel = () => {
  const panel = Object.create(context.Panel.prototype);
  const volume = { key: 'pool_volume_m3', kind: 'number', label: 'Pool volume' };
  const temperature = { key: 'pool_water_temperature_entity', kind: 'entity', label: 'Pool water temperature' };
  const mapping = { control_type: 'switch_schedule', actuator_entity_ids: ['switch.pool'] };
  panel._data = { devices: [{ key: 'pool', name: 'Pool pump', system: 'pool', category: 'pool_heating', included: true,
    mapping_status: 'ready', permission: { enabled: false }, fields: [], system_fields: [temperature, volume], planning_fields: [volume] },
    { key: 'excluded', name: 'Excluded microwave', included: false, category: 'household', fields: [], permission: { enabled: false } }],
    sections: [], labels: { ready: 'Ready', base_load: 'Excluded', pool_heating: 'Pool' } };
  panel._draft = { pool_volume_m3: 55, pool_water_temperature_entity: 'sensor.water', device_control_mappings: { pool: mapping } };
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
  assert.match(controls, /Pool water temperature/);
  assert.doesNotMatch(controls, /Pool volume/);
  assert.match(planning, /Pool volume/);
  assert.doesNotMatch(planning, /Pool water temperature|Let SHS operate/);
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
  panel._draft.pool_water_temperature_entity = 'sensor.other';
  let sent;
  panel._hass = { callWS: async payload => { sent = payload; return { mapping_status: 'ready' }; } };
  await panel._saveDevice('pool', 'controls');
  assert.deepEqual(JSON.parse(JSON.stringify(sent.configuration)), { pool_water_temperature_entity: 'sensor.other' });
  assert.equal(panel._deviceDirty('pool', 'planning'), true);
  panel._draft.pool_water_temperature_entity = 'sensor.third';
  panel._cancelDevice('pool', 'planning');
  assert.equal(panel._draft.pool_volume_m3, 55);
  assert.equal(panel._draft.pool_water_temperature_entity, 'sensor.third');
});
