import {test,expect} from '@playwright/test';

test('backend pairing corrections focus their field and selection failures retain the current source',async({page})=>{
  await page.route('**/api/configuration/select_backend',route=>route.fulfill({status:409,json:{message:'Pair this backend before selecting its plan',field_errors:{backend_production_pairing_code:'Pairing is required'}}}));
  await page.route('**/api/configuration/pair_backend',route=>route.fulfill({status:409,json:{message:'Enter the pairing code from this backend',field_errors:{backend_production_pairing_code:'Pairing code is required'}}}));
  await page.goto('./#settings');
  const editor=page.locator('shs-configuration-editor');
  await expect(editor.getByRole('heading',{name:'Plan source',exact:true})).toBeVisible();
  await editor.getByRole('button',{name:'Use production plans',exact:true}).click();
  const code=editor.getByLabel('Production pairing code',{exact:true});
  await expect(code).toBeFocused();
  await expect(code).toHaveAttribute('aria-invalid','true');
  await expect(editor.getByRole('heading',{name:'Test · Selected'})).toBeVisible();
  await expect(editor.getByRole('link',{name:'Open production billing and pairing'})).toHaveAttribute('href','https://smarthomesolutions.se/portal/account');
  await editor.getByRole('button',{name:'Pair production',exact:true}).click();
  await expect(editor.getByText('Pairing code is required',{exact:true})).toBeVisible();
  await expect(code).toBeFocused();
  expect(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth)).toBe(false);
});

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

test('configuration field deep link opens the section and focuses its real editor',async({page})=>{
  await page.goto('./#settings?field=house_consumption_power_entity&scope=configuration');
  const editor=page.locator('shs-configuration-editor');
  const field=editor.getByRole('combobox',{name:'Instantaneous house consumption',exact:true});
  await expect(field).toBeVisible();
  await expect(field).toBeFocused();
  await expect(field.locator('xpath=ancestor::details')).toHaveAttribute('open','');
});
