"""Isolated read-only acceptance. No migrations, metadata writes or publication.

Run inside an environment with configured Executor and model access. --tables
selects an explicitly reported representative subset, not a production rebuild.
"""
import argparse
import asyncio
import json
import os
from types import SimpleNamespace

import httpx
import psycopg
from app.datasource_modeling import ModelingRepository, ModelingService
from app.model_settings import ModelSettings
from app.relationship_inputs import Checkpoints


async def evaluate(names, fields=None):
    ModelSettings()  # Reads model configuration; never calls save.
    with psycopg.connect(os.environ['METADATA_DATABASE_URL'], options='-c default_transaction_read_only=on') as connection:
        sources = connection.execute('SELECT id,type,secret_ref FROM semora_meta.datasources ORDER BY created_at').fetchall()
        records = {}
        table_maps = {}
        for identifier, kind, secret in sources:
            rows = connection.execute('SELECT object_name,table_id,catalog_name,schema_name,columns,primary_keys,unique_constraints,foreign_keys,indexes,description,object_kind FROM semora_meta.datasource_tables WHERE datasource_id=%s', (identifier,)).fetchall()
            tables = [{k: v for k, v in zip(('name','id','catalog','schema','columns','primary_keys','unique_constraints','foreign_keys','indexes','description','kind'), row)} for row in rows if not names or row[0] in names]
            if fields:
                for table in tables:
                    table['columns'] = [c for c in table['columns'] if c['name'] in fields]
            if not tables: continue
            records[identifier] = SimpleNamespace(id=identifier, type=kind, secret_ref=secret, scan_result={'tables': tables})
            table_maps[identifier] = {t['id']: t for t in tables}
    if len(table_maps) != 1:
        raise ValueError('Representative acceptance requires exactly one selected data source')
    async def request(method, path, **kwargs):
        async with httpx.AsyncClient(timeout=40) as client:
            r = await client.request(method, os.environ['EXECUTOR_URL'].rstrip('/')+'/internal/v1/'+path,
                headers={'X-Semora-Internal-Token':os.getenv('INTERNAL_TOKEN','semora-dev-internal')}, **kwargs)
            r.raise_for_status()
            return r.json()
    repo = ModelingRepository(dsn='')
    job = repo.create_job(next(iter(records)))
    service = ModelingService(SimpleNamespace(repository=SimpleNamespace(get=records.__getitem__)), request, repository=repo)
    candidates = []
    error = None
    try:
        await service._semantic_review(candidates, table_maps, job)
    except Exception as exc:
        error = type(exc).__name__ + ': ' + str(exc)
    accepted = [c for c in candidates if c['status']=='accepted'] if not error else []
    path = Checkpoints().path(job['id'])
    state = json.loads(path.read_text()) if path.exists() else {}
    return {'subset': sorted(names) if names else 'all', 'tables': sum(map(len, table_maps.values())),
            'field_subset': sorted(fields) if fields else 'all', 'technical_error': error, 'metrics': state.get('metrics',{}), 'accepted': accepted,
            'not_generated': [{'id':c['id'],'source_table':c['source_table_id'],'target_table':c['target_table_id'],
                'key_pairs':c['key_pairs'],'reasons':next((e.get('reasons',[]) for e in reversed(c['evidence']) if e.get('kind')=='confirmation'),[])}
                for c in candidates if c['status']!='accepted'],
            'note': 'Read-only representative synthetic-data acceptance; no production ontology publication.'}


if __name__ == '__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--tables',nargs='*',default=[])
    parser.add_argument('--fields',nargs='*',default=[])
    args=parser.parse_args()
    print(json.dumps(asyncio.run(evaluate(set(args.tables), set(args.fields))),ensure_ascii=False,default=str,indent=2))
