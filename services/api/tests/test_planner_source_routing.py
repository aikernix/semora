import asyncio
import httpx
import pytest
from app.workflow import Workflow
from semora_contracts import QueryPlan


def test_single_source_query_in_multi_source_domain_selects_local_mdl(monkeypatch):
    ontology={'dataSourceId':'a','objects':[{'name':'first','dataSourceId':'a','properties':[]},{'name':'second','dataSourceId':'b','properties':[]}], 'relations':[], 'metrics':[], '_wrenArtifact':{'sourceProjects':{'a':{'catalog':'a'},'b':{'catalog':'b'}}}}
    plan=QueryPlan(domain='example',data_source_id='b',intent='one source',metrics=[])
    calls=[]
    def handler(request):
        import json
        body=json.loads(request.content); calls.append(body)
        assert body['ontology']['dataSourceId']=='b'
        assert [o['name'] for o in body['ontology']['objects']]==['second']
        assert body['ontology']['_wrenArtifact']=={'catalog':'b'}
        return httpx.Response(200,json={'semantic_sql':'SELECT 1','physical_sql':'SELECT 1','artifact_hash':'test'})
    client=httpx.AsyncClient
    monkeypatch.setattr(httpx,'AsyncClient',lambda **kwargs:client(transport=httpx.MockTransport(handler),**kwargs))
    monkeypatch.setenv('SEMANTIC_PLANNER_URL','http://planner')
    result=asyncio.run(object.__new__(Workflow)._compile(plan,ontology))
    assert result.physical_sql=='SELECT 1' and len(calls)==1


@pytest.mark.parametrize('fallback',['true','false'])
def test_planner_422_preserves_reason_and_never_falls_back(monkeypatch,fallback):
    client=httpx.AsyncClient
    monkeypatch.setattr(httpx,'AsyncClient',lambda **kwargs:client(transport=httpx.MockTransport(lambda request:httpx.Response(422,json={'detail':'invalid semantic model'})),**kwargs))
    monkeypatch.setenv('SEMANTIC_PLANNER_URL','http://planner')
    monkeypatch.setenv('ALLOW_LOCAL_PLANNER_FALLBACK',fallback)
    with pytest.raises(ValueError,match='invalid semantic model'):
        asyncio.run(object.__new__(Workflow)._compile(QueryPlan(domain='test',intent='test',metrics=[]),{}))
