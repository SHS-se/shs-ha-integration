import {test,expect} from '@playwright/test';

test('accuracy buttons show graded device percentages and restore keyboard focus',async({page},info)=>{
  const devices = [['baseline','Baseline',94,'green'],['ev','Car',84,'lime'],['pool','Pool',74,'yellow'],
    ['boiler','Water boiler',64,'orange'],['battery','House battery',54,'red']].map(([key,name,percentage,grade])=>({key,name,percentage,grade}));
  await page.route('**/api/state',async route=>{
    const response=await route.fetch(),data=await response.json();
    data.snapshot.entries[0].accuracy={version:1,calculated_at:'2026-10-17T16:00:01Z',scheduled_at:'2026-10-17T16:00:00Z',
      timezone:'Europe/Stockholm',forecast_plan_id:'plan',forecast:{percentage:94,grade:'green',devices},historical:{percentage:64,grade:'orange',devices}};
    await route.fulfill({json:data});
  });
  await page.goto('./#schedule');
  const forecast=page.getByRole('button',{name:/Forecast accuracy %/});
  await expect(forecast).toContainText('94');
  await forecast.click();
  const popup=page.getByRole('dialog');
  for(const n of [94,84,74,64,54])await expect(popup.getByText(`${n}%`,{exact:true})).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(forecast).toBeFocused();
  await page.getByRole('button',{name:/Historical accuracy %/}).click();
  await expect(popup.getByRole('heading')).toHaveText('Historical accuracy %');
  expect(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth)).toBe(false);
  await page.screenshot({path:`/private/tmp/shs-forecast-accuracy/ha-accuracy-${info.project.name}.png`});
});
