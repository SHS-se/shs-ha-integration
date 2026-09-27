import {test,expect} from '@playwright/test';

test('branded ingress dashboard, navigation, schedule inspection and themes',async({page},info)=>{
  const errors:string[]=[];page.on('pageerror',e=>errors.push(e.message));
  await page.goto('./');
  await expect(page.getByRole('heading',{name:'A clearer view of your energy'})).toBeVisible();
  await expect(page.getByText('3.79 kW')).toBeVisible();
  await expect(page.getByRole('heading',{name:'Your app is running the home'})).toBeVisible();
  await expect(page.locator('.brand img')).toHaveJSProperty('naturalWidth',512);
  await page.screenshot({path:`test-results/${info.project.name}-overview.png`,fullPage:true});
  await page.getByRole('link',{name:'Schedule',exact:true}).click();
  await expect(page.getByRole('heading',{name:'Your energy schedule'})).toBeVisible();
  await expect(page.locator('canvas')).toHaveCount(1);
  await page.getByRole('button',{name:'Next →'}).click();
  await expect(page.getByLabel('Selected interval')).toHaveValue('1');
  await page.getByRole('button',{name:'Show data table'}).click();
  await expect(page.getByRole('table').first()).toBeVisible();
  await page.getByRole('button',{name:'Show charts'}).click();
  await page.screenshot({path:`test-results/${info.project.name}-schedule.png`,fullPage:true});
  await page.getByRole('link',{name:'Settings',exact:true}).click();
  await page.getByLabel('Color theme').selectOption('dark');
  await expect(page.locator('html')).toHaveAttribute('data-theme','dark');
  await page.getByRole('link',{name:'System',exact:true}).click();
  await expect(page.getByRole('heading',{name:'Database inspection'})).toBeVisible();
  await page.screenshot({path:`test-results/${info.project.name}-system-dark.png`,fullPage:true});
  const overflow=await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth);
  expect(overflow).toBe(false);
  await page.getByRole('link',{name:'Controller',exact:true}).click();
  await expect(page.getByRole('heading',{name:'Latest controller decisions'})).toBeVisible();
  expect(errors).toEqual([]);
});

test('incompatible companion and failed installation stay actionable',async({page})=>{
  await page.route('**/api/state',async route=>{
    const response=await route.fetch();const data=await response.json();
    data.snapshot=null;data.connection={state:'incompatible',message:'Load the bundled companion',loaded_version:'older'};
    data.companion={state:'failed',message:'The installed integration differs from a verified release.'};
    await route.fulfill({json:data});
  });
  await page.goto('./#settings');
  await expect(page.getByText('Companion update required')).toBeVisible();
  await expect(page.getByText('The installed integration differs from a verified release.')).toBeVisible();
  await expect(page.getByRole('link',{name:'Open app settings'})).toHaveAttribute('href','/hassio/addon/test_shs_energy/config');
});

test('keyboard navigation and reduced motion support',async({page})=>{
  await page.emulateMedia({reducedMotion:'reduce'});await page.goto('./');
  await page.keyboard.press('Tab');await expect(page.getByRole('link',{name:'Skip to content'})).toBeFocused();
  await page.goto('./#schedule');
  await page.getByRole('button',{name:'Next →'}).focus();await page.keyboard.press('Enter');
  await expect(page.getByLabel('Selected interval')).toHaveValue('1');
});
