import {test,expect} from '@playwright/test';

test('Plan is default and cloud activity shares the accuracy row',async({page},info)=>{
  await page.clock.install();
  let connection={state:'connected',retry_at:null as string|null,persistent:false};
  await page.route('**/api/state',async route=>{
    const data=await(await route.fetch()).json();data.cloud_connection=connection;
    await route.fulfill({json:data});
  });
  await page.goto('./');
  await expect(page.getByRole('link',{name:'Plan',exact:true})).toHaveAttribute('aria-current','page');
  const row=page.locator('.planner-status-row'),indicator=row.getByLabel('Cloud connection');
  await expect(row.getByRole('button',{name:/Forecast accuracy/})).toBeVisible();
  await expect(indicator).toHaveText('Connected');
  connection={state:'waiting',retry_at:new Date(await page.evaluate(()=>Date.now())+30000).toISOString(),persistent:false};
  await page.clock.runFor(15100);await expect(indicator).toContainText('s');
  const before=await indicator.textContent();await page.clock.runFor(2000);expect(await indicator.textContent()).not.toBe(before);
  await expect(page.locator('.operational-attention')).toHaveCount(0);
  for(const state of ['connecting','disconnecting']) {
    connection={state,retry_at:null,persistent:false};await page.clock.runFor(15100);
    await expect(indicator).toHaveText(state==='connecting'?'Connecting…':'Disconnecting…');
  }
  connection={state:'waiting',retry_at:null,persistent:true};await page.clock.runFor(15100);
  await expect(indicator).toHaveText('Connection error');
  connection={state:'connected',retry_at:null,persistent:false};await page.clock.runFor(15100);await expect(indicator).toHaveText('Connected');
  expect(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth)).toBe(false);
  await page.screenshot({path:`test-results/${info.project.name}-connection-row.png`});
});

test('browser failures remain quiet for an hour and clear after recovery',async({page})=>{
  await page.clock.install();let offline=false;
  await page.route('**/api/state',async route=>{if(offline)await route.fulfill({status:503});else await route.continue();});
  await page.goto('./');await expect(page.getByLabel('Cloud connection')).toHaveText('Connected');
  offline=true;await page.clock.runFor(15100);await expect(page.getByRole('alert')).toHaveCount(0);
  await page.clock.fastForward(3599000);await expect(page.getByRole('alert')).toHaveCount(0);
  await page.clock.runFor(1200);await expect(page.getByRole('alert')).toContainText('App request failed (503)');
  offline=false;await page.clock.runFor(61000);await expect(page.getByRole('alert')).toHaveCount(0);
});
