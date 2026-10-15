import type {Attention} from './types';

export function AttentionNotice({items}:{items:Attention[]}) {
  if(!items.length)return null;
  const critical=items.some(item=>item.severity==='error');
  return <section className={`notice operational-attention ${critical?'critical':''}`} aria-label="SHS needs attention" role={critical?'alert':'status'}>
    <h2>SHS needs attention</h2>
    {items.map(item=><article className="issue" key={item.key} data-stage={item.stage}>
      <h3>{item.title||'Operation needs attention'}</h3><p>{item.detail}</p>
      {item.items&&item.items.length>0&&<ul>{item.items.map((text,index)=><li key={index}>{text}</li>)}</ul>}
      {item.next_step&&<p>{item.next_step}</p>}
      <div className="issue-actions">
        {item.fix?.fields?.map(field=><a className="button" key={field.url} href={field.url}>{field.message||`Fix ${field.key.replaceAll('_',' ')}`}</a>)}
        {item.fix?.kind==='entity'&&item.fix.entity_id&&<a className="button" href={'#settings?entity='+encodeURIComponent(item.fix.entity_id)}>Inspect source entity</a>}
        {item.fix?.kind==='entities'&&item.fix.entity_ids?.map(entity=><a className="button" key={entity} href={'#settings?entity='+encodeURIComponent(entity)}>Inspect {entity}</a>)}
        {item.fix?.url&&<a className="button" href={item.fix.url} target="_blank" rel="noreferrer">Open website settings</a>}
        {!item.fix?.fields?.length&&!['entity','entities'].includes(item.fix?.kind||'')&&!item.fix?.url&&<a className="button" href="#settings?tab=status">Inspect status</a>}
        {item.fix?.kind==='diagnostics'&&<><a className="button" href="./api/diagnostics/controller.json.gz" download>Download controller diagnostics</a><a className="button" href="./api/diagnostics/app-logs.zip" download>Download app logs</a></>}
      </div>
    </article>)}
  </section>;
}
