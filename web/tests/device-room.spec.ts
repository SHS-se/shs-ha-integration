import {expect, test} from '@playwright/test';

test('sets the room on the dashboard meter pair from the selected device', async ({page}) => {
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
  const button = card.getByRole('button', {name: 'Room for Water boiler'});
  const dialog = editor.getByRole('dialog', {name: 'Room for Water boiler'});
  await expect(card).toContainText('Controls configured');
  await button.click();
  await expect(dialog).toContainText('different room assignments');
  await expect(card).toHaveAttribute('open', '');
  await dialog.getByRole('button', {name: 'Cancel'}).click();
  await expect(dialog).toHaveCount(0);
  await expect(button).toBeFocused();
  expect(writes).toBe(0);
  await button.click();
  const room = dialog.getByRole('combobox', {name: 'Room'});
  await room.selectOption('laundry');
  await dialog.getByRole('button', {name: 'Save room', exact: true}).click();
  await expect(dialog.getByRole('alert')).toHaveText('Choose an existing Home Assistant room.');
  await expect(room).toBeFocused();
  await dialog.getByRole('button', {name: 'Save room', exact: true}).click();
  await expect(dialog).toHaveCount(0);
  await expect(card.locator('summary')).toContainText('Laundry');
  await expect(button).toBeFocused();
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

test('projects fields locally, preserves compatible values and saves selectors with bindings once', async ({page}) => {
  const key = 'sensor.heater_energy';
  const original = {control_type: 'switch_schedule', power: 1230, actuator_entity_ids: ['switch.heater']};
  let data: Record<string, any>;
  let writes = 0;
  const fieldsFor = (method: string) => {
    const catalog = data.device_field_catalog;
    return catalog.rows['heating:' + method].fields.map((index: number) => catalog.fields[index]);
  };
  await page.route('**/api/configuration/get', async route => {
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
