import {expect, test} from '@playwright/test';

test('edit the heating target, duration, and flexibility order from the controller', async ({page}) => {
  const room = 'device:living-room';
  const zone = {
    name: 'Living room', target_c: 20, planned_target_c: 20, applied_c: 20,
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
      } else if (edit.field === 'order') {
        controls.order = edit.value;
      }
    }
    await route.fulfill({json: controls});
  });
  await page.goto('./#controller');
  const temperature = page.getByRole('spinbutton', {name: 'Heating target (°C)'});
  await expect(temperature).toHaveValue('20');
  await temperature.fill('22');
  await temperature.press('Enter');
  await expect(page.getByRole('spinbutton', {name: 'Remaining minutes'})).toHaveValue('60');
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
