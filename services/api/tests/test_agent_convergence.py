import asyncio
import json

import httpx
import pytest
from langchain_core.messages import ToolMessage
from langchain_core.tools import StructuredTool
from semora_agent.model import StructuredModel
from semora_agent.progress import ProgressBudget,NoProgressError
from semora_agent.runtime import run_agent
from app.agentic import AnalysisSubmission
from services.api.tests.test_agent_runtime import NativeModel,CARD
from services.api.tests.test_model_requests import client_with_transport


def test_progress_tracks_actual_new_evidence_and_survives_phase_change():
    state={};events=[]
    budget=ProgressBudget(lambda n,d:events.append((n,d)),state)
    def observe(value,identifier):
        budget.pending=[{'name':'read_object','id':identifier}]
        budget.consume([ToolMessage(content=json.dumps(value),tool_call_id=identifier)])
    observe({'object':{'name':'x','properties':['id']}},'1')
    assert state['streak']==0
    observe({'object':{'name':'x','properties':['id']},'message':'different wording'},'2')
    assert state['streak']==1
    budget=ProgressBudget(lambda n,d:events.append((n,d)),state)
    observe({'object':{'name':'x','properties':['id']}},'3')
    assert budget.must_deliver and state['streak']==2
    with pytest.raises(NoProgressError): observe({'object':{'name':'x','properties':['id']}},'4')
    assert state['streak']==3


def test_new_evidence_resets_streak_but_protocol_switch_does_not():
    b=ProgressBudget(lambda *args:None)
    b.fail('empty');b.fail('empty')
    b.pending=[{'name':'read_schema','id':'new'}]
    b.consume([ToolMessage(content='{"tables":[{"name":"items"}]}',tool_call_id='new')])
    assert b.state['streak']==0
    b.pending=[{'name':'enter_planning','id':'transition'}]
    b.consume([ToolMessage(content='{"stage":"planning"}',tool_call_id='transition')])
    assert b.state['streak']==1


async def read_object(name:str):
    """Read a published object."""
    return json.dumps({'object':{'name':name,'properties':['id']}})


def test_repeated_react_observations_force_final_clarification():
    async def scenario():
        model=NativeModel([('read_object',{'name':'item'})]*3+[('AgentSubmission',{'payload':CARD})])
        tools=[StructuredTool.from_function(coroutine=read_object)]
        state={};value,_=await run_agent(model,'发现业务缺口立即澄清',{},tools,[],lambda *a:None,
            submission_schema=AnalysisSubmission,progress_state=state)
        assert value['clarifications'][0]['id']=='time' and len(model.messages)==4
        assert state['streak']==0
        from semora_agent.prompts import FINAL_DELIVERY
        assert model.messages[-1][-1]['content']==FINAL_DELIVERY
    asyncio.run(scenario())


def test_ignoring_last_delivery_stops_before_another_model_request():
    class EmptyLast(NativeModel):
        async def complete_messages(self,messages,tools,**kwargs):
            if len(self.messages)==3:
                self.messages.append(messages)
                assert {t['function']['name'] for t in tools}=={'AgentSubmission'}
                return {'content':'仍在考虑','tool_calls':[]}
            return await super().complete_messages(messages,tools,**kwargs)
    async def scenario():
        m=EmptyLast([('read_object',{'name':'item'})]*3)
        with pytest.raises(NoProgressError):
            await run_agent(m,'system',{},[StructuredTool.from_function(coroutine=read_object)],[],lambda *a:None)
        assert len(m.messages)==4
    asyncio.run(scenario())


def test_transport_retries_count_towards_same_progress_budget(monkeypatch):
    calls=[];events=[];state={}
    def handler(request):
        calls.append(True)
        raise httpx.ReadTimeout('test')
    client=client_with_transport(monkeypatch,handler);client.request_retries=2
    with pytest.raises(NoProgressError):
        asyncio.run(run_agent(client,'system',{},[],[],lambda n,d:events.append((n,d)),progress_state=state))
    assert len(calls)==3 and state['http_attempts']==3 and state['streak']==3
    assert sum(n=='agent.model.started' for n,_ in events)==1


def test_explicit_clarification_returns_immediately_without_more_react():
    m=NativeModel([('AgentSubmission',{'payload':CARD})])
    value,_=asyncio.run(run_agent(m,'system',{},[],[],lambda *a:None,submission_schema=AnalysisSubmission))
    assert value['clarifications'][0]['question']=='统计哪个时间？' and len(m.messages)==1


def test_technical_no_delivery_does_not_invent_business_clarification():
    from app.workflow import Workflow
    from app.store import Store
    from semora_agent import AnalystAgent
    from semora_agent.model import NoDeliveryError
    from services.api.tests.test_workflow import fake_execute,fake_retrieve,fake_snapshot
    class Model:
        configured=required=True
        async def complete_messages(self,*args,**kwargs):raise NoDeliveryError('timeout')
    async def scenario():
        w=Workflow(Store(),fake_execute,fake_retrieve,fake_snapshot)
        w.analyst=AnalystAgent(Model())
        r=await w.start('demo-session','查询指标')
        assert r.status.value=='failed' and '交付截止时间' in r.error
        assert r.sql is None and not any(e.event=='alignment.required' for e in r.events)
    asyncio.run(scenario())
