import {useEffect, useRef} from 'react';
import './configuration-editor.js';

type Editor = HTMLElement & {backend:typeof backend;dark:boolean};
const backend = {
  async request(body:Record<string,unknown>) {
    const {action,...data}=body;
    const response=await fetch(`./api/configuration/${action}`,{
      method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data),
    });
    if(!response.ok) {
      const content=await response.text();
      let detail;
      try {detail=JSON.parse(content);} catch {throw new Error(content);}
      throw Object.assign(new Error(detail.message),detail);
    }
    return response.json();
  },
  download() {return fetch('./api/diagnostics/controller.json.gz',{cache:'no-store'});},
};

export function Configuration({dark}:{dark:boolean}) {
  const mount=useRef<HTMLDivElement>(null),editor=useRef<Editor|null>(null);
  useEffect(()=>{
    let active=true;
    void customElements.whenDefined('shs-configuration-editor').then(()=>{
      if(!active)return;
      const element=document.createElement('shs-configuration-editor') as Editor;
      element.backend=backend;element.dark=dark;editor.current=element;
      mount.current?.append(element);
    });
    return()=>{active=false;editor.current?.remove();editor.current=null;};
  },[]);
  useEffect(()=>{if(editor.current)editor.current.dark=dark;},[dark]);
  return <div ref={mount} className="configuration-editor"/>;
}
