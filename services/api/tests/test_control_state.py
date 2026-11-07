"""Explicit persistence and authority boundaries for role workspaces and execution."""
import asyncio,json
import pytest
from app.control_state import run_payload,restore_run,record_preflight,preflight_case,review_input_hash,invalidate_execution
from app.task_memory import digest
from semora_contracts import QueryRun,ReviewDecision,ReviewFinding
from services.api.tests.test_source_execution import harness,ontology


def test_execution_control_roundtrips_outside_agent_workspace(monkeypatch):
    async def scenario():
        w,session,_=harness(monkeypatch)
        run=await w.start(session.id,'查询',domain='commerce')
        assert run.execution_binding and len(run.execution_receipts)==2
        assert 'source_execution' not in run.agent_memory
        payload=json.loads(json.dumps(run_payload(run)))
        restored=restore_run(payload)
        assert restored.execution_binding==run.execution_binding
        assert restored.execution_receipts==run.execution_receipts
        public=restored.model_dump(mode='json')
        assert not {'execution_binding','execution_receipts','review_case','source_results','agent_memory'} & public.keys()
        for step,result in restored.source_results.items():
            assert restored.execution_receipts[step].output_hash==digest(result.model_dump(mode='json'))
    asyncio.run(scenario())


def test_persistent_review_case_retains_issue_id_resolution_and_binding(monkeypatch):
    async def scenario():
        w,session,_=harness(monkeypatch)
        run=await w.start(session.id,'查询',domain='commerce')
        finding=ReviewFinding(kind='repair',paths=['/filters'],message='缺少阈值',evidence='用户确认',acceptance='补齐阈值')
        decision=ReviewDecision(decision='revise',findings=[finding],rationale='缺少条件')
        pending=record_preflight(run,run.plan,ontology(),{},decision,1,[])
        first_id=run.review_case.id
        restored=restore_run(json.loads(json.dumps(run_payload(run))))
        assert restored.review_case.pending[0].id==pending[0]['id']
        approved=ReviewDecision(decision='approved',rationale='已补齐',resolutions=[{'finding_id':pending[0]['id'],'status':'resolved','rationale':'字段修复'}])
        record_preflight(restored,restored.plan,ontology(),{},approved,2,pending)
        assert restored.review_case.id==first_id
        assert restored.review_case.review.resolutions[0].finding_id==pending[0]['id']
        assert restored.review_case.pending==[]
        assert 'approved_preflight' not in restored.agent_memory
        assert restored.review_case.input_hash==review_input_hash(restored)
        restored.question='另一问题'
        assert restored.review_case.input_hash!=review_input_hash(restored)
        invalidate_execution(restored)
        assert restored.review_case is None and restored.execution_binding=='' and restored.execution_receipts=={}
    asyncio.run(scenario())


def test_legacy_control_payload_migrates_once_with_real_audit_receipts(monkeypatch):
    async def scenario():
        w,session,_=harness(monkeypatch)
        run=await w.start(session.id,'查询',domain='commerce')
        payload=run_payload(run)
        payload.pop('review_case');payload.pop('execution_receipts');payload.pop('execution_binding')
        payload['agent_memory']['source_execution']=[{'binding':run.execution_binding}]
        payload['agent_memory']['approved_preflight']=[{'plan_hash':'old-proof','binding':'old-binding','review':{'decision':'approved','rationale':'历史核验'}}]
        restored=restore_run(json.loads(json.dumps(payload)))
        assert restored.execution_binding==run.execution_binding
        assert len(restored.execution_receipts)==2
        assert restored.review_case.plan_hash=='old-proof'
        assert 'approved_preflight' not in restored.agent_memory and 'source_execution' not in restored.agent_memory
    asyncio.run(scenario())


def test_tampered_typed_receipt_blocks_approval_resume(monkeypatch):
    async def scenario():
        from app.settings import settings_service
        from semora_contracts import RunStatus
        settings=settings_service.get();settings.approval_mode='all'
        monkeypatch.setattr(settings_service,'get',lambda:settings.model_copy(deep=True))
        w,session,calls=harness(monkeypatch)
        run=await w.start(session.id,'查询',domain='commerce')
        await w.approve(run,'approver',True,'允许首步')
        assert run.status==RunStatus.AWAITING_APPROVAL
        receipt=run.execution_receipts['pick_clients']
        run.execution_receipts['pick_clients']=receipt.model_copy(update={'output_hash':'tampered'})
        await w.approve(run,'approver',True,'允许后步')
        assert run.status==RunStatus.FAILED and len(calls)==1
        assert '审计摘要不一致' in run.error
    asyncio.run(scenario())


def test_invalid_persistent_approval_is_not_restored_as_authority():
    payload=QueryRun(session_id='test',domain='generic',question='q').model_dump(mode='json')
    payload['review_case']={'plan_hash':'h','binding':'b','review':{
        'decision':'approved','rationale':'invalid','resolutions':[{'finding_id':'open','status':'unresolved','rationale':'not fixed'}]}}
    with pytest.raises(ValueError,match='未解决'):
        restore_run(payload)
