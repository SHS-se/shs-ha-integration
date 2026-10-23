import {test,expect} from '@playwright/test';

const upload={key:'pipeline:upload',severity:'error',stage:'upload',title:'Planning data could not be sent',detail:'The server rejected the planning upload.',next_step:'Check the server connection.',fix:{kind:'diagnostics'}};
const pool={key:'controller:pool',severity:'error',stage:'execution',title:'Pool heating: plan execution is blocked',detail:'Waiting for battery charging to release grid capacity',next_step:'SHS retries automatically.',fix:{kind:'diagnostics'}};

test('failures stay prominent on every screen while a retained plan remains available',async({page},info)=>{
  await page.route('**/api/state',async route=>{
    const data=await (await route.fetch()).json();data.attention=[upload,pool];
    await route.fulfill({json:data});
  });
  await page.goto('./');
  const notice=page.getByRole('alert',{name:'SHS needs attention'});
  for(const screen of ['Overview','Plan','Controller','System','Settings']) {
    await page.getByRole('link',{name:screen,exact:true}).click();
    await expect(notice).toBeVisible();
    await expect(notice).toContainText(upload.detail);
    await expect(notice).toContainText(pool.detail);
    expect(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth)).toBe(false);
  }
  await expect(page.locator('.connection')).toHaveText('Needs attention');
  await page.getByLabel('Color theme').selectOption('dark');
  await expect(page.locator('html')).toHaveAttribute('data-theme','dark');
  await page.getByRole('link',{name:'Plan',exact:true}).click();
  await expect(page.getByRole('heading',{name:'Your energy schedule'})).toBeVisible();
  await page.screenshot({path:`test-results/${info.project.name}-operational-failures.png`,fullPage:true});
  await page.getByRole('link',{name:'Download app logs'}).first().focus();
  await expect(page.getByRole('link',{name:'Download app logs'}).first()).toBeFocused();
});

test('correction links focus real editors and one correction does not clear another stage',async({page})=>{
  await page.clock.install();
  let missing=true,uploadFailed=true,saved:Record<string,unknown>|null=null;
  const field={key:'battery_control',severity:'warning',title:'A house power source is required',detail:'Choose the instantaneous house consumption sensor.',
    fix:{kind:'fields',fields:[{key:'house_consumption_power_entity',scope:'configuration',message:'Choose house power',url:'#settings?field=house_consumption_power_entity&scope=configuration'}]}};
  await page.route('**/api/state',async route=>{
    const data=await (await route.fetch()).json();data.attention=[...(missing?[field]:[]),...(uploadFailed?[upload]:[])];
    await route.fulfill({json:data});
  });
  await page.route('**/api/configuration/get',async route=>{
    const data=await (await route.fetch()).json();
    data.attention=missing?[field]:[];
    data.entities=[{entity_id:'sensor.house',name:'House power',domain:'sensor',unit:'W',state:'1000',device_class:'power',state_class:'measurement'}];
    if(saved){data.configuration=saved;data.configured_keys=Object.keys(saved);}
    await route.fulfill({json:data});
  });
  await page.route('**/api/configuration/save',async route=>{
    const body=route.request().postDataJSON();
    expect(body.configuration.house_consumption_power_entity).toBe('sensor.house');
    saved=body.configuration;missing=false;
    await route.fulfill({json:{saved:true,refreshing:false,revision:body.expected_revision+1}});
  });
  await page.goto('./#system');
  await page.getByRole('link',{name:'Choose house power',exact:true}).click();
  const editor=page.locator('shs-configuration-editor');
  const control=editor.getByRole('combobox',{name:'Instantaneous house consumption',exact:true});
  await expect(control).toBeFocused();await expect(control).toHaveAttribute('aria-invalid','true');
  await control.fill('sensor.house');
  await control.blur();
  await editor.getByRole('button',{name:'Save changes',exact:true}).click();
  await expect(editor.getByText('All changes saved')).toBeVisible();
  await page.clock.fastForward(15000);
  await expect(page.locator('.operational-attention')).not.toContainText(field.title);
  await expect(page.locator('.operational-attention')).toContainText(upload.detail);
  uploadFailed=false;await page.clock.fastForward(15000);
  await expect(page.locator('.operational-attention')).toHaveCount(0);
});

test('source inspection works from Settings and startup failures need no snapshot',async({page})=>{
  let source=true;
  await page.route('**/api/state',async route=>{
    const data=await (await route.fetch()).json();
    data.attention=source?[{key:'pipeline:inputs',severity:'error',stage:'inputs',title:'Planning data could not be collected',detail:'sensor.forecast has no timestamped watts',fix:{kind:'entity',entity_id:'sensor.forecast'}}]:[{...upload,key:'pipeline:app:receipts',stage:'background',detail:'Receipt ingestion stopped'}];
    if(!source){data.snapshot=null;data.connection={state:'disconnected',message:'The app is reconnecting.'};}
    await route.fulfill({json:data});
  });
  await page.goto('./#settings');
  await expect(page.locator('shs-configuration-editor')).toBeVisible();
  await page.getByRole('link',{name:'Inspect source entity',exact:true}).click();
  await expect(page.getByRole('dialog',{name:'Source entity'})).toBeVisible();
  await expect(page.getByRole('dialog')).toContainText('sensor.forecast');
  source=false;await page.reload();
  await expect(page.getByRole('alert',{name:'SHS needs attention'})).toContainText('Receipt ingestion stopped');
  await page.getByRole('link',{name:'System',exact:true}).click();
  await expect(page.getByRole('alert',{name:'SHS needs attention'})).toContainText('Receipt ingestion stopped');
});

test('execution replanning is automatic and future car charging stays conditional',async({page})=>{
  await page.route('**/api/configuration/get',async route=>{
    const data=await(await route.fetch()).json();
    data.replan_recommendations=[];
    data.readiness.execution_replanning={in_flight:true,pending:{},owners:{
      $ev:{owner:'$ev',status:'temporarily_absent',cause:'vehicle_disconnected',blocked_intervals:[]},
      $pool:{owner:'$pool',status:'faulted',cause:'actuator_unavailable',blocked_intervals:[]}}};
    await route.fulfill({json:data});
  });
  await page.goto('./#settings');
  const editor=page.locator('shs-configuration-editor');
  const notice=editor.locator('[data-execution-replanning]');
  await expect(notice).toContainText('Automatically updating the household plan.');
  await expect(notice).toContainText('Future charging remains conditional on connection.');
  await expect(notice).toContainText('Unavailable until recovery is observed.');
  await expect(notice).toContainText('Other executable loads keep their commitments for the next hour.');
  await expect(editor.getByText('Manual replan recommended',{exact:true})).toHaveCount(0);
  expect(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth)).toBe(false);
});
