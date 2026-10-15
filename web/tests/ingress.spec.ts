import {test,expect,type Page,type FrameLocator} from '@playwright/test';

const cookiePath='/api/hassio_ingress/';
const initialSession='a'.repeat(128),newSession='b'.repeat(128);
const recoveryMessage='Home Assistant access could not be renewed.';

test.afterEach(async({page})=>{await page.unrouteAll({behavior:'wait'});});

async function homeAssistant(page:Page,options:{rejectRenewal?:boolean;rejectRetry?:boolean;status?:number;holdRenewal?:Promise<void>}={}) {
  let expired=false,renewals=0,rejected=0,saved=0;
  await page.context().addCookies([{name:'ingress_session',value:initialSession,url:'http://127.0.0.1:8098'+cookiePath}]);
  await page.exposeFunction('renewIngress',async(message:unknown)=>{
    renewals++;
    expect(message).toEqual({type:'supervisor/api',endpoint:'/ingress/session',method:'post'});
    await options.holdRenewal;
    if(options.rejectRenewal)throw new Error('Home Assistant is unavailable');
    return {session:newSession};
  });
  await page.route('**/ha-test',route=>route.fulfill({contentType:'text/html',body:`
    <home-assistant></home-assistant>
    <script>document.querySelector('home-assistant').hass={callWS:message=>window.renewIngress(message)};</script>
    <iframe title="SHS" src="/api/hassio_ingress/test/#schedule" style="width:100%;height:900px"></iframe>`}));
  await page.route('**/api/hassio_ingress/test/**',async route=>{
    const request=route.request();
    const path=new URL(request.url()).pathname.split('/test/')[1];
    if(path.startsWith('api/')&&expired) {
      const authorised=request.headers().cookie?.includes(`ingress_session=${newSession}`);
      if(options.status||!authorised||options.rejectRetry) {
        rejected++;
        await route.fulfill({status:options.status??401,body:'Access rejected'});
        return;
      }
    }
    if(path==='api/configuration/save') {
      saved++;
      const body=request.postDataJSON();
      expect(body.configuration.outdoor_temperature_entity).toBe('sensor.outdoor_new');
      await route.fulfill({json:{saved:true,refreshing:false,revision:body.expected_revision+1}});
      return;
    }
    const response=await route.fetch({url:request.url().replace('/api/hassio_ingress/test/','/ingress/test/')});
    if(path==='api/state'&&expired) {
      const data=await response.json();
      data.snapshot.entries[0].measurements.house.value=4820;
      await route.fulfill({json:data});
    } else await route.fulfill({response});
  });
  await page.goto('/ha-test');
  const app=page.frameLocator('iframe');
  await expect(app.getByRole('heading',{name:'Your energy schedule'})).toBeVisible();
  return {app,options,expire:()=>{expired=true;},counts:()=>({renewals,rejected,saved})};
}

async function visibility(app:FrameLocator,hidden:boolean) {
  await app.locator('body').evaluate((_,hidden)=>{
    Object.defineProperty(document,'hidden',{configurable:true,value:hidden});
    document.dispatchEvent(new Event('visibilitychange'));
  },hidden);
}

test('returning to an expired ingress tab renews access and preserves the open schedule',async({page})=>{
  const harness=await homeAssistant(page),{app}=harness;
  await app.getByRole('button',{name:'Next →'}).click();
  await expect(app.getByLabel('Selected interval')).toHaveValue('1');
  await app.getByRole('button',{name:'Show data table'}).click();
  const documentId=await app.locator('body').evaluate(element=>element.dataset.documentId=crypto.randomUUID());
  await visibility(app,true);harness.expire();
  expect(harness.counts().renewals).toBe(0);
  await visibility(app,false);
  await expect.poll(()=>harness.counts()).toEqual({renewals:1,rejected:1,saved:0});
  await expect(app.locator('body')).toHaveAttribute('data-document-id',documentId);
  await expect(app.getByLabel('Selected interval')).toHaveValue('1');
  await expect(app.getByRole('button',{name:'Show charts'})).toBeVisible();
  await app.getByRole('link',{name:'Overview',exact:true}).click();
  await expect(app.getByText('4.82 kW')).toBeVisible();
  await expect(app.getByRole('alert')).toHaveCount(0);
  expect((await page.context().cookies('http://127.0.0.1:8098'+cookiePath))[0]).toMatchObject({value:newSession,path:cookiePath,sameSite:'Strict'});
});

test('dashboard and settings share renewal and preserve an unsaved draft through a rejected save',async({page})=>{
  let release!:()=>void;
  const harness=await homeAssistant(page,{holdRenewal:new Promise<void>(resolve=>{release=resolve;})}),{app}=harness;
  await app.getByRole('link',{name:'Settings',exact:true}).click();
  const editor=app.locator('shs-configuration-editor');
  await editor.locator('summary').filter({hasText:'Shared outdoor conditions'}).click();
  const latitude=editor.getByLabel('Measured outdoor temperature',{exact:true});
  await latitude.fill('sensor.outdoor_new');
  await visibility(app,true);harness.expire();await visibility(app,false);
  await expect.poll(()=>harness.counts().renewals).toBe(1);
  await editor.getByRole('button',{name:'Save changes',exact:true}).click();
  await expect.poll(()=>harness.counts().rejected).toBe(2);
  // Returning again while renewal is pending must not launch another poll.
  await visibility(app,false);
  expect(harness.counts().renewals).toBe(1);
  await expect(latitude).toHaveValue('sensor.outdoor_new');
  release();
  await expect(editor.getByText('All changes saved')).toBeVisible();
  await expect(latitude).toHaveValue('sensor.outdoor_new');
  await expect(app.locator('.connection')).toHaveText('Connected');
  expect(harness.counts()).toEqual({renewals:1,rejected:2,saved:1});
});

test('failed renewal keeps stale data visible and a later return can recover',async({page})=>{
  const harness=await homeAssistant(page,{rejectRenewal:true}),{app}=harness;
  harness.expire();await visibility(app,false);
  await expect(app.getByRole('alert')).toContainText(recoveryMessage);
  await expect(app.getByRole('alert')).toContainText('Displayed values are from the last successful update');
  await expect(app.getByRole('heading',{name:'Your energy schedule'})).toBeVisible();
  expect(harness.counts()).toEqual({renewals:1,rejected:1,saved:0});
  harness.options.rejectRenewal=false;
  await visibility(app,false);
  await expect.poll(()=>harness.counts().renewals).toBe(2);
  await expect(app.getByRole('alert')).toHaveCount(0);
  await app.getByRole('link',{name:'Overview',exact:true}).click();
  await expect(app.getByText('4.82 kW')).toBeVisible();
});

test('persistent 401 is retried only once and surfaces an actionable access error',async({page})=>{
  const harness=await homeAssistant(page,{rejectRetry:true}),{app}=harness;
  harness.expire();await visibility(app,false);
  await expect(app.getByRole('alert')).toContainText(recoveryMessage);
  expect(harness.counts()).toEqual({renewals:1,rejected:2,saved:0});
  await expect(app.getByRole('heading',{name:'Your energy schedule'})).toBeVisible();
});

test('renewal times out without reloading and subsequent recovery still works',async({page})=>{
  let release!:()=>void;
  const harness=await homeAssistant(page,{holdRenewal:new Promise<void>(resolve=>{release=resolve;})}),{app}=harness;
  await page.clock.install();
  harness.expire();await visibility(app,false);
  await expect.poll(()=>harness.counts().renewals).toBe(1);
  await page.clock.runFor(10_001);
  await expect(app.getByRole('alert')).toContainText(recoveryMessage);
  expect(harness.counts()).toEqual({renewals:1,rejected:1,saved:0});
  release();
  await visibility(app,false);
  await expect(app.getByRole('alert')).toHaveCount(0);
  expect(harness.counts()).toEqual({renewals:2,rejected:2,saved:0});
});

test('server errors do not create a new ingress session',async({page})=>{
  const harness=await homeAssistant(page,{status:503}),{app}=harness;
  harness.expire();await visibility(app,false);
  await expect(app.getByRole('alert')).toContainText('App request failed (503)');
  expect(harness.counts()).toEqual({renewals:0,rejected:1,saved:0});
});

test('unavailable Home Assistant host reports the access problem without reloading',async({page})=>{
  await page.route('**/api/state',route=>route.fulfill({status:401}));
  await page.goto('./');
  await expect(page.getByRole('alert')).toContainText(recoveryMessage);
});
