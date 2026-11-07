"""Candidate authority is scoped to the current run, facts and published inputs."""
import asyncio
import json
import pytest
from semora_contracts import ConfirmedFact
from services.api.tests.test_plan_candidates import setup,PLAN


@pytest.fixture(autouse=True)
def registry_legacy_adapter(monkeypatch):
    """These toolkit fixtures deliberately exercise the old validation tool."""
    monkeypatch.setenv('AGENT_QUERY_INTENT','false')


def test_changed_confirmed_answer_invalidates_already_validated_candidate():
    async def scenario():
        _,run,kit,tool=setup()
        receipt=json.loads(await tool.ainvoke({'analysis':PLAN}))
        assert receipt['valid']
        run.user_answers.append(ConfirmedFact(question_id='scope',field='record.amount',value='20',label='阈值20'))
        with pytest.raises(ValueError,match='失效'):
            kit.resolve_submission({'candidate_id':receipt['candidate_id']})
    asyncio.run(scenario())


def test_mutating_candidate_snapshot_cannot_replace_canonical_analysis():
    async def scenario():
        _,_,kit,tool=setup()
        receipt=json.loads(await tool.ainvoke({'analysis':PLAN}))
        snapshot=kit.candidates
        snapshot[receipt['candidate_id']]['analysis']['filters']=[]
        assert kit.resolve_submission({'candidate_id':receipt['candidate_id']})['filters']==PLAN['filters']
    asyncio.run(scenario())


def test_registry_binding_covers_run_and_phase_and_copies_inputs():
    from semora_agent.candidates import CandidateRegistry,CandidateUnavailable
    from semora_agent.task_contracts import ArtifactBinding
    from dataclasses import replace
    binding=ArtifactBinding.capture(run_id='r',session_id='s',domain='d',phase=1,question='q',
        ontology={'v':1},scope={'allowed':['one']},authorization={'subject':'u'},requirement=None,facts=[])
    registry=CandidateRegistry();payload={'filters':[{'value':10}]}
    identity=registry.register(payload,binding)
    payload['filters'].clear()
    assert registry.resolve(identity,binding)['filters']==[{'value':10}]
    for changed in [replace(binding,run_id='other'),replace(binding,session_id='other'),replace(binding,phase=2),
                    replace(binding,authorization_hash='changed'),replace(binding,ontology_hash='changed')]:
        with pytest.raises(CandidateUnavailable):registry.resolve(identity,changed)
    registry.clear()
    assert registry.valid_ids(binding)==[]
    with pytest.raises(CandidateUnavailable):registry.replace_validated(identity,{},binding)


def test_option_description_change_invalidates_the_candidate():
    async def scenario():
        _,run,kit,tool=setup()
        run.agent_memory['confirmed_fact_context']=[{'question_id':'scope','selected_option_description':'全部历史'}]
        receipt=json.loads(await tool.ainvoke({'analysis':PLAN}))
        run.agent_memory['confirmed_fact_context'][0]['selected_option_description']='仅选取窗口'
        with pytest.raises(ValueError,match='失效'):
            kit.resolve_submission({'candidate_id':receipt['candidate_id']})
    asyncio.run(scenario())


def test_typed_task_contract_preserves_full_handoff_wire_shape():
    from app.task_handoff import handoff
    from semora_agent.task_contracts import TaskContract
    from services.api.tests.test_task_handoff import packet
    w,run,context=packet()
    value,guard=handoff(w,run,'analyst','interpret_and_plan',context)
    contract=TaskContract.model_validate(run.agent_memory['task_contracts'][-1])
    assert all(contract.model_dump(mode='json')[k]==v for k,v in value['task_contract'].items())
    assert 'bindings' not in value['task_contract']
    assert value['confirmed_fact_context']==context['confirmed_fact_context']
    guard()
    run.user_answers[0].value='new value'
    from semora_agent.model import TaskSupersededError
    with pytest.raises(TaskSupersededError):guard()


def test_default_candidate_uses_registry_without_legacy_plan_tool(monkeypatch):
    async def scenario():
        from app.agentic import AgentTools
        from app.workflow import Workflow
        from app.store import Store
        from services.api.tests.test_workflow import fake_execute,fake_retrieve,fake_snapshot
        from services.api.tests.test_plan_candidates import ONTOLOGY
        from semora_contracts import QueryRun
        monkeypatch.delenv('AGENT_QUERY_INTENT',raising=False)
        w=Workflow(Store(),fake_execute,fake_retrieve,fake_snapshot)
        r=QueryRun(session_id='default',domain='generic',question='列出记录')
        kit=AgentTools(w,r,ONTOLOGY,{})
        kit.start_phase('default intent')
        tools={t.name:t for t in kit.tools()}
        assert 'validate_intent' in tools and 'validate_plan' not in tools
        receipt=json.loads(await tools['validate_intent'].ainvoke({'intent':{'summary':'列出记录','subject':'record','fields':['record.id']}}))
        assert receipt['valid'],receipt
        r.user_answers.append(ConfirmedFact(question_id='choice',field='record.id',value='new',label='新口径'))
        with pytest.raises(ValueError,match='失效'):
            kit.resolve_submission({'candidate_id':receipt['candidate_id']})
    asyncio.run(scenario())
