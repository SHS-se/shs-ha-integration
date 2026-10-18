import {useEffect, useId, useRef, useState} from 'react';
import type {AccuracyReport, InputAccuracy, InputCheck} from './types';

const CHECKS: Record<InputCheck['key'], string> = {prices:'Prices', solar_forecast:'Solar forecast', sensors:'Sensors', measurements:'Measurements'};

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
      return <button type="button" key={kind} title={kind==='historical'?error||(report?`${stamp} · ${report.forecast_plan_id??''}`:''):''}
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
        <p>{nextUpdate(Date.now(),timeZone)} Updates at 06:00 and 18:00.</p></div></>}
  </dialog></>;
}

/** Live input health: each check is healthy readings over expected readings. */
function InputChecks({input, value, timeZone}: {input?: InputAccuracy|null; value: (p: number|null|undefined) => string; timeZone: string}) {
  if (!input?.checks.length) return <p className="muted">Waiting for the first input check</p>;
  const time = (iso: string) => new Date(iso).toLocaleString(undefined,{timeZone,weekday:'short',hour:'2-digit',minute:'2-digit'});
  const detail = (check: InputCheck) => check.key==='prices'
    ? (check.published_until?`Published to ${time(new Date(Date.parse(check.published_until)+900_000).toISOString())}`:'No published prices')
    : `${check.healthy} of ${check.expected} ${check.key==='measurements'?'quarters complete':'usable'}`;
  return <><ul>{input.checks.map(check=><li key={check.key} className="accuracy-check">
    <span><span>{CHECKS[check.key]??check.key}</span><small>{detail(check)}{check.key!=='prices'&&check.issues.length>0&&<>: {check.key==='measurements'
      ?check.issues.map(time).join(', '):check.issues.join(', ')}</>}</small></span>
    <strong className={check.grade?`accuracy-${check.grade}`:''}>{value(check.percentage)}%</strong>
  </li>)}</ul>
    <div className="accuracy-schedule"><p>Checked <time dateTime={input.observed_at}>{new Date(input.observed_at).toLocaleString(undefined,{timeZone})}</time>. The headline averages these checks.</p></div></>;
}

/** The cloud publishes a report at local 06:00 and 18:00 in the home's time zone. */
function nextUpdate(now: number, timeZone: string) {
  const hour=Number(new Intl.DateTimeFormat('en-GB',{timeZone,hour:'2-digit',hourCycle:'h23'}).formatToParts(now).find(part=>part.type==='hour')!.value);
  return hour<6?'Next update today at 06:00.':hour<18?'Next update today at 18:00.':'Next update tomorrow at 06:00.';
}
