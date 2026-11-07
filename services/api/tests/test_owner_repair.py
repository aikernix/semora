"""Recoverable review failures stay internal, without bypassing review."""
import asyncio,json
import pytest
from app import prepared_context
from semora_agent import AnalystAgent,ReviewerAgent
from semora_contracts import ReviewDecision,ReviewFinding,RunStatus
from semora_agent.review_policy import issue_ledger,validate_resolution
from services.api.tests.test_source_execution import harness
from services.api.tests.test_agent_runtime import NativeModel


@pytest.mark.parametrize('business_correction',[False,True])
def test_owner_recompile_and_targeted_review_can_recover(monkeypatch,business_correction):
    async def scenario():
        monkeypatch.setenv('AGENT_QUERY_INTENT','true');monkeypatch.setenv('AGENT_BOUNDED_CONTEXT','true')
        w,session,calls=harness(monkeypatch)
        intent=dict(summary='查询关联数值',subject='client',fields=['balances.amount'])
        corrected={**intent,'filters':[dict(field='client.status',operator='eq',value='active')]}
        technical=ReviewDecision(decision='revise',rationale='核验筛选范围',issues=['需核对来源条件'],findings=[ReviewFinding(
            kind='repair',paths=['/source_steps/0/query/filters'],message='核对条件',evidence='当前需求',acceptance='范围完整')])
        business=ReviewDecision(decision='revise',rationale='应补齐业务声明',issues=['缺少有效状态'],findings=[ReviewFinding(
            kind='repair',paths=['/query_intent/filters'],message='遗漏状态条件',evidence='用户确认',acceptance='筛选active')])
        pending=issue_ledger(business,[])
        approved={'decision':'approved','rationale':'方案与执行一致'}
        actions=[technical.model_dump(exclude={'phase','reviewer_mode','resolutions'})]
        if business_correction:
            actions += [business.model_dump(exclude={'phase','reviewer_mode','resolutions'}),
                {**approved,'resolutions':[dict(finding_id=pending[0]['id'],status='resolved',rationale='条件已落实')]}]
        else:actions += [approved]
        actions += [approved,approved]
        model=NativeModel([('validate_intent',{'intent':intent})]+([('validate_intent',{'intent':corrected})] if business_correction else []))
        reviewer=NativeModel([('AgentSubmission',{'payload':a}) for a in actions])
        w.analyst=AnalystAgent(model);w.reviewer=ReviewerAgent(reviewer)
        async def prepare(workflow,run,question,ont,scope):return {'packet':prepared_context.context_packet(workflow,run,ont,['client','balances']),'supplements':0}
        monkeypatch.setattr(prepared_context,'prepare_context',prepare)
        run=await w.start(session.id,'查询关联数值，已确认需要有效状态' if business_correction else '查询关联数值',domain='commerce')
        assert run.status==RunStatus.COMPLETED,run.error
        assert len(calls)==2
        assert run.result.rows==([[10],[20]] if business_correction else [[10],[20],[999]])
        assert any(e.event=='planning.repair.started' for e in run.events)
        assert any(e.event=='planning.repair.completed' for e in run.events)
        assert 'owner_repair_diagnosis' not in run.agent_memory
        diagnosis=json.loads(next(m['content'] for m in reviewer.messages[1] if m['role']=='user'))
        assert diagnosis['owner_repair_diagnosis']['business_intent_unchanged']
        assert len(model.messages)==(2 if business_correction else 1)
        assert w.store.audit.verify(run.id)==(True,None)
    asyncio.run(scenario())


def test_optional_existence_is_a_valid_business_repair_target():
    from services.api.tests.test_result_regroup import fixture,build
    ont,intent=fixture();plan=build(ont,intent).plan
    decision=ReviewDecision(decision='revise',rationale='关联条件缺失',findings=[ReviewFinding(
        kind='repair',paths=['/query_intent/existence'],message='缺少存在性条件',evidence='用户要求',acceptance='声明存在性')])
    validate_resolution(decision,[],plan)


def test_execution_review_recompiles_and_rechecks_without_restarting_analyst(monkeypatch):
    async def scenario():
        monkeypatch.setenv('AGENT_QUERY_INTENT','true');monkeypatch.setenv('AGENT_BOUNDED_CONTEXT','true')
        w,session,calls=harness(monkeypatch)
        intent=dict(summary='查询关联数值',subject='client',fields=['balances.amount'])
        model=NativeModel([('validate_intent',{'intent':intent})])
        approved=dict(decision='approved',rationale='执行契约一致')
        revision=dict(decision='revise',issues=['定向核对SQL投影'],rationale='核对程序产物',findings=[dict(
            kind='repair',paths=['/source_steps/1/query/fields'],message='核对投影',evidence='当前SQL',acceptance='SQL投影与声明一致')])
        reviewer=NativeModel([('AgentSubmission',{'payload':p}) for p in [approved,revision,approved,approved]])
        w.analyst=AnalystAgent(model);w.reviewer=ReviewerAgent(reviewer)
        async def prepare(workflow,run,question,ont,scope):return {'packet':prepared_context.context_packet(workflow,run,ont,['client','balances']),'supplements':0}
        monkeypatch.setattr(prepared_context,'prepare_context',prepare)
        run=await w.start(session.id,'查询关联数值',domain='commerce')
        assert run.status==RunStatus.COMPLETED,run.error
        assert len(model.messages)==1 and len(calls)==2 and run.revision_count==1
        context=json.loads(next(m['content'] for m in reviewer.messages[2] if m['role']=='user'))
        assert context['owner_repair_diagnosis']['operation']=='recompile_frozen_plan'
        assert 'owner_repair_diagnosis' not in run.agent_memory
        assert w.store.audit.verify(run.id)==(True,None)
    asyncio.run(scenario())
