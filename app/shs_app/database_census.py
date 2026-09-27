"""Read-only storage census; distinguish working databases from migration archives."""
from contextlib import closing
from pathlib import Path
import sqlite3
from time import perf_counter


def inspect_database(path, label):
    started = perf_counter()
    result = dict(name=label, file_bytes=path.stat().st_size,
                  wal_bytes=Path(str(path)+'-wal').stat().st_size if Path(str(path)+'-wal').exists() else 0,
                  shm_bytes=Path(str(path)+'-shm').stat().st_size if Path(str(path)+'-shm').exists() else 0)
    with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True)) as db:
        db.execute('BEGIN')
        names = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        sizes = {name:(size,pages) for name,size,pages in db.execute('SELECT name,sum(pgsize),count(*) FROM dbstat GROUP BY name')}
        result.update(schema_version=db.execute('PRAGMA user_version').fetchone()[0],
            free_pages=db.execute('PRAGMA freelist_count').fetchone()[0],
            page_size=db.execute('PRAGMA page_size').fetchone()[0],
            journal_mode=db.execute('PRAGMA journal_mode').fetchone()[0],
            tables=[dict(name=name,bytes=sizes.get(name,(0,0))[0],pages=sizes.get(name,(0,0))[1],
                rows=db.execute('SELECT count(*) FROM "'+name.replace('"','""')+'"').fetchone()[0]) for name in names])
        progress = 'progress' if 'progress' in names else 'transport' if 'transport' in names else None
        if progress:
            floor,high = db.execute('SELECT floor,high FROM '+progress).fetchone()
            result['receipts'] = dict(processed_through=floor,received_through=high,pending=high-floor)
        ranges = {}
        for table,column in (('resource_samples','sampled_at'),('traces','at_ms'),('observations','at_ms'),('meters','source_at_ms')):
            if table in names:
                lo,hi = db.execute('SELECT min('+column+'),max('+column+') FROM '+table).fetchone()
                ranges[table] = dict(first=lo,last=hi)
        result['retained_ranges'] = ranges
        if 'indexed_evidence_schema' in names:
            result['evidence_schema'],result['evidence_verified'] = db.execute('SELECT version,verified FROM indexed_evidence_schema').fetchone()
    result['census_ms'] = (perf_counter()-started)*1000
    return result


def census(data, runtime=None, entry=None, ha_config=Path('/homeassistant')):
    active = [(data/'diagnostics.sqlite3','Dashboard resources')]
    if runtime is not None and entry:
        active += [(runtime/'receipts.sqlite','App transport'),
            (runtime/'stores'/f'shs_energy.execution.{entry}.sqlite','Operational accounting'),
            (runtime/'stores'/f'shs_energy.verification.{entry}.sqlite','Controller diagnostics'),
            (ha_config/'.storage'/f'shs_energy.gateway.{entry}.sqlite','HA companion')]
    databases = []
    for path,label in active:
        if path.exists():
            try: databases.append(inspect_database(path,label))
            except (sqlite3.Error,OSError) as error: databases.append(dict(name=label,error=str(error)))
    live_paths = {p.resolve() for p,_ in active}
    archives = []
    for path in data.rglob('*'):
        if path.is_file() and not path.is_symlink() and path.resolve() not in live_paths and (path.suffix in ('.sqlite','.sqlite3','.pre-indexed') or path.name=='import.json'):
            archives.append(dict(name=str(path.relative_to(data)),bytes=path.stat().st_size))
    return dict(databases=databases,archives=archives,diagnostic_retention_days=3)
