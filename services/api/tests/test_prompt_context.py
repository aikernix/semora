import asyncio
from copy import deepcopy
import json

from pydantic import ValidationError
import pytest

from app.model_context import project_knowledge, output_definitions
from app.model_outputs import CandidateProposal, KnowledgeAction, knowledge_action_schema
from semora_agent.prompts import analyst_prompt, reviewer_prompt
from semora_contracts import QueryRun, QueryPlan, QueryIntent


def test_evidence_projection_preserves_business_content_and_private_provenance():
    run=QueryRun(session_id='synthetic',question='查询关联数值')
    content='公式：SUM(x)/COUNT(DISTINCT id)。排除撤销项；空分母返回NULL。精度0.00000000000000001。'
    original={'evidence':[{'knowledge_base_id':'KB_PRIVATE','document_id':'DOC_PRIVATE',
        'chunk_id':'CHUNK_PRIVATE','locator':'Wiki: internal-path','revision':'v7','summary':content,
        'definition_status':'pending','scope':'仅已完成记录','conflicts':['与正式定义冲突'],'pending_confirmation':True}],
        'knowledge':[{'knowledge_base_id':'KB_PRIVATE','slug':'internal-path','links':['internal-link'],
            'revision':'v7','content':content,'sources':[{'document_id':'DOC_PRIVATE',
                'chunks':[{'id':'SOURCE_PRIVATE','content':'原文例外：状态为X时保留。'}]}]}]}
    before=deepcopy(original)
    projected=project_knowledge(original,run)
    assert original==before
    assert projected['evidence'][0]['content']==content
    assert projected['evidence'][0]['status']=='pending'
    assert projected['evidence'][0]['scope']=='仅已完成记录'
    assert projected['evidence'][0]['conflicts']==['与正式定义冲突']
    assert projected['evidence'][0]['pending_confirmation'] is True
    assert projected['knowledge'][0]['content']==content
    assert projected['knowledge'][1]['content']=='原文例外：状态为X时保留。'
    assert 'PRIVATE' not in json.dumps(projected)
    assert 'internal-path' not in json.dumps(projected)
    assert 'internal-link' not in json.dumps(projected)
    refs=run.agent_memory['model_evidence_refs'][0]
    assert refs[projected['evidence'][0]['ref']]['revision']=='v7'
    assert refs[projected['knowledge'][1]['ref']]['chunk_id']=='SOURCE_PRIVATE'
    assert project_knowledge(projected,run)==projected
    standalone=project_knowledge({'knowledge':[{'content':'定义A','status':'pending'}, {'content':'定义B','status':'reference','sources':None}]},run)
    assert standalone['knowledge'][0]['ref']!=standalone['knowledge'][1]['ref']
    assert standalone['knowledge'][0]['status']=='pending'
    # The same serializer used for persisted control state retains the mapping.
    restored=QueryRun.model_validate({**run.model_dump(mode='json'),'agent_memory':json.loads(json.dumps(run.agent_memory))})
    assert restored.agent_memory['model_evidence_refs']==run.agent_memory['model_evidence_refs']


def test_output_definitions_include_measure_fields_and_units():
    intent=QueryIntent(summary='汇总数值',subject='record',measures=[{'name':'total','function':'sum','field':'record.amount'}])
    plan=QueryPlan(domain='test',intent='汇总数值',metrics=[],query_intent=intent)
    ontology={'objects':[{'name':'record','properties':[{'name':'amount','label':'数值','type':'decimal',
        'unit':'单位A','description':'不含撤销项，缺值保持NULL'}]}]}
    definitions=output_definitions(plan,ontology)
    assert definitions['fields'][0]['unit']=='单位A'
    assert definitions['fields'][0]['description']=='不含撤销项，缺值保持NULL'


def test_phase_prompts_and_available_action_schema_are_unambiguous():
    prompts=[reviewer_prompt(p) for p in ('plan_preflight','execution_review','result_review')]
    assert len(set(prompts))==3
    assert knowledge_action_schema(must_finish=True)['properties']['action']['enum']==['finish']
    assert len(knowledge_action_schema()['properties']['action']['enum'])==4
    # Closed actions stay executable-policy controlled, not a second contradictory list.
    assert 'allowed_actions' not in knowledge_action_schema(must_finish=True)
    for prompt in prompts+[analyst_prompt()]:
        assert not any(t in prompt for t in ['SOURCE_PLANNING_RULES','Wiki','银行','课程','不读取','3次工具'])
    with pytest.raises(ValidationError):
        KnowledgeAction.model_validate({'action':'read_source','page':'bad'})
    with pytest.raises(ValidationError):
        CandidateProposal.model_validate({'term':'X','proposal':'定义','kind':'unsupported','patch':{},'evidence_chunk_id':'c'})


def test_execution_contract_preserves_enrichment_then_regroup_and_overflow_semantics():
    from services.api.tests.test_result_regroup import fixture, build
    from app.model_context import execution_contract, intent_field_meanings
    ontology,intent=fixture()
    plan=build(ontology,intent).plan
    contract=execution_contract(plan)
    final=contract['steps'][-1]
    assert [o['kind'] for o in final['result_operations']]==['enrich_source_fields','regroup_partials']
    assert final['sql_limit_role']=='overflow_detection'
    assert final['on_incomplete_input']=='stop_before_key_transfer_or_merge'
    assert final['regroup']['limit']==1
    assert contract['steps'][0]['sql_limit_role']=='business_top_n'
    meanings=intent_field_meanings(plan.query_intent)
    assert set(meanings)=={'selection','record_scope','order_by'}
    assert meanings['selection']['object']==type(plan.query_intent.selection).model_fields['object'].description


def test_compiled_dependency_records_the_actual_completeness_guard(monkeypatch):
    from services.api.tests.test_source_execution import harness
    async def scenario():
        w,session,_=harness(monkeypatch)
        run=await w.start(session.id,'查询关联明细',domain='commerce')
        assert run.status.value=='completed',run.error
        guard=run.source_artifacts['pick_clients'].compilation['completeness_check']
        assert guard['max_accepted_rows']==1000
        assert guard['on_truncated']==guard['on_row_overflow']=='fail_no_result'
        assert guard['checked_before']==['enrichment','key_transfer','regroup','membership']
    asyncio.run(scenario())


@pytest.mark.parametrize('field,expected',[
    ('balances.amount',[[10],[20],[999]]),
    ('client.customer_id',[['A'],['A'],['B']]),
])
def test_malformed_explanations_do_not_discard_valid_query(monkeypatch,field,expected):
    from services.api.tests.test_source_execution import harness
    from semora_agent import model as model_module, AnalystAgent, ReviewerAgent
    from services.api.tests.test_agent_runtime import NativeModel
    async def scenario():
        w,session,_=harness(monkeypatch)
        class Malformed:
            configured=True
            async def complete_json(self,*args,**kwargs):return {'unexpected':'missing explanation'}
        monkeypatch.setattr(model_module,'structured_model',Malformed())
        w.analyst=AnalystAgent(NativeModel([('validate_intent',{'intent':{
            'summary':'查询关联数值','subject':'client','fields':[field]}})]))
        w.reviewer=ReviewerAgent(NativeModel([('AgentSubmission',{'payload':{
            'decision':'approved','rationale':'与确认目标一致'}})]*3))
        run=await w.start(session.id,'关联查询',domain='commerce')
        assert run.status.value=='completed',run.error
        assert run.sql is not None and run.result is not None
        assert run.result.rows==expected
        assert '实际查询结果' in run.answer
        if field=='client.customer_id':
            assert any(e.event=='sql.explanation.unavailable' for e in run.events)
            assert run.sql_explanation
        contexts=[json.loads(next(m['content'] for m in messages if m['role']=='user')) for messages in w.reviewer.model.messages]
        assert 'source_steps' not in contexts[0]['plan']
        assert 'result_output_contract' not in contexts[0]
        assert 'physical_sql' not in json.dumps(contexts[0]['plan'])
        assert 'result_output_contract' in contexts[1] and 'result' not in contexts[1]
        assert contexts[2]['result']['rows']==expected
    asyncio.run(scenario())
