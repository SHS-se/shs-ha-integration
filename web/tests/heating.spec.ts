import {expect, test} from '@playwright/test';

test('read and override the equipment setpoint from the controller', async ({page}) => {
  const room = 'device:living-room';
  const zone = {
    name: 'Living room', target_c: 20, planned_target_c: 20, applied_c: 20.5,
    min_c: 5, max_c: 35, step: .5, expires_at: null as string | null,
    minutes: 60, controlling: true,
  };
  const controls = {zones: {[room]: zone}, loads: {ev: 'Car', pool: 'Pool'}, order: ['pool', 'ev']};
  const edits: unknown[] = [];
  await page.route('**/api/heating', async route => {
    if (route.request().method() === 'POST') {
      const edit = route.request().postDataJSON();
      edits.push(edit);
      if (edit.field === 'target') {
        zone.target_c = edit.value;
        zone.expires_at = new Date(Date.now() + 60 * 60000).toISOString();
      } else if (edit.field === 'minutes') {
        zone.expires_at = new Date(Date.now() + edit.value * 60000).toISOString();
      } else if (edit.field === 'resume') {
        zone.expires_at = null;
        zone.target_c = zone.planned_target_c;
        zone.applied_c = zone.planned_target_c;
      } else if (edit.field === 'order') {
        controls.order = edit.value;
      }
    }
    await route.fulfill({json: controls});
  });
  await page.goto('./#controller');
  const temperature = page.getByRole('spinbutton', {name: 'Setpoint temperature (°C)'});
  await expect(temperature).toHaveValue('20.5');
  const planned = page.getByText('Planned setpoint 20 °C');
  await expect(planned).toBeHidden();
  await page.getByText('Plan details', {exact: true}).click();
  await expect(planned).toBeVisible();
  await page.getByText('Plan details', {exact: true}).click();
  await temperature.fill('22');
  await temperature.press('Enter');
  await expect(page.getByRole('spinbutton', {name: 'Remaining minutes'})).toHaveValue('60');
  await expect(temperature).toHaveValue('20.5');
  zone.applied_c = 22;
  await expect(temperature).toHaveValue('22');
  const remaining = page.getByRole('spinbutton', {name: 'Remaining minutes'});
  await remaining.fill('25');
  await remaining.press('Enter');
  await expect(remaining).toHaveValue('25');
  await page.getByRole('button', {name: 'Move Car earlier'}).click();
  await expect(page.getByRole('button', {name: 'Move Car earlier'})).toBeDisabled();
  await page.getByRole('button', {name: 'Return to plan'}).click();
  await expect(temperature).toHaveValue('20');
  await expect(page.getByRole('spinbutton', {name: 'Override duration (minutes)'})).toHaveValue('60');
  expect(edits).toEqual([
    {owner: room, field: 'target', value: 22},
    {owner: room, field: 'minutes', value: 25},
    {owner: null, field: 'order', value: ['ev', 'pool']},
    {owner: room, field: 'resume', value: null},
  ]);
});

test('a relay room accepts a setpoint while following an on/off plan', async ({page}) => {
  const room = 'room:bathroom';
  const zone = {name: 'Bathroom', planned_target_c: null, applied_c: null as number | null,
    min_c: null, max_c: null, step: .1, expires_at: null as string | null, minutes: 60,
    controlling: true, relay_states: {'device:wall-heater': 'off'}};
  await page.route('**/api/heating', async route => {
    if (route.request().method() === 'POST') {
      const edit = route.request().postDataJSON();
      expect(edit).toEqual({owner: room, field: 'target', value: 22});
      zone.applied_c = edit.value;
      zone.expires_at = new Date(Date.now() + 3600000).toISOString();
    }
    await route.fulfill({json: {zones: {[room]: zone}, loads: {}, order: []}});
  });
  await page.goto('./#controller');
  const temperature = page.getByRole('spinbutton', {name: 'Setpoint temperature (°C)'});
  await expect(temperature).toHaveValue('');
  await expect(page.getByText('The plan switches heating on and off.', {exact: false})).toBeVisible();
  await temperature.fill('22');
  await temperature.press('Enter');
  await expect(temperature).toHaveValue('22');
});

test('a relay room displays its planned temperature before an override', async ({page}) => {
  await page.route('**/api/heating', route => route.fulfill({json: {zones: {'room:hall': {
    name: 'Hall', planned_target_c: 20.5, room_target_c: 20.5, applied_c: null,
    min_c: null, max_c: null, step: .1, expires_at: null, minutes: 60,
    controlling: true, relay_states: {'device:heater': 'off'},
  }}, loads: {}, order: []}}));
  await page.goto('./#controller');
  await expect(page.getByRole('spinbutton', {name: 'Setpoint temperature (°C)'})).toHaveValue('20.5');
  await expect(page.getByText('The plan switches heating on and off.', {exact: false})).toHaveCount(0);
});
