import {expect, test} from '@playwright/test';

test('shows device meters once and keeps standalone meter edits safe to save', async ({page}) => {
  const heater = 'sensor.synthetic_heater_energy';
  const cooler = 'sensor.synthetic_cooler_energy';
  const appliance = 'sensor.synthetic_appliance_energy';
  const standalone = 'sensor.synthetic_standalone_energy';
  const grid = 'sensor.synthetic_grid_energy';
  const configuration = {
    entities_heating: [heater], entities_cooling: [cooler],
    entities_household: [appliance, standalone], entities_grid_import: [grid],
  };
  let saves = 0;
  await page.route('**/api/configuration/get', async route => {
    const data = await (await route.fetch()).json();
    const meter = (id: string, name: string, category: string, included = true) => ({
      key: id, statistic_id: id, name, category, included,
      mapping_status: 'not_configured', meter_room: {meters: [id], editable: true},
    });
    await route.fulfill({json: {
      ...data,
      configuration: {...data.configuration, ...configuration},
      devices: [meter(heater, 'Example heater', 'heating'), meter(cooler, 'Example cooler', 'cooling', false),
        meter(appliance, 'Example appliance', 'household')],
    }});
  });
  await page.route('**/api/configuration/save', async route => {
    const patch = route.request().postDataJSON().configuration;
    if (++saves === 1) expect(patch).toEqual({entities_grid_import: [grid, 'sensor.synthetic_second_grid_energy']});
    else expect(patch).toEqual({entities_household: [appliance]});
    Object.assign(configuration, patch);
    await route.fulfill({json: {saved: true, refreshing: false}});
  });
  await page.route('**/api/configuration/discover', route => route.fulfill({json: {
    configuration: {...configuration, entities_heating: []},
  }}));
  await page.goto('./#settings');
  const editor = page.locator('shs-configuration-editor');
  const card = editor.locator('details[data-open-key="section:metering"]');
  await card.locator('summary').first().click();
  await expect(card.getByRole('button', {name: `Remove ${grid}`, exact: true})).toBeVisible();
  await expect(card.getByRole('button', {name: `Remove ${standalone}`, exact: true})).toBeVisible();
  await expect(card.getByText(/Heating energy|Cooling energy/)).toHaveCount(0);
  await expect(card.getByRole('button', {name: `Remove ${appliance}`, exact: true})).toHaveCount(0);
  const readings = editor.locator('details').filter({has: page.locator('summary', {hasText: 'Current readings'})});
  await readings.locator('summary').click();
  await expect(readings.getByText(standalone, {exact: true})).toBeVisible();
  for (const id of [appliance, cooler, heater]) await expect(readings.getByText(id, {exact: true})).toHaveCount(0);

  await card.getByRole('combobox', {name: 'Add Grid import'}).fill('sensor.synthetic_second_grid_energy');
  await card.locator('button[data-action="add-multi"][data-field-key="entities_grid_import"]').click();
  await editor.getByRole('button', {name: 'Save changes'}).click();
  await expect.poll(() => saves).toBe(1);
  await card.getByRole('button', {name: 'Remove Household energy', exact: true}).click();
  await editor.getByRole('button', {name: 'Save changes'}).click();
  await expect.poll(() => saves).toBe(2);
  expect(configuration.entities_heating).toEqual([heater]);
  expect(configuration.entities_cooling).toEqual([cooler]);

  await editor.getByRole('button', {name: 'Review sources from HA Energy'}).click();
  await expect(editor.getByText('Device meters added: 0, removed: 1')).toBeVisible();
  await expect(editor.getByText(heater, {exact: true})).toHaveCount(0);
  await editor.getByRole('button', {name: 'Discard', exact: true}).click();
  expect(saves).toBe(2);
  await editor.getByRole('button', {name: 'Devices', exact: true}).click();
  await editor.getByRole('button', {name: 'Example heater', exact: true}).click();
  await editor.getByRole('button', {name: 'Measurements', exact: true}).click();
  await expect(editor.getByText(`Energy meter · ${heater}`, {exact: true})).toBeVisible();
});

test('links the selected room to the paired home on the test website', async ({page}) => {
  await page.route('**/api/configuration/get', async route => {
    const data = await (await route.fetch()).json();
    await route.fulfill({json: {...data, rooms: {greenhouse: 'Greenhouse'}}});
  });
  await page.goto('./#settings');
  const editor = page.locator('shs-configuration-editor');
  await editor.getByRole('button', {name: 'Rooms', exact: true}).click();
  const link = editor.getByRole('link', {name: 'Edit comfort on website'});
  await expect(link).toHaveAttribute('href', 'https://test-smart-home-solutions.pages.dev/portal/energy-modeling?tab=comfort&home=00000000-0000-4000-8000-000000000001&room=greenhouse');
});

test('saves the dashboard meter pair from the inline room editor', async ({page}) => {
  const energy = 'sensor.water_boiler_energy';
  const power = 'sensor.water_boiler_power';
  let data: Record<string, any>;
  let writes = 0;
  await page.route('**/api/configuration/get', async route => {
    data = await (await route.fetch()).json();
    data.rooms = {laundry: 'Laundry', garage: 'Garage'};
    data.devices = [{
      key: energy, name: 'Water boiler', statistic_id: energy, category: 'hot_water',
      room_name: 'No room', mapping_status: 'ready', included: true, mapping: {},
      meter_room: {meters: [energy, power], editable: true, area_id: null, different_areas: true},
    }];
    await route.fulfill({json: data});
  });
  await page.route('**/api/configuration/device_room', async route => {
    const body = route.request().postDataJSON();
    expect(body.device_key).toBe(energy);
    expect(body.meters).toEqual([energy, power]);
    expect(body.area_id).toBe('laundry');
    writes++;
    if (writes === 1) {
      await route.fulfill({status: 409, json: {message: 'Choose an existing Home Assistant room.'}});
      return;
    }
    data.devices[0].room_name = 'Laundry';
    data.devices[0].meter_room = {...data.devices[0].meter_room, area_id: 'laundry', different_areas: false};
    await route.fulfill({json: data});
  });
  await page.goto('./#settings');
  const editor = page.locator('shs-configuration-editor');
  await editor.getByRole('button', {name: 'Devices', exact: true}).click();
  const card = editor.locator('details.device-card').filter({hasText: 'Water boiler'});
  const room = card.getByRole('combobox', {name: 'Room', exact: true});
  const saveRoom = card.getByRole('button', {name: 'Save room', exact: true});
  await expect(card).toContainText('Controls configured');
  await expect(card).toContainText('different room assignments');
  await expect(card).toHaveAttribute('open', '');
  await expect(card.getByRole('button', {name: 'Room for Water boiler'})).toHaveCount(0);
  await expect(room).toHaveValue('');
  await room.selectOption('garage');
  await card.getByRole('button', {name: 'Cancel room change'}).click();
  await expect(room).toHaveValue('');
  await expect(room).toBeFocused();
  expect(writes).toBe(0);
  const category = card.getByRole('combobox', {name: 'Category'});
  await category.selectOption('heating');
  await room.selectOption('laundry');
  expect(writes).toBe(0);
  await saveRoom.click();
  await expect(card.getByRole('alert')).toHaveText('Choose an existing Home Assistant room.');
  await expect(room).toHaveAttribute('aria-invalid', 'true');
  await expect(room).toBeFocused();
  await saveRoom.click();
  await expect(saveRoom).toHaveCount(0);
  await expect(card.locator('summary')).toContainText('Laundry');
  await expect(room).toHaveValue('laundry');
  await expect(room).toBeFocused();
  await expect(category).toHaveValue('heating');
  await expect(card.getByRole('button', {name: 'Save controls'})).toBeEnabled();
  await editor.getByRole('button', {name: 'View room'}).click();
  await expect(editor.getByRole('heading', {name: 'Laundry'})).toBeVisible();
});

test('the device header switch shows the persisted inclusion state', async ({page}) => {
  const key = 'sensor.example_load_energy';
  let excluded = false;
  let attempts = 0;
  await page.route('**/api/configuration/get', async route => {
    const data = await (await route.fetch()).json();
    data.configuration.excluded_device_readings = excluded ? [key] : [];
    data.devices = [{key, name: 'Example load', included: !excluded, category: 'household', mapping_status: 'ready'}];
    await route.fulfill({json: data});
  });
  await page.route('**/api/configuration/save', async route => {
    const {configuration} = route.request().postDataJSON();
    expect(configuration).toEqual({excluded_device_readings: excluded ? [] : [key]});
    if (++attempts === 1) {
      await route.fulfill({status: 409, json: {message: 'Could not update device inclusion.'}});
      return;
    }
    excluded = configuration.excluded_device_readings.includes(key);
    await route.fulfill({json: {saved: true}});
  });
  await page.goto('./#settings');
  const editor = page.locator('shs-configuration-editor');
  await editor.getByRole('button', {name: 'Devices', exact: true}).click();
  const summary = editor.locator('details.device-card > summary');
  const toggle = summary.getByRole('switch', {name: 'Include Example load'});
  await expect(toggle).toBeChecked();
  await expect(summary.getByText('Included', {exact: true})).toBeVisible();
  await toggle.press('Space');
  await expect(editor.getByText('Could not update device inclusion.')).toBeVisible();
  await expect(toggle).toBeChecked();
  await expect(toggle).toBeFocused();
  await summary.getByText('Included', {exact: true}).click();
  await expect(summary.getByText('Excluded', {exact: true})).toBeVisible();
  await expect(toggle).not.toBeChecked();
  await expect(toggle).toBeEnabled();
  await expect(editor.getByRole('checkbox', {name: 'Show excluded devices'})).toBeChecked();
  await expect(editor.getByText(/Consumption stays in household totals/)).toBeVisible();
  await toggle.press('Space');
  await expect(summary.getByText('Included', {exact: true})).toBeVisible();
  await expect(toggle).toBeChecked();
  await expect.poll(() => attempts).toBe(3);
});

test('edits a category without writing until Save even when its room is read-only', async ({page}) => {
  const key = 'sensor.boiler_energy';
  let category = 'hot_water';
  let writes = 0;
  let data: Record<string, any>;
  await page.route('**/api/configuration/get', async route => {
    data = await (await route.fetch()).json();
    data.devices = [{
      key, name: 'Boiler', statistic_id: key, category,
      room_name: 'No room', mapping_status: 'not_configured', included: false, mapping: {},
      meter_room: {meters: [key], editable: false, area_id: null},
    }];
    await route.fulfill({json: data});
  });
  await page.route('**/api/configuration/save_device', async route => {
    expect(route.request().postDataJSON()).toMatchObject({device_key: key, settings: {category: 'heating'}});
    if (++writes === 1) {
      await route.fulfill({status: 409, json: {
        message: 'Choose a device category from the list', field_errors: {category: ['Choose a device category from the list']},
      }});
      return;
    }
    category = 'heating';
    data.devices[0].category = category;
    await route.fulfill({json: data});
  });
  await page.goto('./#settings');
  const editor = page.locator('shs-configuration-editor');
  await editor.getByRole('button', {name: 'Devices', exact: true}).click();
  await editor.getByRole('checkbox', {name: 'Show excluded devices'}).press('Space');
  const card = editor.locator('details.device-card').filter({hasText: 'Boiler'});
  const select = card.getByRole('combobox', {name: 'Category'});
  const save = card.getByRole('button', {name: 'Save controls'});
  await expect(select).toHaveValue('hot_water');
  await expect(save).toBeDisabled();
  await select.selectOption('heating');
  expect(writes).toBe(0);
  await expect(card.locator('summary')).toContainText('Hot water');
  await save.click();
  await expect(select).toHaveAttribute('aria-invalid', 'true');
  await expect(select).toBeFocused();
  await save.click();
  await expect(card.locator('summary')).toContainText('Heating');
  expect(writes).toBe(2);
});

test('loads settings and saves projected device fields without crypto.randomUUID', async ({page}) => {
  await page.addInitScript(() => {
    Object.defineProperty(window.crypto, 'randomUUID', { value: undefined });
  });
  const key = 'sensor.heater_energy';
  const original = {control_type: 'switch_schedule', power: 1230, actuator_entity_ids: ['switch.heater']};
  const uuidV4 = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
  const requestIds = new Set<string>();
  let data: Record<string, any>;
  let writes = 0;
  const fieldsFor = (method: string) => {
    const catalog = data.device_field_catalog;
    return catalog.rows['heating:' + method].fields.map((index: number) => catalog.fields[index]);
  };
  await page.route('**/api/configuration/get', async route => {
    const {request_id} = route.request().postDataJSON();
    expect(request_id).toMatch(uuidV4);
    requestIds.add(request_id);
    data = await (await route.fetch()).json();
    data.configuration.device_control_mappings = {[key]: original};
    data.devices = [{
      key, name: 'Heater', category: 'heating', included: true, room_name: 'Room',
      mapping_status: 'ready', control_type: 'switch_schedule', load_type: 'fixed_full_load',
      fields: fieldsFor('switch_schedule'), mapping: original,
      meter_room: {meters: [key], editable: false, area_id: null},
    }];
    await route.fulfill({json: data});
  });
  await page.route('**/api/configuration/save_device', async route => {
    const settings = {control_type: 'setpoint', load_type: 'inverter'};
    const body = route.request().postDataJSON();
    expect(body.request_id).toMatch(uuidV4);
    expect(requestIds.has(body.request_id)).toBe(false);
    requestIds.add(body.request_id);
    expect(body).toMatchObject({device_key: key, settings, mapping: {
      control_type: 'setpoint', power: 1230, actuator_entity_ids: ['switch.heater'],
    }});
    if (++writes === 1) {
      await route.fulfill({status: 409, json: {
        message: 'Choose a room sensor', field_errors: {temperature_entity_id: ['Choose a room sensor']},
      }});
      return;
    }
    expect(body.mapping.temperature_entity_id).toBe('sensor.room');
    data.configuration.device_control_mappings[key] = body.mapping;
    Object.assign(data.devices[0], settings, {fields: fieldsFor('setpoint'), mapping: body.mapping});
    await route.fulfill({json: data});
  });
  await page.goto('./#settings');
  const editor = page.locator('shs-configuration-editor');
  await expect(editor.getByRole('heading', {name: 'Energy and device settings'})).toBeVisible({timeout: 5000});
  await editor.getByRole('button', {name: 'Devices', exact: true}).click();
  const card = editor.locator('details.device-card').filter({hasText: 'Heater'});
  const views = editor.getByRole('navigation', {name: 'Device views'});
  await views.getByRole('button', {name: 'Plan', exact: true}).click();
  await expect(editor.getByRole('heading', {name: 'Not in the plan'})).toBeVisible();
  await expect(editor.getByText('This device is set to Monitoring on the website.')).toBeVisible();
  await views.getByRole('button', {name: 'Configuration', exact: true}).click();
  await expect(editor.getByRole('button', {name: 'Heater', exact: true})).toHaveAttribute('aria-current', 'true');
  const method = card.getByRole('combobox', {name: 'Control method'});
  const load = card.getByRole('combobox', {name: 'Load characteristic'});
  const temperature = card.getByRole('combobox', {name: 'Room temperature'});
  await method.selectOption('setpoint');
  await load.selectOption('inverter');
  await temperature.fill('sensor.room');
  await method.selectOption('variable_power');
  await expect(temperature).toHaveCount(0);
  await expect(card.getByRole('combobox', {name: 'Power or current control'})).toBeVisible();
  await expect(card.getByRole('combobox', {name: 'Power', exact: true})).toHaveValue('1230');
  await method.selectOption('setpoint');
  await expect(temperature).toHaveValue('sensor.room');
  await expect(card.getByRole('combobox', {name: 'Control entity', exact: true})).toHaveValue('switch.heater');
  expect(writes).toBe(0);
  const save = card.getByRole('button', {name: 'Save controls'});
  await save.click();
  await expect(temperature).toBeFocused();
  await expect(temperature).toHaveAttribute('aria-invalid', 'true');
  await save.click();
  await expect(save).toBeDisabled();
  await expect(temperature).toHaveValue('sensor.room');
  expect(writes).toBe(2);
});
