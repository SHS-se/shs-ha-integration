// Use the same authenticated Supervisor API and cookie as Home Assistant's app panel.
// See frontend/src/data/hassio/ingress.ts in home-assistant/frontend.
type HomeAssistant = {callWS(message:Record<string,unknown>):Promise<{session:string}>};
type HomeAssistantRoot = Element & {hass?:HomeAssistant};

const recoveryError='Home Assistant access could not be renewed. Check your Home Assistant connection and sign in again if needed.';
let renewal:Promise<void>|undefined;
let sessionRevision=0;

async function createSession() {
  let timeout:ReturnType<typeof setTimeout>|undefined;
  try {
    const hass=(window.parent.document.querySelector('home-assistant') as HomeAssistantRoot|null)?.hass;
    if(!hass)throw new Error(recoveryError);
    const {session}=await Promise.race([
      hass.callWS({type:'supervisor/api',endpoint:'/ingress/session',method:'post'}),
      new Promise<never>((_,reject)=>{timeout=setTimeout(()=>reject(new Error(recoveryError)),10_000);}),
    ]);
    if(typeof session!=='string'||!session)throw new Error(recoveryError);
    document.cookie=`ingress_session=${encodeURIComponent(session)};path=/api/hassio_ingress/;SameSite=Strict${location.protocol==='https:'?';Secure':''}`;
    sessionRevision++;
  } catch {
    throw new Error(recoveryError);
  } finally {
    clearTimeout(timeout);
  }
}

export async function fetchApp(input:string,init?:RequestInit):Promise<Response> {
  const revision=sessionRevision;
  const response=await fetch(input,init);
  if(response.status!==401)return response;
  init?.signal?.throwIfAborted();
  // Share renewal across dashboard and configuration polls. A delayed 401 from
  // the previous session only needs a retry with the cookie already renewed.
  if(revision===sessionRevision) {
    renewal??=createSession().finally(()=>{renewal=undefined;});
    await renewal;
  }
  init?.signal?.throwIfAborted();
  const retried=await fetch(input,init);
  if(retried.status===401)throw new Error(recoveryError);
  return retried;
}
