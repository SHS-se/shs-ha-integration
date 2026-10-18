import {test,expect} from '@playwright/test';

test('accuracy buttons show graded device percentages and restore keyboard focus',async({page},info)=>{
  const devices = [['baseline','Baseline',94,'green'],['ev','Car',84,'lime'],['pool','Pool',74,'yellow'],
    ['boiler','Water boiler',64,'orange'],['battery','House battery',54,'red']].map(([key,name,percentage,grade])=>({key,name,percentage,grade}));
  await page.route('**/api/state',async route=>{
    const response=await route.fetch(),data=await response.json();
    data.snapshot.entries[0].accuracy={version:2,calculated_at:'2026-10-17T16:00:01Z',scheduled_at:'2026-10-17T16:00:00Z',
      timezone:'Europe/Stockholm',historical:{percentage:64,grade:'orange',devices},prices:{percentage:80,grade:'lime',quarters:96}};
    data.snapshot.entries[0].input_accuracy={version:1,observed_at:'2026-10-18T08:00:00Z',percentage:88,grade:'lime',checks:[
      {key:'prices',percentage:80,grade:'lime',quarters:96,calculated_at:'2026-10-17T16:00:01Z',issues:[]},
      {key:'solar_forecast',percentage:100,grade:'green',healthy:8,expected:8,issues:[]},
      {key:'sensors',percentage:98,grade:'green',healthy:63,expected:64,issues:['sensor.kitchen_heater_power']},
      {key:'measurements',percentage:54,grade:'red',healthy:52,expected:96,issues:['2026-10-18T01:00:00+00:00']}]};
    await route.fulfill({json:data});
  });
  await page.goto('./#plan');
  const input=page.getByRole('button',{name:/Input accuracy %/});
  await expect(input).toContainText('88');
  await expect(page.getByRole('button',{name:/Forecast accuracy %/})).toHaveCount(0);
  await input.click();
  const popup=page.getByRole('dialog');
  for(const name of ['Prices','Solar forecast','Sensors','Measurements'])await expect(popup.getByText(name,{exact:true})).toBeVisible();
  await expect(popup.getByText('sensor.kitchen_heater_power',{exact:false})).toBeVisible();
  await expect(popup.getByText('52 of 96 quarters complete',{exact:false})).toBeVisible();
  await expect(popup.getByText('Estimates against published prices, 96 quarters',{exact:false})).toBeVisible();
  await page.screenshot({path:info.outputPath(`ha-input-accuracy-${info.project.name}.png`)});
  await page.keyboard.press('Escape');
  await expect(input).toBeFocused();
  await page.getByRole('button',{name:/Historical accuracy %/}).click();
  await expect(popup.getByRole('heading')).toHaveText('Historical accuracy %');
  for(const n of [94,84,74,64,54])await expect(popup.getByText(`${n}%`,{exact:true})).toBeVisible();
  await expect(popup.locator('.accuracy-schedule')).toContainText(/Next update (today|tomorrow) at (06|18):00\./);
  expect(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth)).toBe(false);
  await page.screenshot({path:info.outputPath(`ha-accuracy-${info.project.name}.png`)});
});
