from datetime import datetime, timedelta, timezone
import copy
import json

from semora_contracts import ConfirmedFact, QueryRun
from app.task_memory import build_memory, memory_context, read_artifact, repair_context
from services.api.tests.test_plan_candidates import setup, ONTOLOGY


def fact(value, question_id='scope'):
    return ConfirmedFact(question_id=question_id, field='status', value=value, label=value)


def test_followup_memory_keeps_previous_sort_limit_and_filters_visible():
    _,run,_,_=setup()
    previous={'order_by':[{'field':'item.id','direction':'desc'}],'limit':3,
              'filters':[{'field':'item.id','operator':'is_not_null'}]}
    run.continuation={'source_run_id':'previous','confirmed_requirement':previous}
    run.question='只把数量改为2条，其余不变'
    context=memory_context(build_memory(run,ONTOLOGY,{}),run.question)
    visible=context['previous_confirmed_execution']
    assert visible['values']==previous and visible['requires_revalidation']
    assert visible['source_run_id']=='previous' and context['facts']==[]


def test_user_overrides_keep_provenance_and_conflicts_without_promoting_model_text():
    _, run, _, _ = setup()
    run.continuation = {'confirmed_facts': [fact('active').model_dump()], 'source_run_id': 'old', 'question_history': ['不要包含取消记录']}
    run.user_answers = [fact('not_cancelled')]
    run.agent_memory['analyst'] = [{'type': 'ai', 'data': {'content': 'MODEL_GUESSED_FACT'}}]
    saved = build_memory(run, ONTOLOGY, {})
    assert saved['facts'][0]['fact']['value'] == 'not_cancelled'
    assert len(saved['fact_history']) == 2
    assert 'MODEL_GUESSED_FACT' not in json.dumps(saved)
    assert saved['request_history'] == ['不要包含取消记录']
    run.user_answers.append(fact('cancelled', 'another_question'))
    assert build_memory(run, ONTOLOGY, {})['conflicts'][0]['field'] == 'status'


def test_memory_roundtrip_restores_full_candidate_and_rejects_changed_bindings():
    _, run, _, _ = setup()
    candidate = {'plan': {'source_steps': [{'inputs': [{'key_pairs': [{'source_field': 'a.id', 'target_field': 'b.id'}, {'source_field': 'a.kind', 'target_field': 'b.kind'}]}]}]}}
    run.agent_memory['last_candidate'] = [candidate]
    snap = build_memory(run, ONTOLOGY, {})
    payload = {**run.model_dump(mode='json'), 'agent_memory': run.agent_memory}
    restored = QueryRun.model_validate(json.loads(json.dumps(payload)))
    key = next(iter(snap['artifacts']))
    assert read_artifact(restored, ONTOLOGY, {}, key)['artifact']['value'] == candidate
    assert not read_artifact(restored, {**ONTOLOGY, 'version': '2'}, {}, key)['available']
    assert not read_artifact(restored, ONTOLOGY, {'tenant': 'other'}, key)['available']
    assert memory_context(snap)['artifact_refs'][0]['id'] == key
    assert 'artifacts' not in memory_context(snap)


def test_evidence_age_and_failure_handoff(monkeypatch):
    from services.api.tests.test_workflow import fake_retrieve
    import asyncio
    _, run, _, _ = setup()
    run.evidence = asyncio.run(fake_retrieve('query'))
    now = datetime.now(timezone.utc)
    run.continuation = {'evidence_origin_at': (now - timedelta(hours=1)).isoformat()}
    snap = build_memory(run, ONTOLOGY, {}, now=now)
    assert snap['evidence'] and snap['evidence'][0]['requires_refresh']
    assert snap['evidence'][0]['age_seconds'] == 3600
    next_run = QueryRun(session_id=run.session_id, domain=run.domain, question='继续', continuation={'task_memory': snap})
    newer = build_memory(next_run, ONTOLOGY, {'changed': True})
    assert newer['binding_changed']
    assert newer['artifacts'] == {}


def test_timeout_is_not_treated_as_sql_repair():
    assert repair_context('ReadTimeout')['kind'] == 'model_timeout'
    assert repair_context('编译失败')['kind'] == 'compilation'
    assert repair_context('数据库执行失败')['kind'] == 'execution'
    assert repair_context('字段无效')['kind'] == 'plan_structure'


def test_evidence_ttl_boundary_matches_workflow():
    import asyncio
    from services.api.tests.test_workflow import fake_retrieve
    from app.evidence_policy import EVIDENCE_REUSE_TTL_SECONDS
    _, run, _, _ = setup()
    run.evidence = asyncio.run(fake_retrieve('query'))
    now = datetime.now(timezone.utc)
    for age, expired in [(899, False), (900, False), (901, True)]:
        run.continuation = {'evidence_origin_at': (now - timedelta(seconds=age)).isoformat()}
        assert build_memory(run, ONTOLOGY, {}, now=now)['evidence'][0]['requires_refresh'] == expired
    assert EVIDENCE_REUSE_TTL_SECONDS == 900


def test_a_b_a_reconfirmation_does_not_restore_b_after_restart():
    _, run, _, _ = setup()
    for value in ['A', 'B', 'A']:
        run.user_answers = [fact(value)]
        snapshot = build_memory(run, ONTOLOGY, {})
        assert snapshot['facts'][0]['fact']['value'] == value
    restored = QueryRun.model_validate(json.loads(json.dumps({**run.model_dump(mode='json'), 'agent_memory': run.agent_memory})))
    assert build_memory(restored, ONTOLOGY, {})['facts'][0]['fact']['value'] == 'A'
    assert snapshot['fact_history'][-1]['fact']['value'] == 'A'


def test_changed_binding_never_relabels_old_confirmation_as_current():
    _, run, _, _ = setup()
    run.continuation = {'task_memory': {'binding': 'old'}, 'confirmed_requirement': {'version': 'old', 'source_steps': []}}
    snapshot = build_memory(run, ONTOLOGY, {})
    key = next(iter(snapshot['artifacts']))
    assert snapshot['artifacts'][key]['origin_binding'] == 'old'
    assert snapshot['artifacts'][key]['requires_revalidation']
    assert not read_artifact(run, ONTOLOGY, {}, key)['available']


def test_model_projection_does_not_duplicate_identical_current_request():
    _, run, _, _ = setup()
    snapshot = build_memory(run, ONTOLOGY, {})
    projected = memory_context(snapshot, run.question)
    assert 'current_request' not in projected
    assert snapshot['current_request'] == run.question
    assert memory_context(snapshot, 'different request')['current_request'] == run.question


def test_reviewer_receives_only_exact_bound_preflight_proof():
    import asyncio
    from app import agentic
    from app.task_memory import digest, binding
    from semora_agent import ReviewerAgent
    from services.api.tests.test_agent_runtime import NativeModel
    from services.api.tests.test_workflow import fake_snapshot
    async def scenario():
        workflow, _, _, _ = setup()
        run = await workflow.start('demo-session', '各分行贷款余额', approval_mode='disabled')
        _, ontology = await fake_snapshot('retail_credit')
        run.agent_memory['approved_preflight'] = [{'plan_hash': digest(run.plan.model_dump(mode='json')),
            'binding': binding(run, ontology, run.plan.data_scope), 'review': {'decision': 'approved', 'rationale': '已有预检'}}]
        for changed in (False, True):
            if changed: run.plan.data_scope = {'branch_ids': [999]}
            model = NativeModel([('AgentSubmission', {'payload': {'decision': 'approved', 'issues': [], 'rationale': '独立核对完成'}})])
            workflow.reviewer = ReviewerAgent(model)
            await agentic.review(workflow, run, ontology)
            context = json.loads(next(m['content'] for m in model.messages[0] if m['role'] == 'user'))
            assert ('verified_preflight' in context) == (not changed)
    asyncio.run(scenario())
