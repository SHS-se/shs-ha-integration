import {test,expect} from '@playwright/test';

test('app settings save without HA reload and expose revision conflicts',async({page},info)=>{
  await page.route('**/api/configuration/save',async route=>{const body=route.request().postDataJSON();await route.fulfill({json:{saved:true,refreshing:false,revision:body.expected_revision+1}});});
  await page.goto('./#settings');
  const editor=page.locator('shs-configuration-editor');
  await expect(editor.getByRole('heading',{name:'Energy and device settings'})).toBeVisible();
  await expect(editor.getByText('Settings are applied')).toBeVisible();
  await page.screenshot({path:`test-results/${info.project.name}-settings.png`,fullPage:true});
  expect(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth)).toBe(false);
  await editor.locator('summary').filter({hasText:'Solar and electrical measurements'}).click();
  const mode=editor.getByLabel('Solar forecast latitude',{exact:true});
  await mode.fill('60');
  await editor.getByRole('button',{name:'Save changes',exact:true}).click();
  await expect(editor.getByText('All changes saved')).toBeVisible();
  await expect(mode).toHaveValue('60');
  await mode.fill('61');
  await page.route('**/api/configuration/save',route=>route.fulfill({status:409,json:{message:'Configuration changed in another window; refresh before saving'}}));
  await editor.getByRole('button',{name:'Save changes',exact:true}).click();
  await expect(editor.getByText('Configuration changed in another window; refresh before saving',{exact:true})).toBeVisible();
  await expect(mode).toHaveValue('61');
});
