"""Research context and handoff regressions for the SQL-native workflow."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app import prepared_context
from app.continuation import handoff
from app.ontology_paths import connect_objects
from app.research_tools import ResearchTools
from semora_contracts import QueryRun, ConfirmedFact
from test_sql_native import native_harness


def test_topology_includes_required_bridge_and_rejects_disconnected():
    import pytest
    relations=[{'name':'ab','from':'a','to':'b'},{'name':'bc','from':'b','to':'c'}]
    assert set(connect_objects({'a','c'},relations))=={'ab','bc'}
    with pytest.raises(ValueError):connect_objects({'a','missing'},relations)


def test_keyword_requests_are_independent_and_knowledge_failure_does_not_block(monkeypatch):
    from helpers import harness
    w,session,_,_=harness(monkeypatch)
    run=QueryRun(session_id=session.id,domain='test',question='Query totals')
    _,ont=asyncio.run(w.snapshot_loader('test'));ont['knowledgeBaseIds']=['kb']
    w.model=SimpleNamespace(complete_json=AsyncMock(return_value={'knowledge_keywords':['total','status','total']}))
    search=AsyncMock(side_effect=[RuntimeError('offline'),[]])
    monkeypatch.setattr(prepared_context.knowledge_service,'_wiki_search',search)
    saved=asyncio.run(prepared_context.prepare_context(w,run,run.question,ont,{}))
    assert [c.args for c in search.await_args_list]==[('kb','total'),('kb','status')]
    assert saved['packet']['selected_objects']==['item']
    assert saved['packet']['knowledge']==[]
    assert 'knowledge_warning' in saved['packet']


def test_verbatim_excerpt_preserves_matching_constraints_and_marks_partial():
    text='Introduction\n\n'+('Unrelated paragraph '*200)+'\n\nRequired status = 2.\n\nOther notes'
    value=prepared_context.knowledge_excerpt(text,['status'],limit=200)
    assert 'Required status = 2.' in value['content'] and value['excerpted']
    assert value['original_chars']==len(text)


def test_research_page_is_available_to_sql_role_without_second_search(monkeypatch):
    from app.sql_authoring import material
    w,session,_,_=__import__('helpers').harness(monkeypatch)
    run=QueryRun(session_id=session.id,domain='test',question='Query records')
    state={'packet':{'objects':[],'knowledge':[]}}
    run.agent_memory['prepared_context']=[state]
    tools=ResearchTools(w,run,{'knowledgeBaseIds':['kb']},{})
    tools.context_state=state
    page={'slug':'definitions','title':'Definitions','content':'A valid record has status = 2.'}
    tools.remember_page('kb',page);tools.remember_page('kb',page)
    assert len(state['packet']['knowledge'])==1
    assert material(run)['knowledge'][0]['content']==page['content']
    assert run.agent_memory['model_evidence_refs']


def test_context_adds_bridge_without_loading_unrelated_objects(monkeypatch):
    from helpers import harness
    w,session,_,_=harness(monkeypatch)
    run=QueryRun(session_id=session.id,domain='test',question='Query linked records')
    ont={'objects':[{'name':n,'table':n,'properties':[]} for n in ['a','b','c','other']],
         'relations':[{'name':'ab','from':'a','to':'b'},{'name':'bc','from':'b','to':'c'}]}
    packet=prepared_context.context_packet(w,run,ont,['a','c'])
    assert packet['selected_objects']==['a','b','c']


def test_free_text_confirmation_retains_business_facts_across_retry(monkeypatch):
    from semora_contracts import RunStatus
    w,session,*_=native_harness(monkeypatch)
    source=QueryRun(session_id=session.id,domain='test',question='Query items',status=RunStatus.FAILED)
    source.user_answers=[ConfirmedFact(question_id='scope',field='scope',value='Only current records',label='Only current records')]
    source.agent_memory['analyst']=[{'type':'ai','data':{'content':'PRIVATE RAW MEMORY'}}]
    data=handoff(source,session)
    assert data['confirmed_facts'][0]['value']=='Only current records'
    assert 'PRIVATE RAW MEMORY' not in str(data)
    assert not {'task_memory','candidate_intent','review_feedback'} & data.keys()


def test_current_models_keep_historical_display_data_without_restoring_old_execution():
    from app.control_state import restore_run
    run=restore_run({'session_id':'s','question':'Old record','plan':{'domain':'x','intent':'old','metrics':['total'],'stages':[]},
        'review':{'decision':'approved','rationale':'historic'}})
    assert run.plan.sql_program is None and run.plan.model_extra['metrics']==['total']
    assert run.review['decision']=='approved'


def test_retired_compiler_and_policy_routes_are_not_importable():
    import importlib.util
    from app.main import app
    for module in ['app.agentic','app.planner','app.gate','app.audit','semora_agent.roles']:
        assert importlib.util.find_spec(module) is None
    assert not any(r.path.startswith(('/api/v1/audits','/api/v1/approvals')) for r in app.routes)


def test_source_readback_survives_isolated_sql_role_context(monkeypatch):
    from helpers import harness
    from app.sql_authoring import material
    w,session,_,_=harness(monkeypatch)
    run=QueryRun(session_id=session.id,domain='test',question='Query records')
    state={'packet':{'objects':[],'knowledge':[]}}
    run.agent_memory['prepared_context']=[state]
    run.agent_memory['knowledge_discovery']=[{'kb_id':'kb','kind':'source','ref':'doc'}]
    provider=SimpleNamespace(provider='weknora',_request=AsyncMock(return_value=([{'id':'c1','content':'Only confirmed entries count.'}],{})))
    research=ResearchTools(w,run,{'knowledgeBaseIds':['kb']},{},knowledge_provider=provider)
    research.context_state=state
    read=research.knowledge_tools()[2]
    observed=asyncio.run(read('doc',reason='Verify the required counting rule'))
    assert observed['chunks'][0]['id']=='c1'
    assert material(run)['knowledge'][0]['content']=='Only confirmed entries count.'
    refs=run.agent_memory['model_evidence_refs'][-1]
    assert any({'document_id':'doc','chunk_id':'c1'} in ref.get('source_refs',[]) for ref in refs.values())
