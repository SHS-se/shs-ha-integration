import {useEffect, useRef} from 'react';
import './configuration-editor.js';
import {AppAccessError, fetchApp} from './ingress';

type Editor = HTMLElement & {backend:typeof backend;dark:boolean;openCorrection:(query:string)=>void};
const backend = {
  async request(body:Record<string,unknown>) {
    const {action,...data}=body;
    let response:Response;
    try { response=await fetchApp(`./api/configuration/${action}`,{
      method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data),
    }); } catch(error) {
      if(error instanceof AppAccessError)throw error;
      throw Object.assign(new Error(String(error)),{code:'connection_unavailable'});
    }
    if(response.status>=500)throw Object.assign(new Error(`App request failed (${response.status})`),{code:'connection_unavailable'});
    if(!response.ok) {
      const content=await response.text();
      let detail;
      try {detail=JSON.parse(content);} catch {throw new Error(content);}
      throw Object.assign(new Error(detail.message),detail);
    }
    return response.json();
  },
  download() {return fetchApp('./api/diagnostics/controller.json.gz',{cache:'no-store'});},
};

export function Configuration({dark}:{dark:boolean}) {
  const mount=useRef<HTMLDivElement>(null),editor=useRef<Editor|null>(null);
  useEffect(()=>{
    let active=true;
    const correction=()=>editor.current?.openCorrection(location.hash.split('?')[1]||'');
    window.addEventListener('hashchange',correction);
    void customElements.whenDefined('shs-configuration-editor').then(()=>{
      if(!active)return;
      const element=document.createElement('shs-configuration-editor') as Editor;
      element.backend=backend;element.dark=dark;editor.current=element;
      mount.current?.append(element);
    });
    return()=>{active=false;window.removeEventListener('hashchange',correction);editor.current?.remove();editor.current=null;};
  },[]);
  useEffect(()=>{if(editor.current)editor.current.dark=dark;},[dark]);
  return <div ref={mount} className="configuration-editor"/>;
}
