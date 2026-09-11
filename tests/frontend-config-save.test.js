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

test('polling leaves configuration tabs, drafts and focused editors alone', async () => {
  const panel = splitPanel();
  let requests = 0;
  panel._hass = { callWS: async () => { requests++; return panel._data; } };
  for (const tab of ['energy', 'devices']) { panel._tab = tab; await panel._poll(); }
  panel._tab = 'schedule';
  panel._draft.pool_volume_m3 = 60;
  await panel._poll();
  panel._draft.pool_volume_m3 = 55;
  panel.shadowRoot = { activeElement: { tagName: 'INPUT' } };
  await panel._poll();
  assert.equal(requests, 0);
  panel.shadowRoot.activeElement = null;
  await panel._poll();
  assert.equal(requests, 1);
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
