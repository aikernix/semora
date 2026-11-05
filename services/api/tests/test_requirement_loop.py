import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.model_settings import ModelConfigInput, model_settings
from app.store import Store
from app.workflow import Workflow
from semora_agent import AnalystAgent, ReviewerAgent
from semora_agent.model import StructuredModel, structured_model
from semora_agent.roles import AnalysisOutcome
from semora_contracts import AuthorizationContext, ClarificationQuestion, RequirementDraft, RunStatus, QueryCalculation, QueryPlan
from app.planner import compile_plan
from app.knowledge import WeKnoraClient
from services.api.tests.test_workflow import fake_snapshot, fake_execute


def test_multiple_clarification_rounds_continue_without_knowledge_and_execute_once():
    async def scenario():
        store=Store()
        executions=[]
        async def execute(sql,parameters,sensitive):
            executions.append(sql)
            return await fake_execute(sql,parameters,sensitive)
        async def unavailable(_):
            raise RuntimeError('knowledge unavailable')
        workflow=Workflow(store,execute,unavailable,fake_snapshot)
        workflow.reviewer=ReviewerAgent(StructuredModel())
        class RoundAnalyst:
            calls=0
            async def analyse(self,question,evidence,ontology,data_scope):
                self.calls+=1
                if self.calls<=2:
                    key='metric' if self.calls==1 else 'time'
                    return AnalysisOutcome(None,RequirementDraft(summary='待确认'+key,data_scope=data_scope,clarifications=[ClarificationQuestion(id=key,field=key,question='请确认'+key,reason='影响结果')]))
                assert 'metric=贷款余额' in question and 'time=全部数据' in question
                return AnalystAgent(StructuredModel())._deterministic_analysis('各分行贷款余额',evidence,ontology,data_scope)
        analyst=RoundAnalyst()
        workflow.analyst=analyst
        auth=AuthorizationContext(subject='test',username='test',roles=['analyst'],branch_ids=[1])
        run=await workflow.start_background('demo-session','贷款情况',auth)
        for _ in range(100):
            if run.status in {RunStatus.CLARIFYING,RunStatus.FAILED}:break
            await asyncio.sleep(.01)
        assert run.status==RunStatus.CLARIFYING
        assert not run.evidence and any(e.event=='knowledge.warning' for e in run.events)
        run=await workflow.align(run,'user',{'metric':'贷款余额'})
        assert run.status==RunStatus.CLARIFYING and run.requirement_draft.clarifications[0].id=='time'
        assert run.sql is None and not executions
        run=await workflow.align(run,'user',{'time':'全部数据'})
        assert run.status==RunStatus.CLARIFYING and not run.requirement_draft.clarifications
        assert run.sql is None and not executions
        run=await workflow.align(run,'user',{})
        assert run.status==RunStatus.COMPLETED,run.error
        assert len(executions)==1 and analyst.calls==3
        assert len(run.requirement.confirmed_facts)==2
    asyncio.run(scenario())


def test_model_configuration_never_returns_secret_and_preserves_blank_key(monkeypatch):
    for key in ('base_url','api_key','model','mode'):
        monkeypatch.setattr(structured_model,key,getattr(structured_model,key))
    monkeypatch.setattr(model_settings,'dsn',None)
    value=ModelConfigInput(base_url='https://models.example/v1',model='test-model',api_key='secret-test-key')
    public=model_settings.save(value)
    assert public['configured'] and 'api_key' not in public
    assert 'secret-test-key' not in json.dumps(public)
    model_settings.save(ModelConfigInput(base_url=value.base_url,model='next-model'))
    assert structured_model.api_key=='secret-test-key'
    with pytest.raises(ValueError,match='重新输入'):
        model_settings.candidate(ModelConfigInput(base_url='https://different.example/v1',model='test'))
    client=TestClient(app)
    assert 'secret-test-key' not in client.get('/api/v1/settings/model').text


def test_missing_model_is_reported_before_creating_query(monkeypatch):
    monkeypatch.setattr(model_settings,'dsn','configured-local-store')
    monkeypatch.setattr(structured_model,'base_url','')
    monkeypatch.setattr(structured_model,'api_key','')
    response=TestClient(app).post('/api/v1/sessions/demo-session/messages',json={'question':'贷款余额'})
    assert response.status_code==409 and '模型' in response.json()['detail']


def test_temporary_count_does_not_require_a_published_metric():
    ontology={'dataSourceId':'mysql','objects':[{'name':'loan','primaryKey':'ID','table':'ACCT_LOAN','properties':[{'name':'id','column':'ID','type':'string'}]}],'metrics':[],'relations':[]}
    plan=QueryPlan(domain='bank',data_source_id='mysql',intent='贷款账户数',grain='loan',metrics=['loan_count'],calculations=[QueryCalculation(name='loan_count',function='count')])
    artifact=compile_plan(plan,ontology)
    assert 'COUNT(*)' in artifact.physical_sql
    assert ontology['metrics']==[]


def test_wiki_research_actions_are_selected_by_the_model(monkeypatch):
    decisions=iter([
        {'action':'wiki_search','kb_id':'kb','query':'贷款'},
        {'action':'wiki_read_page','kb_id':'kb','slug':'concept/loan'},
        {'action':'finish','summary':'找到贷款定义，来源 concept/loan'},
    ])
    class Model:
        configured=True
        async def complete_json(self,system,user):return next(decisions)
    import semora_agent.model as model_module
    monkeypatch.setattr(model_module,'structured_model',Model())
    def handler(request):
        if request.url.path.endswith('/search'):
            return httpx.Response(200,json={'pages':[{'slug':'concept/loan','title':'贷款','summary':'贷款口径'}]})
        return httpx.Response(200,json={'slug':'concept/loan','title':'贷款','content':'贷款定义','source_refs':['doc'],'chunk_refs':['chunk'],'version':1,'out_links':[],'in_links':[]})
    client=WeKnoraClient('https://wiki.example/api/v1','test-key',httpx.MockTransport(handler))
    result=asyncio.run(client.research('贷款口径',['kb']))
    assert result.agent_id=='semora-llm-wiki-researcher'
    assert len(result.evidence)==1
    assert [step.tool for step in result.steps[:2]]==['wiki_search','wiki_read_page']


def test_greeting_is_clarified_before_any_knowledge_lookup(monkeypatch):
    from app import workflow as workflow_module
    async def unexpected(*args):
        raise AssertionError('Greeting must not be searched')
    monkeypatch.setattr(workflow_module.knowledge_service,'research',unexpected)
    class Model:
        configured=True
        required=True
        calls=0
        async def complete_json(self,system,user):
            self.calls+=1
            return {'summary':'您好，请描述想了解的数据','requires_knowledge':False,'clarifications':[{'id':'goal','field':'goal','question':'您想了解什么数据？','reason':'还未提供查数目标','options':[{'id':'summary','label':'汇总统计','value':'汇总统计'},{'id':'detail','label':'查看明细','value':'查看明细'}]}]}
    async def scenario():
        model=Model()
        workflow=Workflow(Store(),fake_execute,None,fake_snapshot)
        workflow.analyst=AnalystAgent(model)
        auth=AuthorizationContext(subject='test',username='test',roles=['analyst'],branch_ids=[1])
        run=await workflow.start_background('demo-session','你好',auth)
        for _ in range(100):
            if run.status in {RunStatus.CLARIFYING,RunStatus.FAILED}:break
            await asyncio.sleep(.01)
        assert run.status==RunStatus.CLARIFYING,run.error
        assert model.calls==1 and run.sql is None
        assert run.requirement_draft.clarifications[0].id=='goal'
    asyncio.run(scenario())
