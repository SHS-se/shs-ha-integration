import {useEffect, useId, useRef, useState} from 'react';
import type {AccuracyReport} from './types';

export function AccuracyButtons({report, error}: {report?: AccuracyReport|null; error?: string|null}) {
  const [open,setOpen] = useState<'forecast'|'historical'|null>(null);
  const dialog = useRef<HTMLDialogElement>(null);
  const heading = useId();
  useEffect(()=>{if(open&&!dialog.current?.open)dialog.current?.showModal();},[open]);
  const labels = {forecast:'Forecast accuracy %',historical:'Historical accuracy %'};
  const value = (percentage: number|null|undefined) => percentage==null?'—':String(percentage);
  const stamp = report?new Date(report.calculated_at).toLocaleString(undefined,{timeZone:report.timezone}):'';
  return <><div className="accuracy-buttons">
    {(['forecast','historical'] as const).map(kind=>{
      const score=report?.[kind];
      return <button type="button" key={kind} title={error||(report?`${stamp} · ${report.forecast_plan_id??''}`:'')}
        onClick={()=>setOpen(kind)}>{labels[kind]} <strong className={score?.grade?`accuracy-${score.grade}`:''}>{value(score?.percentage)}</strong></button>;
    })}
  </div><dialog ref={dialog} className="accuracy-dialog" aria-labelledby={heading} onClose={()=>setOpen(null)}>
    <div className="accuracy-heading"><h2 id={heading}>{open?labels[open]:''}</h2>
      <button type="button" aria-label="Close" onClick={()=>dialog.current?.close()}>×</button></div>
    {error?<p role="status">{error}</p>:!(open&&report?.[open].devices.length)?<p className="muted">Learning</p>:<ul>{(open?report?.[open].devices:[])?.map(row=><li key={row.key}>
      <span>{row.name}</span><strong className={row.grade?`accuracy-${row.grade}`:''}>{value(row.percentage)}{row.percentage!==null?'%':''}</strong>
    </li>)}</ul>}
    {report&&<time dateTime={report.calculated_at}>{stamp}</time>}
  </dialog></>;
}
