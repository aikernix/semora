import asyncio
import pytest

from app.settings import settings_service
from semora_agent import AnalystAgent,ReviewerAgent
from semora_agent.model import NoDeliveryError
from semora_contracts import QueryRun,RunStatus
from services.api.tests.test_agent_runtime import NativeModel
from services.api.tests.test_source_execution import harness


@pytest.fixture
def delivery_settings(monkeypatch):
    settings=settings_service.get();settings.delivery_mode='best_effort'
    monkeypatch.setattr(settings_service,'get',lambda:settings.model_copy(deep=True))
    return settings


def agents(w,reviewer):
    w.analyst=AnalystAgent(NativeModel([('validate_intent',{'intent':{
        'summary':'查询关联数值','subject':'client','fields':['balances.amount']}})]))
    w.reviewer=ReviewerAgent(reviewer)


def test_three_review_disagreements_deliver_real_results_without_fake_approval(monkeypatch,delivery_settings):
    async def scenario():
        w,session,calls=harness(monkeypatch)
        responses=[{'decision':'revise','rationale':'建议进一步核对口径','findings':[{
            'kind':'repair','paths':['/query_intent/unknown'],'message':'核对口径','acceptance':'口径一致'}]},
            {'decision':'rejected','rationale':'建议调整排序','findings':[{
                'kind':'repair','paths':['/order_by'],'message':'排序口径待确认','acceptance':'按确认顺序'}]},
            {'decision':'revise','rationale':'建议进一步核对解释','findings':[{
                'kind':'repair','paths':['/fields'],'message':'输出列建议调整','acceptance':'确认输出列'}]}]
        model=NativeModel([('AgentSubmission',{'payload':value}) for value in responses]);agents(w,model)
        run=await w.start(session.id,'查询关联数值',domain='commerce')
        assert run.status==RunStatus.COMPLETED,run.error
        assert run.result.rows==[[10],[20],[999]] and len(calls)==2
        assert run.review.decision=='rejected' and run.review.disposition=='advisory'
        assert run.result_review.decision=='revise' and run.result_review.disposition=='advisory'
        assert len(model.messages)==3
        assert run.delivery.kind=='result' and not run.delivery.result_verified
        assert len(run.delivery.warnings)==3 and run.answer
        assert run.revision_count==0 and w.store.audit.verify(run.id)==(True,None)
    asyncio.run(scenario())


def test_reviewer_service_unavailable_keeps_query_results(monkeypatch,delivery_settings):
    class Offline(NativeModel):
        request_count=0
        async def complete_messages(self,*args,**kwargs):
            self.request_count+=1;raise NoDeliveryError('no delivery')
    async def scenario():
        w,session,calls=harness(monkeypatch);reviewer=Offline([]);agents(w,reviewer)
        run=await w.start(session.id,'查询关联数值',domain='commerce')
        assert run.status==RunStatus.COMPLETED,run.error
        assert run.result.rows==[[10],[20],[999]] and len(calls)==2
        assert reviewer.request_count==3
        assert run.review.reviewer_mode=='unavailable' and run.review.disposition=='advisory'
        assert run.result_review.reviewer_mode=='unavailable' and not run.delivery.result_verified
        assert run.delivery.warnings and '待核对' in run.answer
    asyncio.run(scenario())


def test_strict_mode_still_requires_model_review(monkeypatch,delivery_settings):
    delivery_settings.delivery_mode='strict'
    class Offline(NativeModel):
        async def complete_messages(self,*args,**kwargs):raise NoDeliveryError('offline')
    async def scenario():
        w,session,calls=harness(monkeypatch);agents(w,Offline([]))
        run=await w.start(session.id,'查询关联数值',domain='commerce')
        assert run.status==RunStatus.FAILED and not calls
        assert run.error.startswith('Reviewer 方案预检失败')
    asyncio.run(scenario())


def test_cross_source_estimate_capability_loss_delivers_with_warning(monkeypatch,delivery_settings):
    from app.executor_errors import ExecutorOperationError
    async def scenario():
        w,session,calls=harness(monkeypatch)
        agents(w,NativeModel([('AgentSubmission',{'payload':{'decision':'approved','rationale':'一致'}})]*3))
        async def unavailable(*args):raise ExecutorOperationError('capability')
        w.estimate=unavailable
        run=await w.start(session.id,'查询关联数值',domain='commerce')
        assert run.status==RunStatus.COMPLETED,run.error
        assert run.result.rows==[[10],[20],[999]] and len(calls)==2
        assert run.delivery_warnings and any(e.event=='cost.estimate.degraded' for e in run.events)
    asyncio.run(scenario())


def test_cross_source_auth_failure_does_not_query_or_retry_sql(monkeypatch,delivery_settings):
    from app.executor_errors import ExecutorOperationError
    async def scenario():
        w,session,calls=harness(monkeypatch)
        agents(w,NativeModel([('AgentSubmission',{'payload':{'decision':'approved','rationale':'一致'}})]*2))
        estimates=[]
        async def denied(*args):estimates.append(True);raise ExecutorOperationError('auth',1045)
        w.estimate=denied
        run=await w.start(session.id,'查询关联数值',domain='commerce')
        assert run.status==RunStatus.FAILED and not calls and len(estimates)==1
        assert run.result is None and '账号' in run.delivery.next_action
    asyncio.run(scenario())


def test_unexpected_reviewer_bug_is_not_silently_advisory(monkeypatch,delivery_settings):
    class Broken(NativeModel):
        async def complete_messages(self,*args,**kwargs):raise TypeError('unexpected reviewer bug')
    async def scenario():
        w,session,calls=harness(monkeypatch);agents(w,Broken([]))
        run=await w.start(session.id,'查询关联数值',domain='commerce')
        assert run.status==RunStatus.FAILED and not calls and run.result is None
        assert 'unexpected reviewer bug' in run.error
    asyncio.run(scenario())


def test_execution_failure_delivers_saved_sql_and_explanation_without_partial_answer(monkeypatch,delivery_settings):
    async def scenario():
        w,session,calls=harness(monkeypatch,fail_b=True)
        agents(w,NativeModel([('AgentSubmission',{'payload':{'decision':'approved','rationale':'方案一致'}})]*2))
        run=await w.start(session.id,'查询关联数值',domain='commerce')
        assert run.status==RunStatus.FAILED and run.result is None
        assert run.delivery.kind=='blocked' and run.answer and run.sql
        assert run.delivery.next_action and len(calls)==2
        assert '查询返回' not in run.answer
    asyncio.run(scenario())


def test_readonly_gate_cannot_be_bypassed_by_advisory_review(monkeypatch,delivery_settings):
    from semora_contracts import GateDecision,GateCheck
    from app.source_execution import SourceExecution
    async def scenario():
        w,session,calls=harness(monkeypatch)
        agents(w,NativeModel([('AgentSubmission',{'payload':{'decision':'approved','rationale':'业务一致'}})]))
        monkeypatch.setattr(SourceExecution,'template_gate',lambda self:GateDecision(allowed=False,risk_level='high',requires_approval=False,checks=[GateCheck(name='readonly',passed=False,severity='blocking',message='查询未通过只读检查')]))
        run=await w.start(session.id,'查询关联数值',domain='commerce')
        assert run.status==RunStatus.FAILED and not calls and run.result is None
        assert not run.gate.allowed and run.delivery.kind=='blocked'
    asyncio.run(scenario())


def test_model_failure_without_candidate_still_delivers_a_continuable_response(monkeypatch,delivery_settings):
    class Offline(NativeModel):
        async def complete_messages(self,*args,**kwargs):raise NoDeliveryError('offline')
    async def scenario():
        w,session,calls=harness(monkeypatch);w.analyst=AnalystAgent(Offline([]))
        run=await w.start(session.id,'查询关联数值',domain='commerce')
        assert run.delivery.kind=='blocked' and run.answer and not calls
        assert run.sql is None and run.result is None and run.delivery.next_action
    asyncio.run(scenario())


def test_restart_converts_orphaned_active_status_into_explicit_delivery(monkeypatch,delivery_settings):
    w,session,_=harness(monkeypatch)
    run=QueryRun(session_id=session.id,status=RunStatus.REVIEWING,question='继续当前需求')
    w.store.runs[run.id]=run
    w.resume_pending_alignments()
    assert run.status==RunStatus.FAILED and run.delivery.kind=='interrupted'
    assert run.answer and run.delivery.next_action
    assert run.agent_memory['interruption']
