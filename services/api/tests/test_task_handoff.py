import asyncio
from copy import deepcopy
import json

import pytest
from langchain_core.tools import tool
from semora_contracts import ConfirmedFact,OntologySnapshotRef,RunStatus,AuthorizationContext,ExecutionResult,ReviewDecision
from semora_agent.model import TaskSupersededError,ModelError
from semora_agent.runtime import run_agent
from app.task_handoff import ContextNotReady,handoff,project_context,readiness,size
from app.prepared_context import context_packet
from services.api.tests.test_plan_candidates import setup,ONTOLOGY


def packet():
    w,run,_,_=setup()
    run.authorization=AuthorizationContext(subject='tester',username='tester')
    fact=ConfirmedFact(question_id='scope',field='record.amount',value='all',label='全部历史记录')
    run.user_answers=[fact]
    enriched={**fact.model_dump(),'selected_option_description':'不限时间，统计全部历史记录，保留 NULL 与重复值的既定语义'}
    run.agent_memory['confirmed_fact_context']=[enriched]
    context={'request':run.question,'confirmed_fact_context':[deepcopy(enriched)],
        'prepared_context':context_packet(w,run,ONTOLOGY,['record']),
        'task_memory':{'facts':[{'fact':fact.model_dump()}],'request_history':['先前另一个完整用户表达',run.question],
            'previous_confirmed_execution':{'values':{'filters':[{'field':'record.amount','operator':'gt','value':'1234567890.1234567890123456789'}]}}}}
    return w,run,context


def test_projection_preserves_complete_user_facts_and_previous_execution():
    w,run,context=packet()
    context['objects']=[{'name':'record'}];context['metrics']=[]
    before=deepcopy(context)
    projected,guard=handoff(w,run,'analyst','interpret_and_plan',context)
    assert context==before
    assert projected['confirmed_fact_context']==context['confirmed_fact_context']
    assert projected['task_memory']['previous_confirmed_execution']==context['task_memory']['previous_confirmed_execution']
    assert projected['task_memory']['request_history']==['先前另一个完整用户表达']
    assert 'facts' not in projected['task_memory']
    assert 'objects' not in projected and 'schemas' not in projected['prepared_context']
    assert projected['task_contract']['effective_authorization']['sensitivity_clearance']==run.authorization.sensitivity_clearance
    guard()


def test_uncopied_history_facts_are_not_silently_discarded():
    _,_,context=packet()
    context['task_memory']['facts'].append({'fact':{'question_id':'other','field':'record.id','value':'keep'}})
    assert project_context(context,'analyst')['task_memory']['facts']==context['task_memory']['facts']


def test_only_duplicate_scans_and_private_dialogue_are_removed():
    _,_,context=packet()
    context['prepared_context']['schemas']=[{'object':'record','scanned':True,'tables':[{'columns':['duplicated-column']*2000}]}]
    context['analyst_messages']=['PRIVATE_WORKSPACE']*100
    context['reviewer_messages']=['PRIVATE_REVIEW']*100
    context['task_memory']['evidence']=[{'reference':{'source':'required'},'requires_refresh':True}]
    projected=project_context(context,'analyst')
    assert size(projected)<size(context)/3
    assert 'PRIVATE_' not in json.dumps(projected)
    assert projected['task_memory']['evidence']==context['task_memory']['evidence']
    assert projected['prepared_context']['objects']==context['prepared_context']['objects']


@pytest.mark.parametrize('missing',['facts','description','fields','endpoint'])
def test_missing_core_context_stops_before_dispatch(missing):
    w,run,context=packet()
    if missing=='facts':context['confirmed_fact_context']=[]
    elif missing=='description':context['confirmed_fact_context'][0].pop('selected_option_description')
    elif missing=='fields':context['prepared_context']['objects'][0]['field_refs']=[]
    else:context['prepared_context']['relations']=[{'name':'broken','from':'record','to':'absent'}]
    with pytest.raises(ContextNotReady):handoff(w,run,'analyst','interpret_and_plan',context)
    assert run.events[-1].event=='task.context.missing'
    assert not run.agent_memory.get('task_handoffs')
    assert run.sql is None and run.result is None


def test_optional_knowledge_failure_does_not_block_ready_task():
    w,run,context=packet()
    context['prepared_context']['knowledge']=[]
    context['prepared_context']['knowledge_warning']='未找到辅助资料'
    projected,_=handoff(w,run,'analyst','interpret_and_plan',context)
    assert projected['task_contract'] and projected['prepared_context']['knowledge']==[]


@pytest.mark.parametrize('change',['question','answer','ontology','authorization','cancel','result'])
def test_changed_inputs_invalidate_task_but_events_and_evidence_do_not(change):
    w,run,context=packet()
    _,guard=handoff(w,run,'analyst','interpret_and_plan',context)
    run.agent_memory['unrelated_working_data']=['new observation']
    w.emit(run,'test.event',run.status,{},'test')
    guard()
    if change=='question':run.question='新的明确要求'
    elif change=='answer':run.user_answers[0].value='changed'
    elif change=='ontology':run.ontology=OntologySnapshotRef(domain=run.domain,version='new',content_hash='new',artifact_version='new')
    elif change=='authorization':run.authorization.sensitivity_clearance='restricted'
    elif change=='cancel':run.status=RunStatus.FAILED
    else:run.result=ExecutionResult(columns=['value'],rows=[[42]],row_count=1,duration_ms=1)
    with pytest.raises(TaskSupersededError):guard()


def test_reviewer_does_not_receive_private_analysis_and_requires_artifacts():
    w,run,context=packet()
    context.update(user_request=context['request'],plan={'source_steps':[{'name':'one'}]},sql={'physical_sql':'SELECT 1'},
        continuation={'analyst':'PRIVATE'},task_memory={'private':'PRIVATE'},analyst_messages=['PRIVATE'])
    assert '跨源执行产物未完整交接' in readiness(run,'reviewer','execution_review',project_context(context,'reviewer'))
    context['source_artifacts']={'one':{'physical_sql':'SELECT 1'}}
    projected,_=handoff(w,run,'reviewer','execution_review',context)
    assert 'PRIVATE' not in json.dumps(projected)
    assert '缺少执行结果' in readiness(run,'reviewer','result_review',projected)
    projected['result']={'row_count':8}
    assert any('实际执行行数' in m for m in readiness(run,'reviewer','result_review',projected))
    projected['source_execution_receipts']=[{'step':'one','row_count':8,'truncated':False}]
    assert not readiness(run,'reviewer','result_review',projected)


def test_stale_model_response_cannot_trigger_a_tool():
    async def scenario():
        w,run,context=packet();_,guard=handoff(w,run,'analyst','interpret_and_plan',context)
        actions=[]
        @tool
        def observe()->str:
            """Read current evidence."""
            actions.append('called');return 'ok'
        class Model:
            async def complete_messages(self,*args,**kwargs):
                run.question='用户已改变目标'
                return {'tool_calls':[{'id':'old','function':{'name':'observe','arguments':'{}'}}]}
        with pytest.raises(TaskSupersededError):
            await run_agent(Model(),'test',context,[observe],[],lambda *a:None,task_guard=guard)
        assert not actions
    asyncio.run(scenario())


def test_workflow_discards_stale_result_without_failing_new_revision():
    async def scenario():
        w,run,context=packet();w.store.runs[run.id]=run
        run.ontology=OntologySnapshotRef(domain=run.domain,version='1',content_hash='h',artifact_version='v')
        async def stale(*args,**kwargs):
            run.question='新的有效输入'
            raise TaskSupersededError('old response')
        w._analyse_with_tools=stale
        state={'run_id':run.id,'ontology_model':ONTOLOGY,'settings':{}}
        outcome=await w._analyse_node(state)
        assert outcome['task_superseded'] and run.error is None and run.sql is None
        assert run.question=='新的有效输入' and run.events[-1].event=='task.result.superseded'
        assert w._route_after_step({**state,**outcome})=='end'
        assert run.status!=RunStatus.FAILED
    asyncio.run(scenario())


def test_role_budget_includes_tools_and_cannot_override_global_cap(monkeypatch):
    from semora_agent.context_budget import prepare_context
    monkeypatch.setenv('AGENT_CONTEXT_MAX_BYTES','1000')
    with pytest.raises(ModelError,match='未截断关键事实'):
        prepare_context([{'role':'user','content':'保留用户原文'}],[{'schema':'x'*300}],limit_bytes=200)
    with pytest.raises(ModelError):prepare_context([], [{'schema':'x'*2000}],limit_bytes=4000)


def test_json_only_model_calls_also_enforce_budget_before_http(monkeypatch):
    from semora_agent.model import StructuredModel
    async def scenario():
        m=StructuredModel();m.base_url='https://example.invalid';m.api_key='test';m.model='fake'
        async def forbidden(*args,**kwargs):raise AssertionError('HTTP must not start')
        monkeypatch.setattr(m,'_request',forbidden)
        monkeypatch.setenv('AGENT_CONTEXT_MAX_BYTES','100')
        with pytest.raises(ModelError,match='未截断关键事实'):await m.complete_json('system','x'*200)
    asyncio.run(scenario())


def test_explanation_service_failure_preserves_verified_query_results(monkeypatch):
    from types import SimpleNamespace
    from services.api.tests.test_plan_candidates import PLAN
    from semora_agent import model as model_module
    async def scenario():
        w,run,context=packet();w.store.runs[run.id]=run
        run.plan=w.analyst._build_outcome(PLAN,run.question,[],ONTOLOGY,{},'test').plan
        run.result=ExecutionResult(columns=['record_id'],rows=[[1]],row_count=1,duration_ms=1)
        async def approved(*args):return ReviewDecision(decision='approved',rationale='actual result valid')
        async def unavailable(*args,**kwargs):raise ModelError('service unavailable')
        w._agentic_enabled=lambda role:False
        w.reviewer=SimpleNamespace(review_result=approved)
        monkeypatch.setattr(model_module,'structured_model',SimpleNamespace(configured=True,complete_json=unavailable))
        await w._explain_node({'run_id':run.id,'ontology_model':ONTOLOGY})
        assert run.status==RunStatus.COMPLETED and run.result.rows==[[1]] and run.error is None
        assert '模型说明暂不可用' in run.answer
        assert '脱敏' not in run.answer
        assert any(e.event=='answer.explanation.unavailable' for e in run.events)
    asyncio.run(scenario())
