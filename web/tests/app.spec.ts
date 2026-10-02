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
  const chart=page.getByRole('img',{name:/Price, power flows, consumption, storage and cost/});
  await expect(chart).toBeVisible();
  await expect(page.locator('canvas')).toHaveCount(0);
  await expect(chart.getByText('Pool heater')).toBeVisible();
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
  const skip=page.getByRole('link',{name:'Skip to content'});
  await expect(skip).toBeAttached();await page.keyboard.press('Tab');await expect(skip).toBeFocused();
  await page.goto('./#schedule');
  await page.getByRole('button',{name:'Next →'}).focus();await page.keyboard.press('Enter');
  await expect(page.getByLabel('Selected interval')).toHaveValue('1');
});


test('existing runtime recovery shows progress and then the live dashboard',async({page})=>{
  let recovering=true;
  await page.route('**/api/state',async route=>{
    const response=await route.fetch();const data=await response.json();
    if(recovering){data.snapshot=null;data.recovery={processed_receipt:42};data.connection={state:'recovering',message:'Processing queued observations.'};}
    await route.fulfill({json:data});
  });
  await page.goto('./');
  await expect(page.getByRole('heading',{name:'Restoring SHS'})).toBeVisible();
  await expect(page.getByText('Processed through observation 42.')).toBeVisible();
  await expect(page.getByRole('heading',{name:'Connect your Home Assistant integration'})).toHaveCount(0);
  recovering=false;
  await page.reload();
  await expect(page.getByRole('heading',{name:'Your app is running the home'})).toBeVisible();
  await expect(page.getByRole('heading',{name:'Restoring SHS'})).toHaveCount(0);
});

test('schedule chart explains an interval, selects it and narrows to one day',async({page},info)=>{
  await page.route('**/api/state',async route=>{
    const response=await route.fetch();const data=await response.json();
    const schedule=data.snapshot.entries[0].schedule,first=schedule.household[1],start=Date.now()-2*3600000;
    schedule.devices=[{key:'pool_heater',name:'Pool heater',category:'pool_heating'},{key:'pool_pump',name:'pool_pump',category:'pool_heating'}];
    schedule.household=Array.from({length:200},(_,i)=>{const day=(i%96)/96,sun=Math.max(0,Math.sin((day-.25)*2*Math.PI)),on=i%96<24;return {...first,
      start:new Date(Math.floor(start/900000)*900000+i*900000).toISOString(),duration_hours:.25,binding:i<110,
      shadow_import_sek_per_kwh:1.4+Math.sin(i/14),shadow_export_sek_per_kwh:.6+Math.sin(i/14)/2,
      pv_w:5000*sun,base_w:900,device_loads_w:{pool_heater:on?2400:0,pool_pump:on?700:0},load_w:900+(on?3100:0),
      grid_import_w:on?2500:0,grid_export_w:sun>.6?1500:0,battery_charge_w:sun>.6?2000:0,battery_discharge_w:on?600:900,
      battery_soc:.2+.6*sun,ev_soc:.69,import_cost_sek:on?.9:0,export_revenue_sek:sun>.6?.2:0};});
    await route.fulfill({json:data});
  });
  await page.goto('./#schedule');
  const chart=page.getByRole('img',{name:/Price, power flows, consumption, storage and cost/});
  await expect(chart.getByText('NOW · PLAN →')).toBeVisible();
  await expect(chart.getByText(/dashed = estimated/)).toBeVisible();
  // Positions are relative to the chart, so a scroll between steps cannot move the target.
  const point={x:220,y:(await chart.boundingBox())!.height*.4};
  await chart.hover({position:point});
  const tooltip=page.getByRole('tooltip');
  await expect(tooltip).toContainText('House demand');
  await expect(tooltip).toContainText('Base load');
  await expect(tooltip).toContainText('Cost so far');
  await page.screenshot({path:`test-results/${info.project.name}-schedule-tooltip.png`,fullPage:true});
  await chart.click({position:point});
  await expect(page.getByLabel('Selected interval')).not.toHaveValue('0');
  const days=page.getByRole('group',{name:'Days shown'});
  await expect(days.getByRole('button',{name:'All'})).toHaveAttribute('aria-pressed','true');
  await days.getByRole('button').nth(1).click();
  await expect(days.getByRole('button').nth(1)).toHaveAttribute('aria-pressed','true');
  await chart.focus();await page.keyboard.press('ArrowRight');await page.keyboard.press('ArrowRight');
  await expect(tooltip).toContainText('Home battery');
  await page.keyboard.press('Enter');
  await expect(page.getByLabel('Selected interval')).not.toHaveValue('0');
  expect(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth)).toBe(false);
  await page.getByRole('link',{name:'Settings',exact:true}).click();
  await page.getByLabel('Color theme').selectOption('dark');
  await page.getByRole('link',{name:'Schedule',exact:true}).click();
  await page.screenshot({path:`test-results/${info.project.name}-schedule-dark.png`,fullPage:true});
});
