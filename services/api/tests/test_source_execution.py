import asyncio
import copy
import re
import sqlite3

import pytest
import sqlglot

from app import datasources
from app.datasources import DataSourceRecord, DataSourceRepository
from app.store import Store
from app.workflow import Workflow
from semora_agent import AnalystAgent, ReviewerAgent
from semora_agent.model import StructuredModel
from semora_contracts import AuthorizationContext, ExecutionResult, OntologySnapshotRef, ReviewDecision, RunStatus


def ontology():
    return {'domain': 'commerce', 'dataSourceId': 'customers-db', 'knowledgeBaseIds': [],
        'objects': [
            {'name': 'client', 'label': '客户', 'table': 'people', 'dataSourceId': 'customers-db', 'primaryKey': 'row_id',
             'properties': [{'name': n, 'column': n, 'type': 'string' if n != 'row_id' else 'integer'} for n in ['row_id', 'customer_id', 'segment', 'status']]},
            {'name': 'balances', 'label': '账户', 'table': 'accounts', 'dataSourceId': 'accounts-db', 'primaryKey': 'id',
             'properties': [{'name': 'customer_id', 'column': 'customer_id', 'type': 'string'}, {'name': 'amount', 'column': 'amount', 'type': 'decimal'}]},
        ], 'relations': [{'name': 'customer_accounts', 'from': 'client', 'to': 'balances', 'condition': 'client.customer_id = balances.customer_id', 'cardinality': '1:N'}], 'metrics': []}


def proposal(enrich=True):
    return {'intent': '查有效客户的账户余额', 'source_steps': [
        {'name': 'pick_clients', 'data_source_id': 'customers-db', 'query': {'domain': 'commerce', 'data_source_id': 'customers-db',
            'intent': '查有效客户', 'grain': 'client', 'fields': ['client.customer_id', 'client.segment'], 'metrics': [],
            'filters': [{'field': 'client.status', 'operator': 'eq', 'value': 'active'}]}},
        {'name': 'lookup_accounts', 'data_source_id': 'accounts-db', 'query': {'domain': 'commerce', 'data_source_id': 'accounts-db',
            'intent': '查账户余额', 'grain': 'balances', 'fields': ['balances.customer_id', 'balances.amount'], 'metrics': []},
         'inputs': [{'relation': 'customer_accounts', 'source_step': 'pick_clients', 'source_field': 'client.customer_id',
                     'target_field': 'balances.customer_id', 'include_source_fields': ['client.segment'] if enrich else []}]},
    ]}


def harness(monkeypatch, *, rows=None, truncated=False, reject=False, fail_b=False, masked=False):
    repository = DataSourceRepository()
    for key in ('customers-db', 'accounts-db'):
        repository.save(DataSourceRecord(key, key, 'postgresql', 'DATASOURCE_TEST_URL', status='healthy'))
    monkeypatch.setattr(datasources.datasource_service, 'repository', repository)
    databases = {'customers-db': sqlite3.connect(':memory:'), 'accounts-db': sqlite3.connect(':memory:')}
    databases['customers-db'].executescript("CREATE TABLE people(row_id INTEGER,customer_id TEXT,segment TEXT,status TEXT); INSERT INTO people VALUES(1,'A','retail','active'),(2,'A','retail','active'),(3,'B','corporate','inactive');")
    databases['accounts-db'].executescript("CREATE TABLE accounts(id INTEGER,customer_id TEXT,amount NUMERIC); INSERT INTO accounts VALUES(1,'A',10),(2,'A',20),(3,'B',999);")
    calls = []
    model = StructuredModel(); model.base_url = model.api_key = ''; model.mode = 'optional'
    class Analyst:
        async def analyse(self, question, evidence, model_ontology, scope):
            return AnalystAgent(model)._build_outcome(copy.deepcopy(proposal()), question, evidence, model_ontology, scope, 'test')
    async def snapshot(domain):
        return OntologySnapshotRef(domain='commerce', version='1', content_hash='sha256:test', artifact_version='published'), ontology()
    async def unused(*args):
        raise AssertionError('unrouted execution')
    w = Workflow(Store(), unused, snapshot_loader=snapshot)
    w.store.create_session('跨源', 'commerce')
    session = next(s for s in w.store.sessions.values() if s.domain == 'commerce')
    w.analyst, w.reviewer = Analyst(), ReviewerAgent(model)
    if reject:
        class Reject:
            async def review(self, *args):
                return ReviewDecision(decision='rejected', issues=['业务范围不符'], rationale='拒绝')
        w.reviewer = Reject()
    async def execute(sql, parameters, sensitive, settings):
        key = settings['data_source_id']; calls.append((key, sql, parameters))
        if key == 'accounts-db' and fail_b:
            raise RuntimeError('B unavailable')
        sql = re.sub(r'%\(([A-Za-z_][A-Za-z0-9_]*)\)s', r':\1', sql)
        sql = sqlglot.transpile(sql, read='postgres', write='sqlite')[0]
        cursor = databases[key].execute(sql, parameters)
        result_rows = [list(row) for row in cursor.fetchall()]
        if key == 'customers-db' and rows is not None:
            result_rows = rows
        cut = len(result_rows) > settings['max_result_rows'] or (truncated and key == 'customers-db')
        result_rows = result_rows[:settings['max_result_rows']]
        return ExecutionResult(columns=[d[0] for d in cursor.description], rows=result_rows, row_count=len(result_rows), duration_ms=1,
            truncated=cut, summary={'masked_columns': ['client_customer_id'] if masked else []})
    w.execute = execute
    return w, session, calls


def test_two_independent_databases_are_queried_in_order_with_deduplicated_bound_keys(monkeypatch):
    async def scenario():
        w, session, calls = harness(monkeypatch)
        r = await w.start(session.id, '查有效客户账户', domain='commerce')
        assert r.status == RunStatus.COMPLETED, r.error
        assert [c[0] for c in calls] == ['customers-db', 'accounts-db']
        assert calls[0][1].endswith('LIMIT 1001')
        assert list(calls[1][2].values()) == ['A']
        assert r.result.columns == ['balances_customer_id', 'balances_amount', 'pick_clients__client_segment']
        assert r.result.rows == [['A', 10, 'retail'], ['A', 20, 'retail']]
        assert len(r.source_artifacts) == 2 and 'source_results' not in r.model_dump(mode='json')
        assert w.store.audit.verify(r.id) == (True, None)
    asyncio.run(scenario())


@pytest.mark.parametrize('kind', ['empty', 'truncated', 'rejected', 'b_failure', 'masked'])
def test_cross_source_failure_and_empty_conditions_never_publish_partial_answers(monkeypatch, kind):
    async def scenario():
        w, session, calls = harness(monkeypatch, rows=[] if kind == 'empty' else None,
            truncated=kind == 'truncated', reject=kind == 'rejected', fail_b=kind == 'b_failure', masked=kind == 'masked')
        r = await w.start(session.id, '查询', domain='commerce')
        if kind == 'empty':
            assert r.status == RunStatus.COMPLETED and r.result.rows == []
            assert [c[0] for c in calls] == ['customers-db']
        else:
            assert r.status == RunStatus.FAILED, r.error
            assert r.result is None and r.answer is None
            if kind in {'truncated', 'masked'}:
                assert [c[0] for c in calls] == ['customers-db']
            if kind == 'rejected':
                assert calls == []
            if kind == 'b_failure':
                assert [c[0] for c in calls] == ['customers-db', 'accounts-db']
                assert 'B unavailable' in r.error
    asyncio.run(scenario())


def test_empty_timeout_message_reports_step_and_operation(monkeypatch):
    import httpx
    async def scenario():
        w,session,calls=harness(monkeypatch)
        async def timeout(*args):raise httpx.ReadTimeout('')
        w.estimate=timeout
        run=await w.start(session.id,'查询',domain='commerce')
        assert run.status==RunStatus.FAILED and run.result is None
        assert 'pick_clients' in run.error and '成本估算' in run.error and 'ReadTimeout' in run.error
        assert run.events[-1].data['operation']=='成本估算'
        assert not calls
    asyncio.run(scenario())


@pytest.mark.parametrize('kind', ['oversized', 'conflicting_attributes', 'injection', 'unauthorized'])
def test_intermediate_keys_are_complete_unique_safe_and_authorized(monkeypatch, kind):
    async def scenario():
        attack = "A'); DROP TABLE accounts; --"
        rows = ([[str(i), 'retail'] for i in range(1001)] if kind == 'oversized' else
                [['A', 'retail'], ['A', 'corporate']] if kind == 'conflicting_attributes' else
                [[attack, 'retail']] if kind == 'injection' else None)
        w, session, calls = harness(monkeypatch, rows=rows)
        auth = AuthorizationContext(subject='test', username='test', roles=['analyst'], branch_ids=[1],
            allowed_data_source_ids=['customers-db'] if kind == 'unauthorized' else None)
        r = await w.start(session.id, '查询', domain='commerce', authorization=auth)
        if kind == 'injection':
            assert r.status == RunStatus.COMPLETED and r.result.rows == []
            assert attack in calls[1][2].values() and attack not in calls[1][1]
        else:
            assert r.status == RunStatus.FAILED and r.result is None
            assert [c[0] for c in calls] == ([] if kind == 'unauthorized' else ['customers-db'])
    asyncio.run(scenario())


def test_optional_manual_approval_binds_each_actual_step_without_repeating_a(monkeypatch):
    async def scenario():
        from app.settings import settings_service
        settings = settings_service.get(); settings.approval_mode = 'all'
        monkeypatch.setattr(settings_service, 'get', lambda: settings.model_copy(deep=True))
        w, session, calls = harness(monkeypatch)
        r = await w.start(session.id, '查询', domain='commerce')
        assert r.status == RunStatus.AWAITING_APPROVAL and calls == []
        first_hash = r.approval.sql_hash
        await w.approve(r, 'approver', True, '允许A')
        assert r.status == RunStatus.AWAITING_APPROVAL
        assert [c[0] for c in calls] == ['customers-db']
        assert r.approval.sql_hash != first_hash
        assert 'A' in r.sql.parameters.values()
        await w.approve(r, 'approver', True, '允许B')
        assert r.status == RunStatus.COMPLETED, r.error
        assert [c[0] for c in calls] == ['customers-db', 'accounts-db']
    asyncio.run(scenario())


def test_public_background_flow_executes_after_review_without_a_confirmation_click(monkeypatch):
    async def scenario():
        w, session, calls = harness(monkeypatch)
        auth = AuthorizationContext(subject='business', username='业务用户', roles=['business_user'], branch_ids=[1])
        r = await w.start_background(session.id, '查有效客户账户', auth, domain='commerce')
        for _ in range(200):
            if r.status in {RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CLARIFYING}:
                break
            await asyncio.sleep(.01)
        assert r.status == RunStatus.COMPLETED, r.error
        assert r.review.decision == 'approved' and len(calls) == 2
        assert not any(e.event in {'alignment.required', 'approval.required'} for e in r.events)
    asyncio.run(scenario())


def test_intermediate_keys_and_rows_are_not_sent_to_reviewer(monkeypatch):
    async def scenario():
        from services.api.tests.test_agent_runtime import NativeModel
        w, session, _ = harness(monkeypatch, rows=[['PRIVATE_INTERMEDIATE_KEY', 'PRIVATE_INTERMEDIATE_SEGMENT']])
        model = StructuredModel(); model.base_url = model.api_key = ''; model.mode = 'optional'
        class AggregateAnalyst:
            async def analyse(self, question, evidence, model_ontology, scope):
                value = proposal(enrich=False)
                value['source_steps'][1]['query'].update({'fields': [], 'metrics': ['account_count'],
                    'calculations': [{'name': 'account_count', 'function': 'count', 'field': 'balances.customer_id'}]})
                return AnalystAgent(model)._build_outcome(value, question, evidence, model_ontology, scope, 'test')
        w.analyst = AggregateAnalyst()
        reviewer = NativeModel([('AgentSubmission', {'payload': {'decision': 'approved', 'issues': [], 'rationale': '口径完整'}})] * 2)
        w.reviewer = ReviewerAgent(reviewer)
        r = await w.start(session.id, '查询账户数', domain='commerce')
        assert r.status == RunStatus.COMPLETED, r.error
        import json
        sent = json.dumps(reviewer.messages)
        assert 'PRIVATE_INTERMEDIATE_KEY' not in sent and 'PRIVATE_INTERMEDIATE_SEGMENT' not in sent
        assert r.result.rows == [[0]]
    asyncio.run(scenario())


def test_tampered_intermediate_result_cannot_resume_second_source(monkeypatch):
    async def scenario():
        from app.settings import settings_service
        settings = settings_service.get(); settings.approval_mode = 'all'
        monkeypatch.setattr(settings_service, 'get', lambda: settings.model_copy(deep=True))
        w, session, calls = harness(monkeypatch)
        r = await w.start(session.id, '查询', domain='commerce')
        await w.approve(r, 'approver', True, '允许A')
        assert r.status == RunStatus.AWAITING_APPROVAL
        r.source_results['pick_clients'].rows[0][0] = 'TAMPERED'
        await w.approve(r, 'approver', True, '允许B')
        assert r.status == RunStatus.FAILED and '审计摘要不一致' in r.error
        assert [c[0] for c in calls] == ['customers-db']
    asyncio.run(scenario())


@pytest.mark.parametrize('kind', ['forward_reference', 'wrong_type', 'sensitive_key', 'missing_relation', 'lost_target_key', 'missing_scope'])
def test_invalid_cross_source_contracts_fail_before_any_query(kind):
    value, model_ontology = proposal(), ontology()
    scope = {}
    binding = value['source_steps'][1]['inputs'][0]
    if kind == 'forward_reference': binding['source_step'] = 'lookup_accounts'
    if kind == 'wrong_type': model_ontology['objects'][1]['properties'][0]['type'] = 'integer'
    if kind == 'sensitive_key': model_ontology['objects'][0]['properties'][1]['sensitive'] = True
    if kind == 'missing_relation': binding['relation'] = 'unpublished'
    if kind == 'lost_target_key': value['source_steps'][1]['query']['fields'] = ['balances.amount']
    if kind == 'missing_scope': scope = {'branch_ids': [1]}
    model = StructuredModel(); model.base_url = model.api_key = ''; model.mode = 'optional'
    with pytest.raises(ValueError):
        AnalystAgent(model)._build_outcome(value, '查询', [], model_ontology, scope, 'test')


def test_reviewer_can_revise_only_source_steps_then_execute_without_extra_confirmation(monkeypatch):
    async def scenario():
        w, session, calls = harness(monkeypatch)
        proposals = iter([proposal(), proposal(enrich=False)])
        class Analyst:
            async def analyse(self, question, evidence, model_ontology, scope):
                return AnalystAgent(StructuredModel())._build_outcome(next(proposals), question, evidence, model_ontology, scope, 'test')
        decisions = iter([
            ReviewDecision(decision='revise', issues=['去掉不需要的来源附加列'], rationale='只需账户结果'),
            ReviewDecision(decision='approved', issues=[], rationale='步骤已修订'),
        ])
        class Reviewer(ReviewerAgent):
            async def review(self, *args):
                return next(decisions)
        w.analyst, w.reviewer = Analyst(), Reviewer(StructuredModel())
        r = await w.start(session.id, '查有效客户的账户余额', domain='commerce')
        assert r.status == RunStatus.COMPLETED, r.error
        assert r.revision_count == 1
        assert [call[0] for call in calls] == ['customers-db', 'accounts-db']
        assert r.result.columns == ['balances_customer_id', 'balances_amount']
        assert not any(e.event in {'alignment.required', 'approval.required'} for e in r.events)
    asyncio.run(scenario())


def test_native_tool_submission_runs_cross_source_plan_without_confirmation(monkeypatch):
    async def scenario():
        from services.api.tests.test_agent_runtime import NativeModel

        w, session, calls = harness(monkeypatch)
        w.analyst = AnalystAgent(NativeModel([
            ('read_object', {'name': 'client'}),
            ('AgentSubmission', {'payload': proposal()}),
        ]))
        r = await w.start(session.id, '查有效客户的账户余额，并带上客户类型', domain='commerce')
        assert r.status == RunStatus.COMPLETED, r.error
        assert len(r.plan.source_steps) == 2
        assert [call[0] for call in calls] == ['customers-db', 'accounts-db']
        assert r.review.decision == 'approved'
        assert not any(e.event in {'alignment.required', 'approval.required'} for e in r.events)
    asyncio.run(scenario())


def test_cross_source_join_cannot_fall_back_to_single_connection_sql():
    from app.planner import compile_plan, PlanningError
    from semora_contracts import QueryPlan
    plan = QueryPlan(domain='commerce', data_source_id='customers-db', intent='禁止跨实例JOIN', metrics=[],
                     fields=['client.customer_id', 'balances.amount'], grain='client', relationship_path=['customer_accounts'])
    with pytest.raises(PlanningError, match='不同数据源对象不能直接JOIN'):
        compile_plan(plan, ontology())


@pytest.mark.parametrize('function,expected', [('count', [[0]]), ('sum', [[None]])])
def test_empty_source_keys_preserve_global_aggregate_semantics(monkeypatch, function, expected):
    async def scenario():
        w, session, calls = harness(monkeypatch, rows=[])
        class Analyst:
            async def analyse(self, question, evidence, model_ontology, scope):
                value = proposal(enrich=False)
                value['source_steps'][1]['query'].update({
                    'fields': [], 'metrics': ['total'],
                    'calculations': [{'name': 'total', 'function': function, 'field': 'balances.amount'}],
                })
                return AnalystAgent(StructuredModel())._build_outcome(value, question, evidence, model_ontology, scope, 'test')
        w.analyst = Analyst()
        r = await w.start(session.id, '查询有效客户的账户汇总', domain='commerce')
        assert r.status == RunStatus.COMPLETED, r.error
        assert r.result.rows == expected
        assert [call[0] for call in calls] == ['customers-db', 'accounts-db']
        assert list(calls[1][2].values()) == [None]
    asyncio.run(scenario())


def test_unmatched_lookup_key_cannot_silently_lose_enriched_attributes(monkeypatch):
    async def scenario():
        w, session, calls = harness(monkeypatch)
        execute = w.execute
        async def collation_lookup(sql, parameters, sensitive, settings):
            result = await execute(sql, parameters, sensitive, settings)
            if settings['data_source_id'] == 'accounts-db':
                # A database collation may match 'a' to a bound 'A'; Python
                # cannot assume those keys identify the same source record.
                for row in result.rows:
                    row[0] = 'a'
            return result
        w.execute = collation_lookup
        r = await w.start(session.id, '查有效客户账户并带上来源属性', domain='commerce')
        assert r.status == RunStatus.FAILED
        assert r.result is None
        assert '关联键' in r.error
    asyncio.run(scenario())


def test_source_output_alias_collisions_are_rejected_before_sql():
    value = proposal()
    query = value['source_steps'][0]['query']
    query['metrics'] = ['client_customer_id']
    query['calculations'] = [{'name': 'client_customer_id', 'function': 'count', 'field': 'client.row_id'}]
    value['source_steps'][1]['inputs'][0]['include_source_fields'] = ['client_customer_id']
    with pytest.raises(ValueError, match='别名冲突'):
        AnalystAgent(StructuredModel())._build_outcome(value, '查询客户及记录数', [], ontology(), {}, 'test')


@pytest.mark.parametrize('failure', ['connection', 'timeout', 'invalid_response'])
def test_executor_cancel_failure_still_stops_cross_source_workflow(monkeypatch, failure):
    async def scenario():
        import httpx
        w, session, calls = harness(monkeypatch)
        started = asyncio.Event()
        async def waiting_execute(sql, parameters, sensitive, settings):
            calls.append(settings['data_source_id'])
            started.set()
            await asyncio.Event().wait()
        w.execute = waiting_execute
        auth = AuthorizationContext(subject='test', username='test', roles=['analyst'], branch_ids=[1])
        run = await w.start_background(session.id, '跨源查询', auth, domain='commerce')
        await asyncio.wait_for(started.wait(), 3)
        task = w._tasks[run.id]
        async def offline(*args, **kwargs):
            assert task.cancelling() > 0
            if failure == 'timeout':
                raise httpx.ReadTimeout('executor timeout')
            if failure == 'invalid_response':
                return httpx.Response(200, content=b'invalid json', request=httpx.Request('POST', 'http://executor/cancel'))
            raise httpx.ConnectError('executor unavailable')
        monkeypatch.setattr(httpx.AsyncClient, 'post', offline)
        result = await w.cancel(run.id)
        assert result['task_cancelled'] and not result['executor_cancelled']
        assert result['executor_cancel_error']
        assert run.status == RunStatus.FAILED
        assert calls == ['customers-db']
    asyncio.run(scenario())


def test_cross_source_explicit_predicate_checked_in_step_not_root():
    raw = proposal()
    raw['assumptions'] = ["client.status = 'active'"]
    agent = AnalystAgent()
    result = agent._build_outcome(copy.deepcopy(raw), '查有效记录', [], ontology(), {}, 'test')
    assert result.plan.filters == []
    assert result.plan.source_steps[0].query.filters[0].field == 'client.status'
    raw['source_steps'][0]['query']['filters'] = []
    with pytest.raises(ValueError, match='未落实到 filters'):
        agent._build_outcome(raw, '查有效记录', [], ontology(), {}, 'test')


def test_explicit_predicate_adjacent_to_chinese_text_is_not_silently_ignored():
    raw = proposal()
    raw['assumptions'] = ["仅保留client.status='active'筛选有效记录"]
    raw['source_steps'][0]['query']['filters'] = []
    with pytest.raises(ValueError, match='未落实到 filters'):
        AnalystAgent()._build_outcome(raw, '查有效记录', [], ontology(), {}, 'test')


def test_unqualified_cross_source_key_is_not_misreported_as_missing_output():
    from semora_agent.multisource import validate_source_steps
    from semora_contracts import QueryPlan
    raw=proposal()
    raw['source_steps'][1]['inputs'][0]['source_field']='customer_id'
    plan=QueryPlan(domain='commerce',data_source_id='customers-db',intent='query',metrics=[],source_steps=raw['source_steps'])
    with pytest.raises(ValueError) as error:
        validate_source_steps(plan,ontology())
    assert '字段引用无效' in str(error.value)
    assert 'client.customer_id' in str(error.value) and 'balances.customer_id' in str(error.value)
    assert '未包含在上一步输出' not in str(error.value)
