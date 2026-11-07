import asyncio
from types import SimpleNamespace

import pytest

from app.review_budget import run_review
from app.review_context import supplement_review
from semora_agent.model import ModelError


def test_recheck_does_not_renew_request_budget():
    async def scenario():
        run=SimpleNamespace(agent_memory={})
        limits=[]
        async def runner(*args,event,max_models,max_tools,**kwargs):
            limits.append((max_models,max_tools))
            for _ in range(max_models):event('agent.model.started',{})
            return {},[]
        for _ in range(2):
            await run_review(run,'execution_review',runner,event=lambda *args:None)
        with pytest.raises(ModelError,match='预算'):
            await run_review(run,'execution_review',runner,event=lambda *args:None)
        assert limits==[(3,2),(1,0)]
        assert run.agent_memory['review_budgets'][0]['execution_review']['attempts']==4
    asyncio.run(scenario())


def test_transport_retries_are_counted_before_another_request_can_start():
    async def scenario():
        run=SimpleNamespace(agent_memory={}); sent=[]
        async def runner(*args,event,**kwargs):
            event('agent.model.started',{})
            for _ in range(5):
                event('agent.request.started',{})
                sent.append(True)
        with pytest.raises(ModelError,match='包含网络重试'):
            await run_review(run,'plan_preflight',runner,event=lambda *args:None)
        assert len(sent)==3
        assert run.agent_memory['review_budgets'][0]['plan_preflight']['attempts']==3
    asyncio.run(scenario())


def test_review_timeout_cancels_work_and_cannot_reset_elapsed_budget(monkeypatch):
    monkeypatch.setenv('REVIEW_TIMEOUT_SECONDS','.01')
    async def scenario():
        run=SimpleNamespace(agent_memory={});cancelled=[]
        async def runner(*args,event,**kwargs):
            try:await asyncio.sleep(10)
            finally:cancelled.append(True)
        with pytest.raises(ModelError,match='总时间预算'):
            await run_review(run,'result_review',runner,event=lambda *args:None)
        with pytest.raises(ModelError,match='预算已用完'):
            await run_review(run,'result_review',runner,event=lambda *args:None)
        assert cancelled==[True]
    asyncio.run(scenario())


def test_real_correction_can_use_remaining_budget_without_reopening_old_input():
    async def scenario():
        run=SimpleNamespace(agent_memory={})
        amounts=iter([2,1,1])
        async def runner(*args,event,**kwargs):
            for _ in range(next(amounts)):event('agent.model.started',{})
            return {},[]
        for key in ['original','original','corrected']:
            await run_review(run,'plan_preflight',runner,event=lambda *args:None,revision_key=key)
        with pytest.raises(ModelError,match='预算'):
            await run_review(run,'plan_preflight',runner,event=lambda *args:None,revision_key='another')
        assert run.agent_memory['review_budgets'][0]['plan_preflight']['attempts']==4
    asyncio.run(scenario())


def context_toolkit():
    row={'object':{'name':'item','properties':[{'name':'id','type':'string'}]},'field_refs':['item.id']}
    return SimpleNamespace(context_state={'binding':'version-1','packet':{
        'selected_objects':['item'],'objects':[row],'relations':[]}},run=SimpleNamespace(agent_memory={}))


def test_review_supplement_requires_specific_gap_and_cannot_expand_scope():
    kit=context_toolkit()
    assert not supplement_review(kit,['item'],'join','核对关联',[], '')['available']
    unknown=supplement_review(kit,['other'],'join','核对关联',['other.id'],'检查关联键类型')
    assert not unknown['available'] and unknown['finalize']
    missing=supplement_review(kit,['item'],'join','核对关联',['item.unknown'],'检查关联键类型')
    assert not missing['available'] and missing['missing_fields']==['item.unknown']


def test_review_supplement_deduplicates_semantically_equal_requests():
    kit=context_toolkit()
    first=supplement_review(kit,['item'],'join','核对类型',['item.id'],'关联字段类型一致')
    second=supplement_review(kit,['item'],'output','换个说法',['item.id'],'检查字段')
    assert first['available'] and first['finalize'] and first['objects']
    assert second['unchanged'] and second['finalize'] and 'objects' not in second


def test_delivery_action_identifies_authentication_instead_of_asking_for_business_details():
    from app.delivery import next_action
    run=SimpleNamespace(agent_memory={},error='Executor 数据源操作失败（HTTP 502）：MySQL 连接失败（1045）')
    assert '账号、密码' in next_action(run)


def test_only_reviewer_requires_gap_contract(monkeypatch):
    from app.agent_tools import AgentTools
    from services.api.tests.test_plan_candidates import setup,ONTOLOGY
    from pydantic import ValidationError
    monkeypatch.setenv('AGENT_BOUNDED_CONTEXT','true')
    monkeypatch.setenv('AGENT_QUERY_INTENT','false')
    workflow,run,_,_=setup()
    value={'objects':['record'],'purpose':'filter','reason':'核对条件'}
    for role in ['analyst','reviewer']:
        tool=next(t for t in AgentTools(workflow,run,ONTOLOGY,{},role=role).tools() if t.name=='request_context')
        if role=='analyst':assert tool.args_schema.model_validate(value).field_refs is None
        else:
            with pytest.raises(ValidationError):tool.args_schema.model_validate(value)
            assert tool.args_schema.model_validate({**value,'field_refs':['record.id'],'acceptance':'字段存在'})


def test_unexpected_bugs_and_invalid_configuration_are_not_advisory():
    from app.delivery import RECOVERABLE_REVIEW_ERRORS
    from semora_agent.model import ReviewTimeoutError,ReviewBudgetExhaustedError,TaskSupersededError
    assert isinstance(ReviewTimeoutError(),RECOVERABLE_REVIEW_ERRORS)
    assert isinstance(ReviewBudgetExhaustedError(),RECOVERABLE_REVIEW_ERRORS)
    for exc in [TypeError(),KeyError(),TaskSupersededError(),ModelError('invalid configuration')]:
        assert not isinstance(exc,RECOVERABLE_REVIEW_ERRORS)


def test_real_transport_retries_share_review_budget(monkeypatch):
    import httpx
    from services.api.tests.test_model_requests import client_with_transport
    requests=[]
    def handler(request):
        requests.append(True)
        raise httpx.ReadTimeout('test',request=request)
    client=client_with_transport(monkeypatch,handler)
    client.request_retries=10
    async def scenario():
        run=SimpleNamespace(agent_memory={})
        async def runner(*args,event,**kwargs):
            event('agent.model.started',{})
            return await client.complete_messages([],[],on_request_event=event)
        with pytest.raises(ModelError,match='包含网络重试'):
            await run_review(run,'plan_preflight',runner,event=lambda *args:None)
        assert len(requests)==3
    asyncio.run(scenario())


@pytest.mark.parametrize('mode',['strict','best_effort'])
def test_bounded_reviewer_supplements_once_then_only_submits(monkeypatch,mode):
    from app import agentic
    from semora_agent import ReviewerAgent
    from services.api.tests.test_plan_candidates import setup,ONTOLOGY,PLAN
    from services.api.tests.test_agent_runtime import NativeModel
    monkeypatch.setenv('AGENT_QUERY_INTENT','false')
    monkeypatch.setenv('AGENT_BOUNDED_CONTEXT','true')
    async def scenario():
        workflow,run,_,_=setup()
        run.agent_memory['delivery_policy']=[{'mode':mode}]
        outcome=workflow.analyst._build_outcome(PLAN,run.question,[],ONTOLOGY,{},'test')
        class ObservedModel(NativeModel):
            offered=[]
            async def complete_messages(self,messages,tools,**kwargs):
                self.offered.append({t['function']['name'] for t in tools})
                return await super().complete_messages(messages,tools,**kwargs)
        model=ObservedModel([
            ('request_context',{'objects':['record'],'purpose':'filter','reason':'核对筛选字段的定义',
                                'field_refs':['record.amount'],'acceptance':'字段支持数值比较'}),
            ('AgentSubmission',{'payload':{'decision':'approved','rationale':'筛选与字段定义一致'}})])
        workflow.reviewer=ReviewerAgent(model)
        decision=await agentic.review_plan(workflow,run,ONTOLOGY,run.question,outcome)
        assert decision.decision=='approved' and len(model.messages)==2
        assert run.agent_memory['review_budgets'][0]['plan_preflight']['attempts']==2
        assert len(run.agent_memory['review_context_reads'])==1
        assert model.offered[-1]=={'AgentSubmission'}
    asyncio.run(scenario())
