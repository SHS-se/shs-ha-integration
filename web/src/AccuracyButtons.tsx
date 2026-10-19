import {useEffect, useId, useRef, useState} from 'react';
import type {AccuracyReport, InputAccuracy, InputCheck} from './types';

const CHECKS: Record<InputCheck['key'], string> = {prices:'Prices', solar_forecast:'Solar forecast', sensors:'Sensors', measurements:'Measurements', battery:'Battery'};
/** The cloud recalculates on new data, in five-minute steps. */
const UPDATES = 'Recalculated within five minutes of a new plan, measurement or published price.';

export function AccuracyButtons({report, error, input}: {report?: AccuracyReport|null; error?: string|null; input?: InputAccuracy|null}) {
  const [open,setOpen] = useState<'input'|'historical'|null>(null);
  const dialog = useRef<HTMLDialogElement>(null);
  const heading = useId();
  useEffect(()=>{if(open&&!dialog.current?.open)dialog.current?.showModal();},[open]);
  const labels = {input:'Input accuracy %',historical:'Historical accuracy %'};
  const value = (percentage: number|null|undefined) => percentage==null?'—':String(percentage);
  const timeZone = report?.timezone??Intl.DateTimeFormat().resolvedOptions().timeZone;
  const stamp = report?new Date(report.calculated_at).toLocaleString(undefined,{timeZone}):'';
  const scores = {input, historical:report?.historical};
  return <><div className="accuracy-buttons">
    {(['input','historical'] as const).map(kind=>{
      const score=scores[kind];
      return <button type="button" key={kind} title={kind==='historical'?error||stamp:''}
        onClick={()=>setOpen(kind)}>{labels[kind]} <strong className={score?.grade?`accuracy-${score.grade}`:''}>{value(score?.percentage)}</strong></button>;
    })}
  </div><dialog ref={dialog} className="accuracy-dialog" aria-labelledby={heading} onClose={()=>setOpen(null)}>
    <div className="accuracy-heading"><h2 id={heading}>{open?labels[open]:''}</h2>
      <button type="button" aria-label="Close" onClick={()=>dialog.current?.close()}>×</button></div>
    {open==='input'?<InputChecks input={input} value={value} timeZone={timeZone}/>:<>
      {error?<p role="status">{error}</p>:!report?.historical.devices.length?<p className="muted">Learning</p>:<ul>{report.historical.devices.map(row=><li key={row.key}>
        <span>{row.name}</span><strong className={row.grade?`accuracy-${row.grade}`:''}>{value(row.percentage)}{row.percentage!==null?'%':''}</strong>
      </li>)}</ul>}
      <div className="accuracy-schedule">{report&&<p>Calculated <time dateTime={report.calculated_at}>{stamp}</time></p>}
        <p>{UPDATES}</p></div></>}
  </dialog></>;
}

/** Live checks are healthy over expected readings; Prices is the cloud's estimate accuracy. */
function InputChecks({input, value, timeZone}: {input?: InputAccuracy|null; value: (p: number|null|undefined) => string; timeZone: string}) {
  if (!input?.checks.length) return <p className="muted">Waiting for the first input check</p>;
  const time = (iso: string) => new Date(iso).toLocaleString(undefined,{timeZone,weekday:'short',hour:'2-digit',minute:'2-digit'});
  const detail = (check: InputCheck) => check.quarters!==undefined
    ? (check.quarters?`Estimates against published prices, ${check.quarters} quarters${check.calculated_at?` · ${time(check.calculated_at)}`:''}`:'No published price compared yet')
    : `${check.healthy} of ${check.expected} ${check.key==='measurements'?'quarters complete':check.key==='battery'?'charge targets reached':'usable'}`;
  return <><ul>{input.checks.map(check=><li key={check.key} className="accuracy-check">
    <span><span>{CHECKS[check.key]??check.key}</span><small>{detail(check)}{check.quarters===undefined&&check.issues.length>0&&<>: {check.key==='measurements'||check.key==='battery'
      ?check.issues.map(time).join(', '):check.issues.join(', ')}</>}</small></span>
    <strong className={check.grade?`accuracy-${check.grade}`:''}>{value(check.percentage)}{check.percentage!==null?'%':''}</strong>
  </li>)}</ul>
    <div className="accuracy-schedule"><p>Checked <time dateTime={input.observed_at}>{new Date(input.observed_at).toLocaleString(undefined,{timeZone})}</time>. The headline averages the scored checks. A changed check is sent within five minutes.</p></div></>;
}
