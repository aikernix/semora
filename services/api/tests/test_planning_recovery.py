import asyncio
import json
from types import SimpleNamespace

import pytest
from semora_contracts import QueryIntent
from semora_query_core.intent import compile_intent,IntentPlanningError
from app.planning_recovery import failure,preserve_unrelated
from services.api.tests.test_scoped_aggregates import fixture,build,execute
from semora_query_core.sql import compile_plan


def test_entity_detail_with_related_aggregate_infers_grain_without_losing_rows():
    ontology,_=fixture()
    intent={'summary':'按主体展示记录和关联统计','subject':'entity','fields':['entity.id','entity.label'],
            'measures':[{'name':'items','function':'count','field':'position.id'},
                        {'name':'last_value','function':'max','field':'position.days'}],
            'having':[{'field':'items','operator':'gte','value':2}]}
    _,outcome=build(intent)
    assert outcome.plan.dimensions==intent['fields']
    assert execute(compile_plan(outcome.plan,ontology))==[[1,'a',3,5]]
    assert outcome.plan.query_intent.group_by==[]  # Keep original declaration, not a fake user confirmation.


def test_ambiguous_entity_grain_is_a_business_question_not_silent_grouping():
    ontology,_=fixture()
    for fields in [['entity.label'],['entity.id','movement.id']]:
        intent={'summary':'统计并展示明细','subject':'entity','fields':fields,
                'measures':[{'name':'items','function':'count','field':'position.id'}]}
        with pytest.raises(IntentPlanningError) as caught:compile_intent(intent,ontology,{})
        assert caught.value.kind=='ambiguity' and caught.value.path=='group_by'


@pytest.mark.parametrize('mysql',[False,True])
def test_min_max_preserve_non_numeric_types_instead_of_rounding_dates_to_numbers(mysql):
    import sqlite3
    import re
    import sqlglot
    from semora_agent import AnalystAgent
    from semora_query_core.sql import adapt_mysql_artifact
    ontology={'domain':'generic','dataSourceId':'one','objects':[{'name':'record','table':'records','primaryKey':'id',
        'properties':[{'name':'id','column':'id','type':'integer'},{'name':'event_time','column':'event_time','type':'date'},
                      {'name':'code','column':'code','type':'string'}]}],'relations':[],'metrics':[]}
    value=compile_intent({'summary':'输出边界值','subject':'record','measures':[
        {'name':'last_event','function':'max','field':'record.event_time'},
        {'name':'first_code','function':'min','field':'record.code'}]},ontology,{})
    outcome=AnalystAgent()._build_outcome(value,'query',[],ontology,{},'test')
    artifact=compile_plan(outcome.plan,ontology)
    if mysql:artifact=adapt_mysql_artifact(artifact)
    assert 'ROUND' not in artifact.physical_sql.upper()
    sql=sqlglot.transpile(artifact.physical_sql,read=artifact.dialect,write='sqlite')[0]
    with sqlite3.connect(':memory:') as db:
        db.executescript("CREATE TABLE records(id INT,event_time TEXT,code TEXT); INSERT INTO records VALUES(1,'2026-10-01','002'),(2,'2026-11-07','001');")
        assert db.execute(sql).fetchall()==[('2026-11-07','001')]


def test_targeted_correction_is_one_attempt_and_preserves_unrelated_filters():
    intent=QueryIntent(summary='查记录',subject='record',fields=['record.typo'],
                       filters=[{'field':'record.state','operator':'eq','value':'active'}])
    kit=SimpleNamespace(run=SimpleNamespace(agent_memory={}),intent_repair_only=False)
    feedback=failure(kit,intent,kind='configuration',path='fields',message='未发布字段',reference='record.typo')
    assert feedback['kind']=='intent_correction' and kit.intent_repair_only
    fixed=intent.model_dump(mode='json');fixed['fields']=['record.id']
    assert preserve_unrelated(kit.intent_repair_base,fixed,kit.intent_repair_paths)
    fixed['filters'][0]['value']='all'
    assert not preserve_unrelated(kit.intent_repair_base,fixed,kit.intent_repair_paths)
    failure(kit,QueryIntent.model_validate(fixed),kind='compiler_validation',path='intent',message='改变了其他条件')
    assert kit.run.agent_memory['last_intent_candidate'][-1]['filters'][0]['value']=='active'
    second=failure(kit,intent,kind='configuration',path='fields',message='仍然未修正',reference='record.typo')
    assert kit.protocol_stage=='finalizing' and second['kind']=='configuration'
    assert kit.run.agent_memory['last_intent_candidate']


def test_react_repairs_only_the_invalid_reference_without_new_retrieval(monkeypatch):
    from app import agentic
    from app.role_ports import IntentRolePorts
    from app.intent_analysis import analyse_intent
    from app.prepared_context import context_packet
    from app import prepared_context
    from semora_agent import AnalystAgent
    from semora_agent.runtime import run_agent
    from services.api.tests.test_agent_runtime import NativeModel
    from services.api.tests.test_plan_candidates import setup,ONTOLOGY
    monkeypatch.setenv('AGENT_QUERY_INTENT','false')
    workflow,run,_,_=setup()
    monkeypatch.setenv('AGENT_QUERY_INTENT','true');monkeypatch.setenv('AGENT_BOUNDED_CONTEXT','true')
    async def prepared(*args):return {'packet':context_packet(workflow,run,ONTOLOGY,['record'])}
    monkeypatch.setattr(prepared_context,'prepare_context',prepared)
    class Model(NativeModel):
        offered=[]
        async def complete_messages(self,messages,tools,**kwargs):
            self.offered.append({t['function']['name'] for t in tools})
            return await super().complete_messages(messages,tools,**kwargs)
    model=Model([('validate_intent',{'intent':{'summary':'查询记录','subject':'record','fields':['record.missing']}}),
                 ('validate_intent',{'intent':{'summary':'查询记录','subject':'record','fields':['record.id']}})])
    workflow.analyst=AnalystAgent(model);workflow._agentic_enabled=lambda role:False
    async def scenario():
        result=await analyse_intent(workflow,run,run.question,ONTOLOGY,{},ports=IntentRolePorts(agentic.AgentTools,agentic.AnalysisSubmission,None,run_agent))
        assert result.plan.fields==['record.id'] and len(model.messages)==2
        assert model.offered[-1]<={'validate_intent','AgentSubmission'}
        assert run.agent_memory['planning_diagnostics']
    asyncio.run(scenario())


def test_real_grain_ambiguity_becomes_clarification_instead_of_failed_analysis(monkeypatch):
    from app import agentic,prepared_context
    from app.role_ports import IntentRolePorts
    from app.intent_analysis import analyse_intent
    from semora_agent import AnalystAgent
    from semora_agent.runtime import run_agent
    from services.api.tests.test_agent_runtime import NativeModel
    from services.api.tests.test_plan_candidates import setup,ONTOLOGY
    monkeypatch.setenv('AGENT_QUERY_INTENT','false')
    workflow,run,_,_=setup()
    monkeypatch.setenv('AGENT_QUERY_INTENT','true');monkeypatch.setenv('AGENT_BOUNDED_CONTEXT','true')
    async def prepared(*args):return {'packet':prepared_context.context_packet(workflow,run,ONTOLOGY,['record'])}
    monkeypatch.setattr(prepared_context,'prepare_context',prepared)
    model=NativeModel([
        ('validate_intent',{'intent':{'summary':'明细和汇总','subject':'record','fields':['record.amount'],
                                    'measures':[{'name':'items','function':'count','field':'record.id'}]}}),
        ('AgentSubmission',{'payload':{'summary':'请确认输出粒度','clarifications':[{'id':'grain','field':'group_by',
            'question':'每行代表一个对象还是一个汇总分组？','reason':'影响明细和计数的对应关系','options':[]}]}})])
    workflow.analyst=AnalystAgent(model);workflow._agentic_enabled=lambda role:False
    async def scenario():
        result=await analyse_intent(workflow,run,run.question,ONTOLOGY,{},ports=IntentRolePorts(agentic.AgentTools,agentic.AnalysisSubmission,None,run_agent))
        assert result.plan is None and result.requirement.clarifications[0].id=='grain'
        assert len(model.messages)==2
    asyncio.run(scenario())
