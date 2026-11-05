import asyncio
import copy
import json
from datetime import timedelta

from app import agentic
from app.store import Store
from app.workflow import Workflow
from semora_agent import AnalystAgent, ReviewerAgent
from semora_agent.model import StructuredModel, ModelError
from semora_contracts import ConfirmedFact, QueryRun, RunStatus
from services.api.tests.test_agent_runtime import NativeModel, PLAN
from services.api.tests.test_workflow import fake_snapshot, fake_execute, fake_retrieve


async def seed():
    w = Workflow(Store(), fake_execute, fake_retrieve, fake_snapshot)
    source, _ = w._prepare_run('demo-session', '按地区统计金额', 'retail_credit', None, None)
    source.ontology, ontology = await fake_snapshot('retail_credit')
    source.user_answers = [ConfirmedFact(question_id='time', field='time_range', value='全部时间', label='全部时间')]
    source.evidence = await fake_retrieve('业务口径')
    outcome = AnalystAgent(StructuredModel())._build_outcome(PLAN, source.question, source.evidence, ontology, {}, 'test')
    source.agent_memory['last_candidate'] = [{'requirement': outcome.requirement.model_dump(mode='json'), 'plan': outcome.plan.model_dump(mode='json')}]
    w.emit(source, 'agent.model.started', RunStatus.UNDERSTANDING, {'agent': 'analyst', 'iteration': 1})
    w.emit(source, 'plan.preflight.completed', RunStatus.UNDERSTANDING, {'review': {'decision': 'revise', 'issues': ['补齐状态过滤'], 'rationale': '口径需要落实'}})
    source.error = '方案修订失败'
    w.emit(source, 'run.failed', RunStatus.FAILED, {'error': source.error})
    return w, source, ontology


def test_failed_followup_restores_business_handoff_but_no_execution_authority():
    async def scenario():
        w, source, ontology = await seed()
        before = copy.deepcopy(source.model_dump(mode='json'))
        run, _ = w._prepare_run('demo-session', '只统计有效记录', source.domain, None, source.authorization)
        assert run.id != source.id and run.continuation['source_run_id'] == source.id
        assert run.continuation['question_history'] == ['按地区统计金额']
        assert run.continuation['candidate_plan']['metrics'] == PLAN['metrics']
        assert run.continuation['review_feedback']['review']['issues'] == ['补齐状态过滤']
        assert '全部时间' in w._analysis_question(run) and '只统计有效记录' in w._analysis_question(run)
        assert all(x is None for x in (run.sql, run.plan, run.requirement, run.gate, run.review, run.approval, run.result))
        assert run.agent_memory == {} and source.model_dump(mode='json') == before
        w.analyst = AnalystAgent(NativeModel([('AgentSubmission', {'payload': {**PLAN, 'filters': [{'field': 'loan.status', 'operator': 'eq', 'value': 'active'}]}})]))
        w.reviewer = ReviewerAgent(StructuredModel())
        outcome = await agentic.analyse(w, run, w._analysis_question(run), ontology, {})
        assert outcome.plan.filters[0].value == 'active'
        context = json.loads(next(x['content'] for x in w.analyst.model.messages[0] if x['role'] == 'user'))
        assert context['continuation']['source_run_id'] == source.id
        event = next(e for e in run.events if e.event == 'agent.model.started')
        assert event.data['iteration'] == 2 and event.data['role_iteration'] == 2
        # The handoff and new typed answers survive persistence/hydration.
        restored = QueryRun.model_validate(run.model_dump(mode='json'))
        assert restored.continuation == run.continuation and restored.user_answers == run.user_answers
    asyncio.run(scenario())


def test_new_request_and_changed_permissions_never_reuse_failed_plan():
    async def scenario():
        for mode, text, changed in [('new', '其他问题', False), ('auto', '新需求：分析库存', False), ('auto', '继续', True)]:
            w, source, _ = await seed()
            auth = source.authorization.model_copy(deep=True)
            if changed:
                auth.branch_ids = [999]
            run, _ = w._prepare_run('demo-session', text, source.domain, None, auth, mode)
            assert run.continuation is None and run.message_mode == 'new'
            assert w._analysis_question(run) == text and not run.evidence
    asyncio.run(scenario())


def test_repeated_failure_keeps_original_goal_and_candidate_without_copying_raw_agent_memory():
    async def scenario():
        w, source, _ = await seed()
        source.agent_memory['analyst'] = [
            {'type': 'ai', 'data': {'content': 'PRIVATE_MODEL_TEXT'}},
            {'type': 'human', 'data': {'content': json.dumps({'request': '原需求。用户补充确认：status=有效'})}},
        ]
        run, _ = w._prepare_run('demo-session', '再试一次', source.domain, None, source.authorization)
        assert run.continuation['legacy_confirmations'] == 'status=有效'
        run.status = RunStatus.FAILED
        following, _ = w._prepare_run('demo-session', '保留相同口径', source.domain, None, source.authorization)
        assert following.continuation['root_run_id'] == source.id
        assert following.continuation['question_history'] == ['按地区统计金额', '再试一次']
        assert following.continuation['candidate_plan'] is not None
        assert 'PRIVATE_MODEL_TEXT' not in json.dumps(following.model_dump(mode='json'))
    asyncio.run(scenario())


def test_evidence_reuse_requires_current_binding_and_bounded_original_age():
    async def scenario():
        for expired, changed in [(False, False), (True, False), (False, True)]:
            w, source, _ = await seed()
            if expired:
                source.created_at -= timedelta(hours=1)
            if changed:
                source.ontology.version = 'old'
            # Native tool mode lets the Analyst decide what to retrieve.
            w.analyst = AnalystAgent(NativeModel([]))
            run, _ = w._prepare_run('demo-session', '继续', source.domain, None, source.authorization)
            await w._retrieve_node({'run_id': run.id})
            assert bool(run.evidence) == (not expired and not changed)
            assert run.continuation['bindings_changed'] == changed
    asyncio.run(scenario())


def test_answer_is_persisted_even_when_supplementary_analysis_fails(monkeypatch):
    async def scenario():
        w = Workflow(Store(), fake_execute, fake_retrieve, fake_snapshot)
        auth = (await seed())[1].authorization
        # Use an actual interrupt/resume so answers cannot be lost on failure.
        from services.api.tests.test_agent_runtime import CARD
        w.analyst = AnalystAgent(NativeModel([('AgentSubmission', {'payload': CARD})]))
        run = await w.start_background('demo-session', '统计金额', auth)
        for _ in range(100):
            if run.status in {RunStatus.CLARIFYING, RunStatus.FAILED}:
                break
            await asyncio.sleep(.01)
        assert run.status == RunStatus.CLARIFYING
        async def fail(*args, **kwargs):
            raise ModelError('诊断失败')
        monkeypatch.setattr(w, '_analyse_with_tools', fail)
        await w.align(run, 'test', {'time': '全部时间'})
        assert run.status == RunStatus.FAILED
        assert any(f.value == '全部时间' for f in run.user_answers)
        next_run, _ = w._prepare_run('demo-session', '继续', run.domain, None, auth)
        assert '全部时间' in w._analysis_question(next_run)
    asyncio.run(scenario())
