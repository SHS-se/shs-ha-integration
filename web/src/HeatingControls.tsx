import {useEffect, useRef, useState} from 'react';
import {fetchApp} from './ingress';

type Zone = {
  name: string; planned_target_c: number | null;
  min_c: number | null; max_c: number | null; step: number;
  applied_c: number | null; expires_at: string | null;
  minutes: number; controlling: boolean;
};
type Controls = {zones: Record<string, Zone>; loads: Record<string, string>; order: string[]};
type Edit = {owner: string; field: 'target' | 'minutes' | 'resume'; value: number | null}
  | {owner: null; field: 'order'; value: string[]};

function NumberEdit({label, value, min, max, step, disabled, commit}: {
  label: string; value: number | null; min?: number; max?: number;
  step: number; disabled: boolean; commit: (value: number) => void;
}) {
  const [draft, setDraft] = useState<string | null>(null);
  return <form className="toolbar" onSubmit={event => {
    event.preventDefault();
    if (draft !== null && draft !== '') commit(Number(draft));
    setDraft(null);
  }}>
    <label>{label}
      <input type="number" value={draft ?? value ?? ''} min={min} max={max} step={step}
        disabled={disabled} required onChange={event => setDraft(event.target.value)}/>
    </label>
    <button type="submit" disabled={disabled || draft === null}>Apply</button>
  </form>;
}

export function HeatingControls() {
  const [controls, setControls] = useState<Controls | null>(null);
  const [saving, setSaving] = useState(false);
  const [issue, setIssue] = useState('');
  const edits = useRef(0);
  const writing = useRef(false);
  useEffect(() => {
    const abort = new AbortController();
    async function refresh() {
      if (writing.current) return;
      const revision = edits.current;
      try {
        const response = await fetchApp('api/heating', {signal: abort.signal});
        if (!response.ok) return;
        const next: Controls = await response.json();
        if (!abort.signal.aborted && revision === edits.current) setControls(next);
      } catch {
        // The app connection indicator already reports connection changes.
      }
    }
    void refresh();
    const timer = window.setInterval(refresh, 3000);
    return () => { abort.abort(); window.clearInterval(timer); };
  }, []);
  async function change(edit: Edit) {
    if (writing.current) return;
    writing.current = true;
    edits.current += 1;
    setSaving(true);
    setIssue('');
    try {
      const response = await fetchApp('api/heating', {
        method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(edit),
      });
      if (!response.ok) {
        const failure = response.headers.get('Content-Type')?.includes('application/json')
          ? (await response.json()).message : 'SHS could not save this change. Try again when connected.';
        throw new Error(failure);
      }
      setControls(await response.json());
    } catch (error) {
      setIssue(error instanceof Error ? error.message : 'SHS could not save this change.');
    } finally {
      writing.current = false;
      setSaving(false);
    }
  }
  if (!controls) return null;
  const order = controls.order.filter(owner => owner in controls.loads);
  const unused = Object.entries(controls.loads).filter(([owner]) => !order.includes(owner));
  function orderChanged(next: string[]) {
    void change({owner: null, field: 'order', value: next});
  }
  function move(index: number, offset: number) {
    const next = [...order];
    const other = index + offset;
    [next[index], next[other]] = [next[other], next[index]];
    orderChanged(next);
  }

  return <>
    <section className="card">
      <h2>Room heating</h2>
      {issue && <p role="alert">{issue}</p>}
      {Object.entries(controls.zones).length === 0 && <p>No temperature-controlled heating zones.</p>}
      {Object.entries(controls.zones).map(([owner, zone]) => {
        const remaining = zone.expires_at
          ? Math.max(0, Math.ceil((Date.parse(zone.expires_at) - Date.now()) / 60000))
          : zone.minutes;
        const disabled = saving || !zone.controlling;
        return <div className="controller-row" key={owner}>
          <h3>{zone.name}</h3>
          <NumberEdit label="Setpoint temperature (°C)" value={zone.applied_c} min={zone.min_c ?? undefined}
            max={zone.max_c ?? undefined} step={zone.step} disabled={disabled}
            commit={value => void change({owner, field: 'target', value})}/>
          <NumberEdit label={zone.expires_at ? 'Remaining minutes' : 'Override duration (minutes)'} value={remaining}
            min={1} step={1} disabled={disabled} commit={value => void change({owner, field: 'minutes', value})}/>
          {zone.expires_at && <button type="button" disabled={disabled}
            onClick={() => void change({owner, field: 'resume', value: null})}>Return to plan</button>}
          {!zone.controlling && <p className="muted">Enable Controlling to change this room’s setpoint.</p>}
          {zone.planned_target_c != null && <details>
            <summary>Plan details</summary>
            <p className="muted">Planned setpoint {zone.planned_target_c} °C</p>
          </details>}
        </div>;
      })}
    </section>
    <section className="card">
      <h2>Load flexibility</h2>
      <p className="muted">If an override needs more power, SHS adjusts eligible loads from the top of this list first.</p>
      {order.length === 0 && <p>No loads are ranked for reduction.</p>}
      <ol>
        {order.map((owner, index) => <li key={owner}>
          <div className="toolbar">
            <span>{controls.loads[owner]}</span>
            <button type="button" disabled={saving || index === 0} onClick={() => move(index, -1)}
              aria-label={`Move ${controls.loads[owner]} earlier`}>Earlier</button>
            <button type="button" disabled={saving || index === order.length - 1} onClick={() => move(index, 1)}
              aria-label={`Move ${controls.loads[owner]} later`}>Later</button>
            <button type="button" disabled={saving} onClick={() => orderChanged(order.filter(value => value !== owner))}
              aria-label={`Remove ${controls.loads[owner]}`}>Remove</button>
          </div>
        </li>)}
      </ol>
      {unused.length > 0 && <label>Add flexible load
        <select value="" disabled={saving} onChange={event => orderChanged([...order, event.target.value])}>
          <option value="" disabled>Choose a load</option>
          {unused.map(([owner, name]) => <option key={owner} value={owner}>{name}</option>)}
        </select>
      </label>}
    </section>
  </>;
}
