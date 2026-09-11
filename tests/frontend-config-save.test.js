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
    { key: 'pool_stop_temperature_entity', label: 'Stop heating at', kind: 'entity' }], { temperature: 'sensor.room', pool_start_temperature_entity: 'number.start' });
  assert.match(html, /sensor.room/); assert.doesNotMatch(html, /Heating permission|data-field-key="offset_entity_id"/);
  assert.match(html, /data-field-key="pool_stop_temperature_entity"/); assert.match(html, /aria-label="Room temperature"/);
});

test('one permission row serves every device and always permits stopping', () => {
  const panel = makePanel(); panel._data.website_url = 'https://example.test/settings';
  for (const key of ['heater', 'ev', 'pool', '$battery']) {
    const html = panel._choices({ key, name: key, choice_label: 'Excluded', mode: 'controlling', permission: { enabled: true, reason: 'Excluded on website', verification_reason: 'Excluded on website' } });
    assert.match(html, /Include in the plan/); assert.match(html, /Device mode/);
    assert.match(html, /value="controlling" selected/);
    assert.match(html, /value="monitoring"\s*>/);
    assert.match(html, /value="control_verification"\s+disabled>/);
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
  panel._data = { devices: [{ key: 'pool', name: 'Pool pump', system: 'pool', category: 'pool_heating', included: true,
    mapping_status: 'ready', permission: { enabled: false }, fields: [], system_fields: [permission, temperature, volume], planning_fields: [volume] },
    { key: 'excluded', name: 'Excluded microwave', included: false, category: 'household', fields: [], permission: { enabled: false } }],
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

test('verification download is compact gzip JSON with shared slot references', async () => {
  const { gunzipSync } = require('node:zlib');
  const data = { schema_version: 2, slots: { slot: { start: 'now' } }, attempts: [{ slot_id: 'slot', count: 9 }], coverage: [] };
  let blob;
  let clicked = false;
  let revoked;
  const anchor = { click() { clicked = true; } };
  const downloadContext = loadPanel({ define() {}, get() {} });
  Object.assign(downloadContext, {
    Blob, Response, CompressionStream,
    URL: { createObjectURL(value) { blob = value; return 'blob:verification'; }, revokeObjectURL(value) { revoked = value; } },
    document: { createElement(tag) { assert.equal(tag, 'a'); return anchor; } },
  });
  const panel = Object.create(downloadContext.Panel.prototype);
  panel._entryId = 'entry';
  panel._render = () => {};
  panel._hass = { callWS: async payload => {
    assert.equal(payload.type, 'shs_energy/verification/download');
    return data;
  } };
  await panel._downloadVerification();
  assert.equal(panel._error, undefined);
  assert.equal(anchor.download, 'shs-control-verification.json.gz');
  assert.equal(blob.type, 'application/gzip');
  assert.equal(gunzipSync(Buffer.from(await blob.arrayBuffer())).toString(), JSON.stringify(data));
  assert.equal(clicked, true);
  assert.equal(revoked, 'blob:verification');
});
