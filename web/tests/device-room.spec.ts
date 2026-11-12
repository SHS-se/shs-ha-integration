import {expect, test} from '@playwright/test';

test('sets the room on the dashboard meter pair from a collapsed device card', async ({page}) => {
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
  const button = card.getByRole('button', {name: 'Settings for Water boiler'});
  const dialog = editor.getByRole('dialog', {name: 'Settings for Water boiler'});
  await expect(card).toContainText('Controls configured');
  await button.click();
  await expect(dialog).toContainText('different room assignments');
  await expect(card).not.toHaveAttribute('open', '');
  await dialog.getByRole('button', {name: 'Cancel'}).click();
  await expect(dialog).toHaveCount(0);
  await expect(button).toBeFocused();
  expect(writes).toBe(0);
  await button.click();
  const room = dialog.getByRole('combobox', {name: 'Room'});
  await room.selectOption('laundry');
  await dialog.getByRole('button', {name: 'Save', exact: true}).click();
  await expect(dialog.getByRole('alert')).toHaveText('Choose an existing Home Assistant room.');
  await expect(room).toBeFocused();
  await dialog.getByRole('button', {name: 'Save', exact: true}).click();
  await expect(dialog).toHaveCount(0);
  await expect(card.locator('summary')).toContainText('Laundry');
  await expect(button).toBeFocused();
});

test('changes the category from the cogwheel even when the room cannot be edited', async ({page}) => {
  const key = 'sensor.boiler_energy';
  let category = 'hot_water';
  let writes = 0;
  let data: Record<string, any>;
  await page.route('**/api/configuration/get', async route => {
    data = await (await route.fetch()).json();
    data.devices = [{
      key, name: 'Boiler', statistic_id: key, category,
      room_name: 'No room', mapping_status: 'not_configured', included: false, mapping: {},
      meter_room: {meters: [key], editable: false, area_id: null, different_areas: false},
    }];
    await route.fulfill({json: data});
  });
  await page.route('**/api/configuration/device_category', async route => {
    expect(route.request().postDataJSON()).toMatchObject({device_key: key, category: 'heating'});
    if (++writes === 1) {
      await route.fulfill({status: 409, json: {message: 'Choose a device category from the list'}});
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
  const typeFilter = editor.getByRole('combobox', {name: 'All types'});
  await typeFilter.selectOption('hot_water');
  const card = editor.locator('details.device-card').filter({hasText: 'Boiler'});
  const cogwheel = card.getByRole('button', {name: 'Settings for Boiler'});
  await cogwheel.click();
  const dialog = editor.getByRole('dialog', {name: 'Settings for Boiler'});
  const select = dialog.getByRole('combobox', {name: 'Category'});
  await expect(select).toHaveValue('hot_water');
  await expect(select.getByRole('option')).toHaveCount(7);
  await expect(dialog.getByRole('combobox', {name: 'Room'})).toHaveCount(0);
  const save = dialog.getByRole('button', {name: 'Save', exact: true});
  await expect(save).toBeDisabled();
  await select.selectOption('heating');
  await save.click();
  await expect(dialog.getByRole('alert')).toHaveText('Choose a device category from the list');
  await expect(select).toHaveAttribute('aria-invalid', 'true');
  await expect(select).toBeFocused();
  await save.click();
  await expect(dialog).toHaveCount(0);
  await expect(typeFilter).toHaveValue('');
  await expect(card).toContainText('Heating');
  await expect(cogwheel).toBeFocused();
  await cogwheel.click();
  await expect(select).toHaveValue('heating');
  expect(writes).toBe(2);
});
