"""Domain references and SQL-only requests for the optional offline extension."""
import asyncio
import os
from typing import Annotated

import httpx
from fastapi import APIRouter, HTTPException, Path, Response
from pydantic import BaseModel, Field

from .ontology import OntologyCoreError, ontology_service

router = APIRouter(prefix='/api/v1')
OfflineID = Annotated[str, Path(pattern=r'^[a-f0-9]{32}$')]
binding_lock = asyncio.Lock()


async def offline_request(method, path, *, timeout=8, **kwargs):
    endpoint = os.getenv('OFFLINE_MODELING_URL', '').rstrip('/')
    if os.getenv('OFFLINE_MODELING_ENABLED', 'true').lower() != 'true' or not endpoint:
        raise HTTPException(503, '离线建模扩展未启用')
    password = os.getenv('OFFLINE_ACCESS_PASSWORD')
    auth = ('offline', password) if password else None
    try:
        async with httpx.AsyncClient(timeout=timeout, auth=auth) as client:
            result = await client.request(method, endpoint + path, **kwargs)
        if not result.is_success:
            if result.status_code in {404, 409, 422, 504}:
                raise HTTPException(result.status_code, result.json()['detail'])
            raise HTTPException(502, '离线建模服务暂时不可用')
        return result.json() if result.content else None
    except httpx.TimeoutException as exc:
        raise HTTPException(504, '离线建模服务响应超时') from exc
    except (httpx.HTTPError, KeyError, ValueError) as exc:
        raise HTTPException(502, '离线建模服务暂时不可用') from exc


async def ready_offline_sources():
    return [source for source in await offline_request('GET','/v1/imports?summary=true') if source['status']=='ready']


async def domain_draft(domain):
    try:
        return await ontology_service.get_draft(domain)
    except OntologyCoreError as exc:
        raise HTTPException(exc.status_code, str(exc)) from exc


def domain_source_mode(domain):
    offline = domain.get('offlineSourceIds') or []
    online = bool(domain.get('sourcePackageRefs') or domain.get('objects') or domain.get('dataSourceId'))
    if offline:
        return 'conflict' if online or len(offline) > 1 else 'offline'
    return 'online' if online else 'unbound'


async def query_catalog(reference, ontology, run):
    """Pin the selected imported catalog as the workflow's query snapshot."""
    import sqlglot
    from sqlglot import exp
    from .datasource_modeling import package_objects
    from .run_state import digest

    source_id = run.offline_source_id
    if not source_id or source_id not in (ontology.get('offlineSourceIds') or []):
        raise ValueError('该离线数据源未绑定当前业务域')
    if domain_source_mode(ontology) != 'offline':
        raise ValueError('请先在本体工作室解除不兼容的数据源绑定')
    allowed = run.authorization.allowed_data_source_ids if run.authorization else None
    if allowed is not None and source_id not in allowed:
        raise ValueError('当前用户无权查询该数据源')
    try:
        data = await offline_request('GET',f'/v1/imports/{source_id}')
    except HTTPException as exc:
        raise ValueError(exc.detail) from exc
    if data['status'] != 'ready' or not data.get('tables'):
        raise ValueError('离线数据源尚未导入完成')
    config = data['config']
    tables = [{**item,'schema':item['namespace'],'description':item['comment'],
               'primary_keys':[{'columns':item['primary_key']}] if item['primary_key'] else [],
               'unique_constraints':[{'columns':key} for key in item['unique']]}
              for item in data['tables'].values()]
    objects, mapping = package_objects(source_id,tables)
    relations = []
    for identity, relation in data['relations'].items():
        if relation['status'] != 'active':
            continue
        left = mapping[relation['source']]
        right = mapping[relation['target']]
        condition = exp.and_(*(exp.EQ(this=exp.column(left[a],table=left['object']),
                                     expression=exp.column(right[b],table=right['object']))
                               for a,b in relation['pairs']))
        if relation['condition']:
            guards = sqlglot.parse_one(relation['condition'],read=config['dialect'])
            def bind_column(node):
                if not isinstance(node, exp.Column):
                    return node
                fields = left if node.table=='source' else right
                return exp.column(fields[node.name],table=fields['object'],quoted=True)
            condition = exp.and_(condition,guards.transform(bind_column))
        relations.append({'name':'r_'+identity,'label':'表间关联','from':left['object'],'to':right['object'],
                          'cardinality':relation['cardinality'],'condition':condition.sql(dialect='postgres')})
    profile = {'id':source_id,'name':data['name'],'type':config['product'],
               'vendor':config['product_label'],'version':config['version'],'dialect':config['dialect'],
               'compatibility_mode':config['dialect'],'features':{},'capabilities':{}}
    snapshot = {**ontology,'dataSourceId':source_id,'sourcePackageRefs':[],
                'objects':objects,'relations':relations,'metrics':[],
                'defaultFilters':[],'sourceScopes':{},'scopeField':'','timeField':'',
                'offlineProfile':profile,'offlineTables':tables}
    run.agent_memory['offline_source'] = [profile]
    reference = reference.model_copy(update={'data_source_id':source_id,
        'content_hash':'sha256:'+digest(snapshot),'artifact_version':'offline-schema'})
    return reference,snapshot


@router.get('/ontology/offline-sources')
async def list_sources():
    return await ready_offline_sources()


@router.post('/ontology/configurations/{domain}/offline-sources/{identifier}')
async def bind_source(domain: str, identifier: OfflineID):
    async with binding_lock:
        if not any(source['id']==identifier for source in await ready_offline_sources()):
            raise HTTPException(404, '离线数据源不存在或尚未导入完成')
        try:
            return await ontology_service.bind_offline_source(domain,identifier)
        except OntologyCoreError as exc:
            raise HTTPException(exc.status_code, str(exc)) from exc


@router.delete('/ontology/offline-sources/{identifier}', status_code=204)
async def delete_source(identifier: OfflineID):
    async with binding_lock:
        try:
            drafts = await ontology_service.list_drafts()
        except OntologyCoreError as exc:
            raise HTTPException(exc.status_code, str(exc)) from exc
        if any(identifier in (draft.get('offlineSourceIds') or []) for draft in drafts):
            raise HTTPException(409,'该数据源仍绑定业务域，请先解除绑定')
        await offline_request('DELETE',f'/v1/imports/{identifier}')
    return Response(status_code=204)


class SQLRequest(BaseModel):
    source_id: str = Field(pattern=r'^[a-f0-9]{32}$')
    question: str = Field(min_length=1,max_length=1500)


@router.post('/domains/{domain}/offline-sql')
async def generate_offline_sql(domain: str, body: SQLRequest):
    draft = await domain_draft(domain)
    if body.source_id not in (draft.get('offlineSourceIds') or []):
        raise HTTPException(404,'该离线数据源未绑定当前业务域')
    if domain_source_mode(draft) != 'offline':
        raise HTTPException(409, '请先在本体工作室解除不兼容的数据源绑定')
    return await offline_request('POST',f'/v1/imports/{body.source_id}/sql',
                                 timeout=650,json={'question':body.question})
