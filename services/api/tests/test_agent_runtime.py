import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from app import agentic
from app.store import Store
from app.workflow import Workflow
from semora_agent import AnalystAgent, ReviewerAgent
from semora_agent.model import ModelError, StructuredModel
from semora_agent.runtime import run_agent
from semora_contracts import AuthorizationContext, QueryRun, RunStatus, ReviewDecision, QueryFilter
from services.api.tests.test_workflow import fake_snapshot, fake_execute, fake_retrieve


PLAN = {'intent': '分行贷款余额', 'metrics': ['loan_balance'], 'dimensions': ['branch.name'], 'grain': 'branch'}
CARD = {'summary': '请确认时间', 'clarifications': [{'id': 'time', 'field': 'time_range', 'question': '统计哪个时间？', 'reason': '影响范围', 'options': []}]}


@pytest.mark.parametrize('domain,name,object_label,location_label', [
    ('inventory', '仓储管理', '商品', '仓库'),
    ('production', '生产管理', '工单', '车间'),
])
def test_agents_use_configured_business_domain_without_industry_defaults(domain, name, object_label, location_label):
    async def scenario():
        ontology = {
            'domain': domain, 'name': name, 'description': f'{name}数量分析', 'dataSourceId': f'{domain}-db',
            'knowledgeBaseIds': [],
            'objects': [
                {'name': 'item', 'label': object_label, 'table': 'items', 'properties': [{'name': 'quantity', 'column': 'quantity', 'label': '数量'}]},
                {'name': 'location', 'label': location_label, 'table': 'locations', 'properties': [{'name': 'name', 'column': 'name', 'label': f'{location_label}名称'}]},
            ],
            'relations': [{'name': 'item_at_location', 'from': 'item', 'to': 'location', 'condition': 'item.location_id = location.id', 'cardinality': 'N:1'}],
            'metrics': [{'name': 'quantity_total', 'label': '数量合计', 'expression': 'SUM(item.quantity)', 'grain': 'location'}],
        }
        proposal = {'intent': f'各{location_label}的{object_label}数量', 'metrics': ['quantity_total'], 'dimensions': ['location.name'], 'grain': 'location'}
        analyst = NativeModel([('read_object', {'name': 'item'}), ('AgentSubmission', {'payload': proposal})])
        reviewer = NativeModel([('read_object', {'name': 'location'}), ('AgentSubmission', {'payload': {'decision': 'approved', 'issues': [], 'rationale': '按已配置对象和关系统计数量'}})])
        workflow = Workflow(Store(), fake_execute, fake_retrieve, fake_snapshot)
        workflow.analyst, workflow.reviewer = AnalystAgent(analyst), ReviewerAgent(reviewer)
        run = QueryRun(session_id='demo-session', domain=domain, question=proposal['intent'])
        outcome = await agentic.analyse(workflow, run, run.question, ontology, {})
        assert outcome.plan.domain == domain
        assert outcome.plan.relationship_path == ['item_at_location']
        assert outcome.plan.metrics == ['quantity_total']
        for client in (analyst, reviewer):
            messages = client.messages[0]
            context = json.loads(next(m['content'] for m in messages if m['role'] == 'user'))
            assert context['business_domain'] == {key: ontology[key] for key in ('domain', 'name', 'description')}
            prompt = next(m['content'] for m in messages if m['role'] == 'system')
            assert not any(word in prompt for word in ['银行', '贷款', '逾期', '还款', '分行'])
        assert run.sql is None and run.result is None
    asyncio.run(scenario())


def test_agent_deadline_uses_configured_budget_and_cancels_pending_model(monkeypatch):
    monkeypatch.setenv('AGENT_TIMEOUT_SECONDS', '0.2')
    cancelled = []

    class SlowModel:
        async def complete_messages(self, *args, **kwargs):
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                cancelled.append(True)
                raise

    async def scenario():
        events = []
        with pytest.raises(ModelError, match='Agent 达到 0.2 秒总时限'):
            await run_agent(SlowModel(), '提交结果', {}, [], [], lambda name, data: events.append((name, data)))
        assert cancelled == [True]
        timeout = next(data for name, data in events if name == 'agent.analysis.timeout')
        assert timeout['model_calls'] == 1 and timeout['tool_calls'] == 0
        assert timeout['timeouts'] == 0 and timeout['retries'] == 0

    asyncio.run(scenario())


class NativeModel:
    configured = required = True

    def __init__(self, calls):
        self.calls = iter(calls)
        self.messages = []

    async def complete_messages(self, messages, tools, **kwargs):
        self.messages.append(messages)
        name, args = next(self.calls)
        assert name in {t['function']['name'] for t in tools}
        return {'content': 'This text must not be persisted.', 'reasoning_content': 'PRIVATE_REASONING', 'tool_calls': [
            {'id': f'call-{len(self.messages)}', 'type': 'function', 'function': {'name': name, 'arguments': json.dumps(args)}}
        ]}

    async def complete_json(self, *args):
        raise AssertionError('The Agent must use native tool calls, not one-shot JSON')


def test_analyst_observes_tool_results_and_resumes_memory_for_followup():
    async def scenario():
        workflow = Workflow(Store(), fake_execute, fake_retrieve, fake_snapshot)
        ref, ontology = await fake_snapshot('retail_credit')
        run = QueryRun(session_id='demo-session', question='分行贷款余额', ontology=ref)
        model = NativeModel([
            ('read_object', {'name': 'loan'}),
            ('AgentSubmission', {'payload': CARD}),
            ('read_object', {'name': 'branch'}),
            ('validate_plan', {'analysis': PLAN}),
            ('AgentSubmission', {'payload': PLAN}),
        ])
        workflow.analyst = AnalystAgent(model)
        first = await agentic.analyse(workflow, run, run.question, ontology, {})
        assert first.plan is None
        second = await agentic.analyse(workflow, run, '全部数据', ontology, {})
        assert second.plan.metrics == ['loan_balance']
        assert len(model.messages) == 5
        assert any(m['role'] == 'tool' and 'outstanding_balance' in str(m['content']) for m in model.messages[1])
        assert any(m['role'] == 'user' and '分行贷款余额' in str(m['content']) for m in model.messages[2])
        assert 'PRIVATE_REASONING' not in json.dumps(run.agent_memory)
        assert 'This text must not be persisted.' not in json.dumps(run.agent_memory)
        assert 'agent_memory' not in run.model_dump()
        assert any(e.event == 'agent.tool.completed' for e in run.events)
    asyncio.run(scenario())


def test_invalid_submission_is_returned_to_agent_for_correction():
    async def scenario():
        workflow = Workflow(Store(), fake_execute, fake_retrieve, fake_snapshot)
        ref, ontology = await fake_snapshot('retail_credit')
        run = QueryRun(session_id='demo-session', question='贷款余额', ontology=ref)
        model = NativeModel([
            ('AgentSubmission', {'payload': {**PLAN, 'fields': ['loan.not_a_field']}}),
            ('read_object', {'name': 'loan'}),
            ('AgentSubmission', {'payload': PLAN}),
        ])
        workflow.analyst = AnalystAgent(model)
        outcome = await agentic.analyse(workflow, run, run.question, ontology, {})
        assert outcome.plan and not outcome.plan.fields
        assert any(e.event == 'agent.validation.failed' for e in run.events)
    asyncio.run(scenario())


def test_invalid_physical_field_gets_specific_catalog_feedback_then_is_corrected():
    async def scenario():
        workflow = Workflow(Store(), fake_execute, fake_retrieve, fake_snapshot)
        ref, ontology = await fake_snapshot('retail_credit')
        run = QueryRun(session_id='demo-session', question='分行贷款余额', ontology=ref)
        invalid = {**PLAN, 'fields': ['loans.OUTSTANDING_BALANCE']}
        model = NativeModel([
            ('AgentSubmission', {'payload': invalid}),
            ('read_object', {'name': 'loan'}),
            ('AgentSubmission', {'payload': PLAN}),
        ])
        workflow.analyst = AnalystAgent(model)
        outcome = await agentic.analyse(workflow, run, run.question, ontology, {})
        feedback = next(e.data['summary'] for e in run.events if e.event == 'agent.validation.failed')
        assert 'fields[0]=loans.OUTSTANDING_BALANCE' in feedback
        assert 'loan.outstanding_balance' in feedback
        assert 'field_refs' in str(model.messages[-1])
        assert outcome.plan.fields == [] and not run.sql
        assert invalid['fields'] == ['loans.OUTSTANDING_BALANCE']
    asyncio.run(scenario())


def test_final_failed_submission_is_logged_with_the_invalid_field():
    async def scenario():
        workflow = Workflow(Store(), fake_execute, fake_retrieve, fake_snapshot)
        ref, ontology = await fake_snapshot('retail_credit')
        run = QueryRun(session_id='demo-session', question='贷款余额', ontology=ref)
        model = NativeModel([('AgentSubmission', {'payload': {**PLAN, 'metrics': ['missing_metric'], 'filters': [{'field': 'loan.missing', 'operator': 'gt', 'value': 0}]}})] * 3)
        workflow.analyst = AnalystAgent(model)
        with pytest.raises(ModelError, match=r'filters\[0\].field=loan.missing'):
            await agentic.analyse(workflow, run, run.question, ontology, {})
        failures = [e for e in run.events if e.event == 'agent.validation.failed']
        assert len(failures) == 3 and failures[-1].data['attempt'] == 3
        assert 'loan.missing' in failures[-1].data['summary']
        assert 'missing_metric' in failures[-1].data['summary']
        assert run.sql is None and run.result is None
    asyncio.run(scenario())


def test_tool_budget_stops_before_thirteenth_tool_execution():
    model = NativeModel([('read_object', {'name': 'loan'})] * 20)
    async def scenario():
        workflow = Workflow(Store(), fake_execute, fake_retrieve, fake_snapshot)
        ref, ontology = await fake_snapshot('retail_credit')
        run = QueryRun(session_id='demo-session', question='贷款', ontology=ref)
        workflow.analyst = AnalystAgent(model)
        with pytest.raises(ModelError, match='12 次'):
            await agentic.analyse(workflow, run, run.question, ontology, {})
        assert sum(e.event == 'agent.tool.started' for e in run.events) == 12
    asyncio.run(scenario())


def test_wiki_tools_enforce_discovery_scope_and_follow_links(monkeypatch):
    class Wiki:
        provider = 'weknora'
        calls = []
        async def _wiki_search(self, kb_id, query, limit):
            self.calls.append('search')
            return [{'slug': 'loan', 'title': '贷款'}]
        async def _wiki_page(self, kb_id, slug):
            self.calls.append(slug)
            return {'slug': slug, 'title': slug, 'content': '业务定义', 'out_links': ['overdue'], 'source_refs': ['doc']}
        _page_evidence = staticmethod(agentic.knowledge_service._page_evidence)
    wiki = Wiki()
    monkeypatch.setattr(agentic, 'knowledge_service', wiki)
    async def scenario():
        workflow = Workflow(Store(), fake_execute, fake_retrieve, fake_snapshot)
        ref, ontology = await fake_snapshot('retail_credit')
        kb = ontology['knowledgeBaseIds'][0]
        run = QueryRun(session_id='demo-session', question='逾期', ontology=ref)
        tools = {t.name: t for t in agentic.AgentTools(workflow, run, ontology, {}).tools()}
        assert not json.loads(await tools['read_wiki'].ainvoke({'kb_id': kb, 'slug': 'loan'}))['available']
        assert not json.loads(await tools['search_wiki'].ainvoke({'kb_id': 'other-tenant', 'query': '贷款'}))['available']
        assert wiki.calls == []
        await tools['search_wiki'].ainvoke({'kb_id': kb, 'query': '贷款'})
        await tools['read_wiki'].ainvoke({'kb_id': kb, 'slug': 'loan'})
        await tools['read_wiki'].ainvoke({'kb_id': kb, 'slug': 'overdue'})
        assert len(run.evidence) == 2
        assert wiki.calls == ['search', 'loan', 'overdue']
        reviewer = {t.name for t in agentic.AgentTools(workflow, run, ontology, {}, 'reviewer').tools()}
        assert not reviewer & {'execute_sql', 'validate_plan', 'search_wiki', 'call_analyst'}
    asyncio.run(scenario())


def test_reviewer_revise_drives_reanalysis_and_requires_new_confirmation(manual_plan_confirmation):
    async def scenario():
        executions = []
        async def execute(*args):
            executions.append(args)
            return await fake_execute(*args)
        workflow = Workflow(Store(), execute, fake_retrieve, fake_snapshot)
        class Reviewer:
            count = 0
            async def review(self, *args):
                self.count += 1
                return ReviewDecision(decision='revise' if self.count == 1 else 'approved', issues=['核实关联角色'] if self.count == 1 else [], rationale='检查关联')
            async def review_result(self, *args):
                return ReviewDecision(decision='approved', issues=[], rationale='结果通过')
        workflow.reviewer = Reviewer()
        original = workflow._analyse_with_tools
        async def reanalyse(*args, **kwargs):
            outcome = await original(*args, **kwargs)
            if len(args) > 5 and args[5]:
                changed = QueryFilter(field='loan.status', operator='ne', value='closed')
                outcome.plan.filters.append(changed)
                outcome.requirement.filters = outcome.plan.filters
            return outcome
        workflow._analyse_with_tools = reanalyse
        auth = AuthorizationContext(subject='user', username='user', roles=['analyst'], branch_ids=[1])
        run = await workflow.start_background('demo-session', '各分行贷款余额', auth)
        for _ in range(100):
            if run.status in {RunStatus.CLARIFYING, RunStatus.FAILED}: break
            await asyncio.sleep(.01)
        assert run.status == RunStatus.CLARIFYING, run.error
        assert not executions
        run = await workflow.align(run, 'user', {})
        assert run.status == RunStatus.CLARIFYING, run.error
        assert run.revision_count == 1
        assert run.sql is None and run.requirement is None and not executions
        run = await workflow.align(run, 'user', {})
        assert run.status == RunStatus.COMPLETED, run.error
        assert run.revision_count == 1 and len(executions) == 1
        assert any(e.event == 'review.revision.required' for e in run.events)
        assert any(e.event == 'requirement.revised' for e in run.events)
        assert run.sql.requirement_hash == run.requirement.content_hash
        assert workflow.store.audit.verify(run.id) == (True, None)
    asyncio.run(scenario())


def test_unchanged_review_revision_stops_without_reconfirmation_or_execution():
    async def scenario():
        async def forbidden(*args):
            raise AssertionError('Rejected SQL must never execute')
        workflow = Workflow(Store(), forbidden, fake_retrieve, fake_snapshot)
        class Reviewer:
            async def review(self, *args):
                return ReviewDecision(decision='revise', issues=['仍存在重复累计风险'], rationale='必须修正')
        workflow.reviewer = Reviewer()
        run = await workflow.start('demo-session', '各分行贷款余额', approval_mode='disabled')
        assert run.status == RunStatus.FAILED
        assert run.revision_count == 1
        assert '未改变执行方案' in run.error and run.result is None
    asyncio.run(scenario())


def test_reviewer_investigates_with_tools_in_an_isolated_context():
    async def scenario():
        workflow = Workflow(Store(), fake_execute, fake_retrieve, fake_snapshot)
        run = await workflow.start('demo-session', '各分行贷款余额', approval_mode='disabled')
        assert run.status == RunStatus.COMPLETED
        run.agent_memory['analyst'] = [{'private': 'PRIVATE_ANALYST_NOT_IN_REVIEW'}]
        model = NativeModel([
            ('read_evidence', {}), ('read_object', {'name': 'loan'}),
            ('AgentSubmission', {'payload': {'decision': 'revise', 'issues': ['确认归属机构'], 'rationale': '存在不同关联角色'}}),
        ])
        workflow.reviewer = ReviewerAgent(model)
        _, ontology = await fake_snapshot('retail_credit')
        decision = await agentic.review(workflow, run, ontology)
        assert decision.decision == 'revise' and len(model.messages) == 3
        assert 'PRIVATE_ANALYST_NOT_IN_REVIEW' not in json.dumps(model.messages)
    asyncio.run(scenario())


def test_reviewer_reuses_observations_and_finishes_with_four_model_rounds():
    async def scenario():
        workflow = Workflow(Store(), fake_execute, fake_retrieve, fake_snapshot)
        run = await workflow.start('demo-session', '各分行贷款余额', approval_mode='disabled')
        model = NativeModel([
            ('read_object', {'name': 'loan'}), ('read_object', {'name': 'loan'}),
            ('read_evidence', {}), ('AgentSubmission', {'payload': {'decision': 'approved', 'issues': [], 'rationale': '字段与口径一致'}}),
        ])
        workflow.reviewer = ReviewerAgent(model)
        _, ontology = await fake_snapshot('retail_credit')
        decision = await agentic.review(workflow, run, ontology)
        assert decision.decision == 'approved' and len(model.messages) == 4
        assert any(e.data.get('observation', {}).get('reused') for e in run.events if e.event == 'agent.tool.completed')
        context = json.loads(next(m['content'] for m in model.messages[0] if m['role'] == 'user'))
        assert context['structural_checks']['fanout_checked']
        assert 'selected_catalog' in context and 'ontology_objects' not in context
    asyncio.run(scenario())


def test_reviewer_budget_blocks_a_fourth_tool_before_execution():
    async def scenario():
        workflow = Workflow(Store(), fake_execute, fake_retrieve, fake_snapshot)
        run = await workflow.start('demo-session', '各分行贷款余额', approval_mode='disabled')
        model = NativeModel([('read_object', {'name': 'loan'}), ('read_object', {'name': 'branch'}), ('read_object', {'name': 'customer'}), ('read_evidence', {})])
        workflow.reviewer = ReviewerAgent(model)
        _, ontology = await fake_snapshot('retail_credit')
        before = len(run.events)
        with pytest.raises(ModelError, match='3 次工具调用上限'):
            await agentic.review(workflow, run, ontology)
        assert not any(e.event == 'agent.tool.started' and e.data['tool'] == 'read_evidence' for e in run.events[before:])
    asyncio.run(scenario())


def test_alignment_is_accepted_before_model_finishes_and_duplicates_are_blocked(manual_plan_confirmation):
    async def scenario():
        release = asyncio.Event()
        workflow = Workflow(Store(), fake_execute, fake_retrieve, fake_snapshot)
        class Analyst:
            calls = 0
            async def analyse(self, question, evidence, ontology, scope):
                self.calls += 1
                if self.calls == 1:
                    return AnalystAgent()._build_outcome(CARD, question, evidence, ontology, scope, 'test')
                await release.wait()
                return AnalystAgent()._build_outcome(PLAN, question, evidence, ontology, scope, 'test')
        workflow.analyst = Analyst()
        auth = AuthorizationContext(subject='user', username='user', roles=['analyst'], branch_ids=[1])
        run = await workflow.start_background('demo-session', '贷款余额', auth)
        for _ in range(100):
            if run.status == RunStatus.CLARIFYING: break
            await asyncio.sleep(.01)
        response = workflow.start_alignment(run, 'user', {'time': '全部数据'})
        assert response.status == RunStatus.UNDERSTANDING
        assert response.agent_memory['pending_alignment'][0]['answers'] == {'time': '全部数据'}
        with pytest.raises(ValueError, match='不处于需求对齐'):
            workflow.start_alignment(run, 'user', {'time': '全部数据'})
        task = workflow._tasks[run.id]
        await asyncio.sleep(.01)
        assert not task.done() and run.sql is None
        assert run.status == RunStatus.UNDERSTANDING  # SSE must stay open while the model is running.
        release.set()
        await task
        assert run.status == RunStatus.CLARIFYING
        assert not run.requirement_draft.clarifications
        assert 'pending_alignment' not in run.agent_memory
        assert run.sql is None
    asyncio.run(scenario())


def test_model_connection_test_requires_real_tool_calling(monkeypatch):
    from app.main import app, model_settings
    class Candidate:
        model = 'test-provider'
        async def complete_json(self, *args):
            return {'ok': True}
        async def complete_messages(self, *args, **kwargs):
            return {'tool_calls': []}
    candidate = Candidate()
    monkeypatch.setattr(model_settings, 'candidate', lambda body: candidate)
    body = {'base_url': 'https://example.com/v1', 'model': 'test', 'api_key': 'test-secret'}
    response = TestClient(app).post('/api/v1/settings/model/test', json=body)
    assert response.status_code == 422 and 'tool calling' in response.json()['detail']
    async def supported(*args, **kwargs):
        return {'tool_calls': [{'function': {'name': 'semora_probe', 'arguments': '{"ok":true}'}}]}
    candidate.complete_messages = supported
    response = TestClient(app).post('/api/v1/settings/model/test', json=body)
    assert response.status_code == 200 and response.json()['tool_calling']
    assert 'test-secret' not in response.text


def test_typed_agent_output_corrects_text_confidence_before_delivery():
    async def scenario():
        workflow = Workflow(Store(), fake_execute, fake_retrieve, fake_snapshot)
        ref, ontology = await fake_snapshot('retail_credit')
        run = QueryRun(session_id='demo-session', question='贷款统计', ontology=ref)
        model = NativeModel([
            ('AgentSubmission', {'payload': {**CARD, 'confidence': {'metrics': 'unknown; user clarification pending'}}}),
            ('AgentSubmission', {'payload': {**CARD, 'confidence': {'metrics': 0.2}}}),
        ])
        workflow.analyst = AnalystAgent(model)
        outcome = await agentic.analyse(workflow, run, run.question, ontology, {})
        assert outcome.plan is None
        assert outcome.requirement.confidence == {'metrics': 0.2}
        assert len(model.messages) == 2
        assert any(m['role'] == 'tool' and 'confidence' in str(m['content']) for m in model.messages[1])
    asyncio.run(scenario())


def test_native_agents_run_inside_checkpointed_workflow_and_async_alignment(manual_plan_confirmation):
    async def scenario():
        executions = []
        async def execute(*args):
            executions.append(args)
            return await fake_execute(*args)
        workflow = Workflow(Store(), execute, fake_retrieve, fake_snapshot)
        model = NativeModel([
            ('read_object', {'name': 'loan'}), ('AgentSubmission', {'payload': CARD}),
            ('read_object', {'name': 'branch'}), ('AgentSubmission', {'payload': PLAN}),
        ])
        workflow.analyst = AnalystAgent(model)
        workflow.reviewer = ReviewerAgent(StructuredModel())
        auth = AuthorizationContext(subject='user', username='user', roles=['analyst'], branch_ids=[1])
        run = await workflow.start_background('demo-session', '贷款余额', auth)
        for _ in range(100):
            if run.status in {RunStatus.CLARIFYING, RunStatus.FAILED}: break
            await asyncio.sleep(.01)
        assert run.status == RunStatus.CLARIFYING, run.error
        workflow.start_alignment(run, 'user', {'time': '全部数据'})
        await workflow._tasks[run.id]
        assert run.status == RunStatus.CLARIFYING, run.error
        assert run.requirement_draft.clarifications == [] and run.sql is None
        assert not executions and len(model.messages) == 4
        workflow.start_alignment(run, 'user', {})
        await workflow._tasks[run.id]
        assert run.status == RunStatus.COMPLETED, run.error
        assert len(executions) == 1
        assert workflow.store.audit.verify(run.id) == (True, None)
    asyncio.run(scenario())


def test_task_rounds_continue_after_restore_and_reviewer_has_a_separate_role_count():
    async def scenario():
        workflow = Workflow(Store(), fake_execute, fake_retrieve, fake_snapshot)
        ref, ontology = await fake_snapshot('retail_credit')
        run = QueryRun(session_id='demo-session', question='贷款余额', ontology=ref)
        workflow.analyst = AnalystAgent(NativeModel([
            ('read_object', {'name': 'loan'}), ('AgentSubmission', {'payload': CARD}),
        ]))
        await agentic.analyse(workflow, run, run.question, ontology, {})
        # Simulate Store.save_run / hydration without changing the audit events.
        run = QueryRun.model_validate({**run.model_dump(mode='json'), 'agent_memory': run.agent_memory})
        workflow.analyst = AnalystAgent(NativeModel([
            ('read_object', {'name': 'branch'}), ('AgentSubmission', {'payload': PLAN}),
        ]))
        outcome = await agentic.analyse(workflow, run, '用户补充：不限时间', ontology, {})
        run.plan = outcome.plan
        workflow.reviewer = ReviewerAgent(NativeModel([
            ('AgentSubmission', {'payload': {'decision': 'approved', 'issues': [], 'rationale': '核验通过'}}),
        ]))
        await agentic.review(workflow, run, ontology)
        rounds = [e.data for e in run.events if e.event == 'agent.model.started']
        assert [e['iteration'] for e in rounds] == [1, 2, 3, 4, 5]
        assert [e['role_iteration'] for e in rounds] == [1, 2, 3, 4, 1]
        assert [e['local_iteration'] for e in rounds] == [1, 2, 1, 2, 1]
        phases = [e.data['phase'] for e in run.events if e.event == 'agent.phase.started']
        assert phases == ['首次需求分析', '补充需求分析', '执行前复核']
    asyncio.run(scenario())


def test_public_tool_trace_contains_goal_and_facts_but_not_raw_private_fields():
    async def scenario():
        workflow = Workflow(Store(), fake_execute, fake_retrieve, fake_snapshot)
        ref, ontology = await fake_snapshot('retail_credit')
        ontology['objects'][0]['metadata'] = {'password': 'PRIVATE_VALUE'}
        run = QueryRun(session_id='demo-session', question='贷款余额', ontology=ref)
        toolkit = agentic.AgentTools(workflow, run, ontology, {})
        toolkit.start_phase('首次需求分析')
        toolkit.event('agent.model.started', {'iteration': 1})
        tool = next(t for t in toolkit.tools() if t.name == 'read_object')
        await tool.ainvoke({'name': ontology['objects'][0]['name'], 'analysis_goal': '核实分行对象的字段与关联角色'})
        started = next(e.data for e in run.events if e.event == 'agent.tool.started')
        completed = next(e.data for e in run.events if e.event == 'agent.tool.completed')
        assert started['analysis_goal'] == '核实分行对象的字段与关联角色'
        assert started['lookup']['name'] == ontology['objects'][0]['name']
        assert completed['observation']['field_count'] == len(ontology['objects'][0]['properties'])
        assert completed['iteration'] == 1
        assert 'PRIVATE_VALUE' not in json.dumps([e.model_dump() for e in run.events], default=str)
    asyncio.run(scenario())


def test_revision_rejects_text_only_changes_and_requires_a_changed_execution_plan():
    async def scenario():
        workflow = Workflow(Store(), fake_execute, fake_retrieve, fake_snapshot)
        ref, ontology = await fake_snapshot('retail_credit')
        run = QueryRun(session_id='demo-session', question='有效贷款余额', ontology=ref)
        run.plan = AnalystAgent()._build_outcome(PLAN, run.question, [], ontology, {}, 'test').plan
        corrected = {**PLAN, 'filters': [{'field': 'loan.status', 'operator': 'eq', 'value': 'active'}]}
        model = NativeModel([
            ('AgentSubmission', {'payload': {**PLAN, 'assumptions': ['已修正，只统计有效贷款']}}),
            ('AgentSubmission', {'payload': corrected}),
        ])
        workflow.analyst = AnalystAgent(model)
        outcome = await agentic.analyse(workflow, run, run.question, ontology, {}, {'decision': 'revise', 'issues': ['缺少有效状态过滤']})
        assert outcome.plan.filters[0].value == 'active'
        assert run.plan.filters == []
        assert sum(e.event == 'agent.validation.failed' for e in run.events) == 1
        context = json.loads(next(m['content'] for m in model.messages[0] if m['role'] == 'user'))
        assert context['rejected_plan']['filters'] == []
    asyncio.run(scenario())


def test_revision_reason_is_visible_even_if_model_omits_issues():
    decision = ReviewerAgent._model_decision({'decision': 'revise', 'issues': [], 'rationale': '缺少必要过滤条件'}, 'pre_execution')
    assert decision.issues == ['缺少必要过滤条件']


def test_explicit_business_predicate_must_be_an_executable_filter():
    async def scenario():
        _, ontology = await fake_snapshot('retail_credit')
        analyst = AnalystAgent()
        proposal = {**PLAN, 'assumptions': ['仅统计 outstanding_balance > 0']}
        with pytest.raises(ValueError, match='未落实到 filters'):
            analyst._build_outcome(proposal, '贷款余额', [], ontology, {}, 'test')
        proposal['filters'] = [{'field': 'loan.outstanding_balance', 'operator': 'gt', 'value': 0}]
        outcome = analyst._build_outcome(proposal, '贷款余额', [], ontology, {}, 'test')
        assert outcome.plan.filters[0].field == 'loan.outstanding_balance'
    asyncio.run(scenario())


def test_preflight_repairs_missing_goal_before_user_confirmation():
    async def scenario():
        workflow = Workflow(Store(), fake_execute, fake_retrieve, fake_snapshot)
        ref, ontology = await fake_snapshot('retail_credit')
        run = QueryRun(session_id='demo-session', question='有效贷款余额', ontology=ref)
        corrected = {**PLAN, 'filters': [{'field': 'loan.status', 'operator': 'eq', 'value': 'active'}]}
        analyst = NativeModel([('AgentSubmission', {'payload': PLAN}), ('AgentSubmission', {'payload': corrected})])
        reviewer = NativeModel([
            ('AgentSubmission', {'payload': {'decision': 'revise', 'issues': ['filters缺少有效状态'], 'rationale': '应落实用户的有效贷款限定'}}),
            ('AgentSubmission', {'payload': {'decision': 'approved', 'issues': [], 'rationale': '目标和条件完整'}}),
        ])
        workflow.analyst = AnalystAgent(analyst)
        workflow.reviewer = ReviewerAgent(reviewer)
        outcome = await agentic.analyse(workflow, run, run.question, ontology, {})
        assert outcome.plan.filters[0].value == 'active'
        assert len(analyst.messages) == 2 and len(reviewer.messages) == 2
        assert run.sql is None and run.result is None
        assert 'preflight_feedback' in str(analyst.messages[-1])
        assert not any(e.event == 'alignment.required' for e in run.events)
    asyncio.run(scenario())


def test_exhausted_preflight_keeps_internal_feedback_out_of_failure_banner():
    async def scenario():
        workflow = Workflow(Store(), fake_execute, fake_retrieve, fake_snapshot)
        ref, ontology = await fake_snapshot('retail_credit')
        run = QueryRun(session_id='demo-session', question='查询', ontology=ref)
        detail = 'INTERNAL_REVIEW: requirement.filters缺少业务条件，完整SQL修订建议' * 20
        workflow.analyst = AnalystAgent(NativeModel([('AgentSubmission', {'payload': PLAN})] * 3))
        workflow.reviewer = ReviewerAgent(NativeModel([('AgentSubmission', {'payload': {
            'decision': 'revise', 'issues': ['缺少条件'], 'rationale': detail}})] * 3))
        with pytest.raises(ValueError) as error:
            await agentic.analyse(workflow, run, run.question, ontology, {})
        assert '自动修订未完成' in str(error.value)
        assert 'INTERNAL_REVIEW' not in str(error.value) and len(str(error.value)) < 150
        reviews = [e for e in run.events if e.event == 'plan.preflight.completed']
        assert len(reviews) == 3 and reviews[-1].data['review']['rationale'] == detail
        assert run.sql is None and run.result is None
    asyncio.run(scenario())


def test_workflow_shows_public_validation_failure_and_keeps_diagnostics_in_events(monkeypatch):
    async def scenario():
        from semora_agent.model import PlanValidationError
        workflow = Workflow(Store(), fake_execute, fake_retrieve, fake_snapshot)
        async def fail(run, *args, **kwargs):
            workflow.emit(run, 'agent.validation.failed', RunStatus.UNDERSTANDING, {'summary': 'INTERNAL_PLAN_DIAGNOSTIC'})
            raise PlanValidationError('INTERNAL_PLAN_DIAGNOSTIC')
        monkeypatch.setattr(workflow, '_analyse_with_tools', fail)
        run = await workflow.start('demo-session', '查询', approval_mode='disabled')
        assert run.status == RunStatus.FAILED and '可执行方案' in run.error
        assert 'INTERNAL_PLAN_DIAGNOSTIC' not in run.error
        assert any(e.data.get('summary') == 'INTERNAL_PLAN_DIAGNOSTIC' for e in run.events)
        assert run.sql is None and run.result is None
    asyncio.run(scenario())


def test_metric_scoped_condition_is_not_forced_into_global_filters():
    async def scenario():
        _, ontology = await fake_snapshot('retail_credit')
        proposal = {**PLAN, 'metrics': ['overdue_90_rate'], 'assumptions': ['payment.days_overdue >= 90']}
        outcome = AnalystAgent()._build_outcome(proposal, '逾期率', [], ontology, {}, 'test')
        assert outcome.plan.filters == []
    asyncio.run(scenario())


def test_confirmed_predicate_is_repaired_and_reviewed_without_another_confirmation():
    async def scenario():
        workflow = Workflow(Store(), fake_execute, fake_retrieve, fake_snapshot)
        original = workflow._analyse_with_tools
        async def legacy_proposal(*args, **kwargs):
            outcome = await original(*args, **kwargs)
            outcome.plan.assumptions = ["status = 'active'"]
            outcome.requirement.assumptions = outcome.plan.assumptions
            if len(args) <= 5 or not args[5]:
                outcome.plan.filters = []
                outcome.requirement.filters = []
            return outcome
        workflow._analyse_with_tools = legacy_proposal
        class Reviewer:
            count = 0
            async def review(self, plan, *args):
                self.count += 1
                return ReviewDecision(decision='approved' if plan.filters else 'revise', issues=[] if plan.filters else ['落实已确认的status条件'], rationale='核验过滤条件')
            async def review_result(self, *args):
                return ReviewDecision(decision='approved', issues=[], rationale='通过')
        workflow.reviewer = Reviewer()
        run = await workflow.start('demo-session', '各分行贷款余额', approval_mode='disabled')
        assert run.status == RunStatus.COMPLETED, run.error
        assert run.revision_count == 1 and workflow.reviewer.count == 2
        assert any(e.event == 'plan.repaired' for e in run.events)
        assert len([e for e in run.events if e.event == 'requirement.aligned']) == 1
        assert len([e for e in run.events if e.event == 'execution.completed']) == 1
        assert len([e for e in run.events if e.event == 'sql.ready']) == 2
        assert not any(e.event == 'requirement.revised' for e in run.events)
        assert run.sql.requirement_hash == run.requirement.content_hash
        assert workflow.store.audit.verify(run.id) == (True, None)
    asyncio.run(scenario())


def test_internal_repairs_cannot_loop_past_three_or_execute_rejected_sql():
    async def scenario():
        async def forbidden(*args):
            raise AssertionError('Rejected SQL must not execute')
        workflow = Workflow(Store(), forbidden, fake_retrieve, fake_snapshot)
        original = workflow._analyse_with_tools
        predicates = [QueryFilter(field='loan.outstanding_balance', operator='gt', value=n) for n in range(4)]
        async def legacy_proposal(*args, **kwargs):
            outcome = await original(*args, **kwargs)
            count = args[0].revision_count + 1 if len(args) > 5 and args[5] else 0
            outcome.plan.filters = predicates[:count]
            outcome.requirement.filters = []
            outcome.plan.assumptions = [f'outstanding_balance > {n}' for n in range(4)]
            outcome.requirement.assumptions = outcome.plan.assumptions
            return outcome
        workflow._analyse_with_tools = legacy_proposal
        class Reviewer:
            async def review(self, *args):
                return ReviewDecision(decision='revise', issues=['仍有未落实条件'], rationale='需要继续修订')
        workflow.reviewer = Reviewer()
        run = await workflow.start('demo-session', '各分行贷款余额', approval_mode='disabled')
        assert run.status == RunStatus.FAILED and run.revision_count == 3
        assert '3 轮' in run.error and run.result is None
        assert len([e for e in run.events if e.event == 'plan.repaired']) == 3
        assert not any(e.event.startswith('execution.') for e in run.events)
    asyncio.run(scenario())
