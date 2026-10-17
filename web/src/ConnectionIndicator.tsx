import {useEffect, useState} from 'react';
import {AppAccessError, fetchApp} from './ingress';
import type {State, ConnectionStatus} from './types';

// The server owns cloud/HA outages. Only the browser can observe its app link.
export function useAppConnection(receive:(state:State)=>void) {
  const [connection,setConnection]=useState<ConnectionStatus&{error:string}>({state:'connecting',retry_at:null,persistent:false,error:''});
  useEffect(()=>{
    const controller=new AbortController();let active=true,pending=false,timer:ReturnType<typeof setTimeout>,promotion:ReturnType<typeof setTimeout>|undefined,firstFailure:number|null=null,observed=0,ceiling=5;
    const update=async()=>{
      if(document.hidden||pending)return;
      clearTimeout(timer);pending=true;
      setConnection(previous=>({...previous,state:'connecting',retry_at:null}));
      try {
        const response=await fetchApp('./api/state',{signal:AbortSignal.any([controller.signal,AbortSignal.timeout(10000)]),cache:'no-store'});
        if(!response.ok)throw new Error(`App request failed (${response.status})`);
        const next:State=await response.json();
        if(!active)return;
        receive(next);firstFailure=null;observed=0;ceiling=5;clearTimeout(promotion);
        setConnection({state:'connected',retry_at:null,persistent:false,error:''});
        timer=setTimeout(update,15000);
      } catch(error) {
        if(!active)return;
        console.warn('SHS app connection failed',error);
        if(firstFailure===null){
          firstFailure=performance.now();
          promotion=setTimeout(()=>{if(active)setConnection(previous=>({...previous,persistent:true}));},Math.max(0,3600000-observed));
        }
        const delay=ceiling*(.8+Math.random()*.2);ceiling=Math.min(60,ceiling*2);
        setConnection({state:'waiting',retry_at:new Date(Date.now()+delay*1000).toISOString(),persistent:error instanceof AppAccessError||observed+performance.now()-firstFailure>=3600000,error:error instanceof Error?error.message:'App connection lost'});
        timer=setTimeout(update,delay*1000);
      } finally {pending=false;}
    };
    const visibility=()=>{
      if(document.hidden){
        clearTimeout(timer);clearTimeout(promotion);
        if(firstFailure!==null){observed+=performance.now()-firstFailure;firstFailure=null;}
      }else void update();
    };
    document.addEventListener('visibilitychange',visibility);
    void update();return()=>{active=false;controller.abort();clearTimeout(timer);clearTimeout(promotion);document.removeEventListener('visibilitychange',visibility);};
  },[receive]);
  return connection;
}

export function ConnectionIndicator({connection}:{connection:ConnectionStatus|null}) {
  const [now,setNow]=useState(Date.now());
  useEffect(()=>{const timer=setInterval(()=>setNow(Date.now()),1000);return()=>clearInterval(timer);},[]);
  const status=connection?.state??'connecting';
  const seconds=connection?.retry_at?Math.max(0,Math.ceil((Date.parse(connection.retry_at)-now)/1000)):0;
  const label=connection?.persistent?'Connection error':status==='connected'?'Connected':status==='waiting'&&seconds>0?`${seconds}s`:status==='disconnecting'?'Disconnecting…':'Connecting…';
  return <span className={`connection-indicator ${connection?.persistent?'error':status==='connected'?'online':''}`} aria-label="Cloud connection" title={status==='waiting'?`Next connection in ${seconds} seconds`:label}>
    <span className="connection-light" aria-hidden="true"/><span>{label}</span>
  </span>;
}
