import {expect, test} from '@playwright/test';

test('adds, saves and removes the optional vehicle location on the car card', async ({page}) => {
  let saved: string | null = null;
  let data: Record<string, any>;
  const tracker = 'device_tracker.synthetic_vehicle';
  await page.route('**/api/configuration/get', async route => {
    data = await (await route.fetch()).json();
    const field = data.sections.flatMap((section: {fields: unknown[]}) => section.fields)
      .find((field: {key: string}) => field.key === 'ev_location_entity');
    expect(field).toMatchObject({label: 'Vehicle location', required: false, domains: ['device_tracker']});
    data.configuration.ev_location_entity = saved;
    data.devices = [{key: '$ev', name: 'Synthetic vehicle', system: 'ev', category: 'ev_charging',
      included: true, planned: true, mapping: {}, fields: [], system_fields: [field], planning_fields: [],
      mapping_status: 'ready', permission: {enabled: false}}];
    data.entities = [{entity_id: tracker, name: 'Synthetic vehicle location', domain: 'device_tracker'}];
    await route.fulfill({json: data});
  });
  await page.route('**/api/configuration/save_device', async route => {
    const body = route.request().postDataJSON();
    expect(body.device_key).toBe('$ev');
    if (body.configuration.ev_location_entity === 'sensor.synthetic_wrong') {
      await route.fulfill({status: 400, json: {message: 'Choose a vehicle tracker.',
        field_errors: {ev_location_entity: ['Choose a vehicle tracker.']}}});
      return;
    }
    saved = body.configuration.ev_location_entity;
    data.configuration.ev_location_entity = saved;
    await route.fulfill({json: {...data, revision: data.revision + 1}});
  });
  await page.goto('./#settings');
  const editor = page.locator('shs-configuration-editor');
  await editor.getByRole('button', {name: 'Devices', exact: true}).click();
  await editor.getByRole('button', {name: 'Synthetic vehicle', exact: true}).click();
  const card = editor.locator('details.device-card');
  await card.locator('summary').filter({hasText: 'Add a setting'}).click();
  await card.getByRole('button', {name: 'Add vehicle location', exact: true}).click();
  const field = card.getByRole('combobox', {name: 'Vehicle location', exact: true});
  await expect(field).toBeVisible();
  await expect(card).toContainText('Arriving home restarts the 60-hour');
  await field.fill('sensor.synthetic_wrong');
  await card.getByRole('button', {name: 'Save', exact: true}).click();
  await expect(field).toHaveAttribute('aria-invalid', 'true');
  await expect(field).toHaveValue('sensor.synthetic_wrong');
  await field.fill(tracker);
  await card.getByRole('button', {name: 'Save', exact: true}).click();
  await expect.poll(() => saved).toBe(tracker);
  await page.reload();
  await editor.getByRole('button', {name: 'Devices', exact: true}).click();
  await editor.getByRole('button', {name: 'Synthetic vehicle', exact: true}).click();
  await expect(field).toHaveValue(tracker);
  await card.getByRole('button', {name: 'Remove Vehicle location', exact: true}).click();
  await card.getByRole('button', {name: 'Save', exact: true}).click();
  await expect.poll(() => saved).toBeNull();
});
