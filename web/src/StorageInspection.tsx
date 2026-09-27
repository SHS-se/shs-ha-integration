import type {StorageCensus} from './types';
const size=(n:number)=>`${(n/1048576).toLocaleString(undefined,{maximumFractionDigits:2})} MB`;
export function StorageInspection({value}:{value:StorageCensus|undefined}) {
  if(!value)return null;
  return <section className="card"><h2>Active storage</h2><p className="muted">Detailed diagnostics are kept for up to {value.diagnostic_retention_days} days, with additional count limits. Operational facts needed for accounting and late corrections have a separate lifetime.</p>
    {value.databases.map(db=><details key={db.name}><summary>{db.name} · {db.file_bytes===undefined?'Unavailable':size(db.file_bytes)}</summary>{db.error?<p className="notice error">{db.error}</p>:<>
      <p>Schema {db.schema_version} · {db.journal_mode} journal · {db.free_pages} reusable pages · WAL {size(db.wal_bytes||0)} · Shared memory {size(db.shm_bytes||0)} · Census {db.census_ms?.toFixed(1)} ms</p>
      {db.receipts&&<p>Received through {db.receipts.received_through} · Processed through {db.receipts.processed_through} · Pending {db.receipts.pending}</p>}
      <div className="table-wrap"><table><thead><tr><th>Table</th><th>Rows</th><th>Pages</th><th>Size</th></tr></thead><tbody>{db.tables?.map(t=><tr key={t.name}><td>{t.name}</td><td>{t.rows.toLocaleString()}</td><td>{t.pages}</td><td>{size(t.bytes)}</td></tr>)}</tbody></table></div>
      {db.retained_ranges&&Object.entries(db.retained_ranges).map(([name,range])=><p key={name}>{name}: {range.first===null?'Empty':`${typeof range.first==='number'?new Date(range.first).toLocaleString():range.first} — ${typeof range.last==='number'?new Date(range.last).toLocaleString():range.last}`}</p>)}
    </>}</details>)}
    <details><summary>Offline migration archives · {size(value.archives.reduce((sum,row)=>sum+row.bytes,0))}</summary><p>Preserved recovery evidence; these files are not read by the active controller.</p>{value.archives.map(row=><p key={row.name}>{row.name} · {size(row.bytes)}</p>)}</details>
    {value.operations&&<details><summary>Operational storage measurements</summary><pre>{JSON.stringify(value.operations,null,2)}</pre></details>}
  </section>;
}
