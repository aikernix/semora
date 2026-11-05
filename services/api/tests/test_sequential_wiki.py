import asyncio
import json

import pytest
import httpx

from app import agentic
from app.store import Store
from app.workflow import Workflow
from app.knowledge import WeKnoraClient
from semora_agent import AnalystAgent
from semora_agent.model import ModelError
from semora_contracts import QueryRun
from services.api.tests.test_agent_runtime import NativeModel, CARD
from services.api.tests.test_workflow import fake_execute, fake_retrieve, fake_snapshot


class Wiki:
    provider = 'weknora'
    _page_evidence = staticmethod(WeKnoraClient._page_evidence)

    def __init__(self):
        self.queries = []

    async def _wiki_search(self, kb_id, query, limit):
        self.queries.append(query)
        if query == '逾期定义':
            return []
        return [{'slug': 'concept/' + query, 'title': query, 'content': query + '业务定义'}]

    async def _wiki_page(self, kb_id, slug):
        return {'slug': slug, 'title': slug, 'content': '可追溯的业务口径', 'out_links': [], 'source_refs': ['document']}


@pytest.mark.parametrize('query', ['逾期定义 贷款逾期 还款记录 合同', '贷款、合同', '贷款|逾期', '贷款，', '   '])
def test_multi_keyword_requests_are_returned_to_model_without_remote_search(monkeypatch, query):
    wiki = Wiki()
    monkeypatch.setattr(agentic, 'knowledge_service', wiki)
    async def scenario():
        workflow = Workflow(Store(), fake_execute, fake_retrieve, fake_snapshot)
        ref, ontology = await fake_snapshot('retail_credit')
        run = QueryRun(session_id='demo-session', question='贷款逾期', ontology=ref)
        tool = next(t for t in agentic.AgentTools(workflow, run, ontology, {}).tools() if t.name == 'search_wiki')
        result = json.loads(await tool.ainvoke({'kb_id': ontology['knowledgeBaseIds'][0], 'query': query}))
        assert result['error'] == 'single_keyword_required'
        assert wiki.queries == []
    asyncio.run(scenario())


def test_model_searches_shorter_keyword_after_zero_hits_then_reads_and_searches_again(monkeypatch):
    wiki = Wiki()
    monkeypatch.setattr(agentic, 'knowledge_service', wiki)
    async def scenario():
        workflow = Workflow(Store(), fake_execute, fake_retrieve, fake_snapshot)
        ref, ontology = await fake_snapshot('retail_credit')
        kb = ontology['knowledgeBaseIds'][0]
        run = QueryRun(session_id='demo-session', question='贷款逾期和还款口径', ontology=ref)
        model = NativeModel([
            ('search_wiki', {'kb_id': kb, 'query': '逾期定义'}),
            ('search_wiki', {'kb_id': kb, 'query': '逾期'}),
            ('read_wiki', {'kb_id': kb, 'slug': 'concept/逾期'}),
            ('search_wiki', {'kb_id': kb, 'query': '还款'}),
            ('read_wiki', {'kb_id': kb, 'slug': 'concept/还款'}),
            ('AgentSubmission', {'payload': CARD}),
        ])
        workflow.analyst = AnalystAgent(model)
        outcome = await agentic.analyse(workflow, run, run.question, ontology, {})
        assert outcome.plan is None
        assert wiki.queries == ['逾期定义', '逾期', '还款']
        assert len(run.evidence) == 2 and len(model.messages) == 6
        zero = json.loads(next(m['content'] for m in reversed(model.messages[1]) if m['role'] == 'tool'))
        assert zero['count'] == 0 and '不能判定整个知识库没有内容' in zero['message']
        assert any(m['role'] == 'tool' and '可追溯的业务口径' in str(m['content']) for m in model.messages[3])
    asyncio.run(scenario())


def test_provider_batch_is_limited_to_one_action_and_next_turn_sees_observation(monkeypatch):
    wiki = Wiki()
    monkeypatch.setattr(agentic, 'knowledge_service', wiki)
    class BatchingModel(NativeModel):
        def __init__(self, kb):
            self.kb = kb
            super().__init__([
                ('search_wiki', {'kb_id': kb, 'query': '逾期'}),
                ('search_wiki', {'kb_id': kb, 'query': '贷款'}),
                ('AgentSubmission', {'payload': CARD}),
            ])
        async def complete_messages(self, messages, tools, **kwargs):
            assert kwargs['parallel_tool_calls'] is False
            result = await super().complete_messages(messages, tools, **kwargs)
            if len(self.messages) == 1:
                result['tool_calls'].append({'id': 'ignored-batch', 'type': 'function', 'function': {'name': 'search_wiki', 'arguments': json.dumps({'kb_id': self.kb, 'query': '合同'})}})
            return result
    async def scenario():
        workflow = Workflow(Store(), fake_execute, fake_retrieve, fake_snapshot)
        ref, ontology = await fake_snapshot('retail_credit')
        kb = ontology['knowledgeBaseIds'][0]
        run = QueryRun(session_id='demo-session', question='贷款逾期', ontology=ref)
        model = BatchingModel(kb)
        workflow.analyst = AnalystAgent(model)
        await agentic.analyse(workflow, run, run.question, ontology, {})
        assert wiki.queries == ['逾期', '贷款']
        assert '合同' not in wiki.queries
        assert len(model.messages[1][-2]['tool_calls']) == 1
        assert model.messages[1][-1]['role'] == 'tool'
        assert any(e.event == 'agent.actions.deferred' for e in run.events)
    asyncio.run(scenario())


def test_plain_text_final_is_retried_with_forced_submission_and_existing_observations():
    class TextFinalModel(NativeModel):
        def __init__(self):
            super().__init__([
                ('read_object', {'name': 'loan'}),
                ('AgentSubmission', {'payload': CARD}),
            ])
            self.provider_calls = 0
        async def complete_messages(self, messages, tools, **kwargs):
            self.provider_calls += 1
            if self.provider_calls == 2:
                return {'content': '需要用户补充时间范围', 'tool_calls': []}
            if self.provider_calls == 3:
                assert kwargs['tool_choice'] == {'type': 'function', 'function': {'name': 'AgentSubmission'}}
                assert any(m['role'] == 'tool' and 'outstanding_balance' in str(m['content']) for m in messages)
            return await super().complete_messages(messages, tools, **kwargs)
    async def scenario():
        workflow = Workflow(Store(), fake_execute, fake_retrieve, fake_snapshot)
        ref, ontology = await fake_snapshot('retail_credit')
        run = QueryRun(session_id='demo-session', question='贷款余额', ontology=ref)
        model = TextFinalModel()
        workflow.analyst = AnalystAgent(model)
        outcome = await agentic.analyse(workflow, run, run.question, ontology, {})
        assert outcome.plan is None and model.provider_calls == 3
        assert sum(e.event == 'agent.tool.completed' for e in run.events) == 1
        assert any(e.event == 'agent.output.retry' for e in run.events)
    asyncio.run(scenario())


def test_repeated_text_finals_fail_with_accurate_delivery_error():
    class Model:
        configured = required = True
        calls = 0
        async def complete_messages(self, *args, **kwargs):
            self.calls += 1
            return {'content': '普通文字', 'tool_calls': []}
    async def scenario():
        workflow = Workflow(Store(), fake_execute, fake_retrieve, fake_snapshot)
        ref, ontology = await fake_snapshot('retail_credit')
        run = QueryRun(session_id='demo-session', question='贷款', ontology=ref)
        model = Model()
        workflow.analyst = AnalystAgent(model)
        with pytest.raises(ModelError, match='连续 3 次未按结构化交付协议'):
            await agentic.analyse(workflow, run, run.question, ontology, {})
        assert model.calls == 3
    asyncio.run(scenario())


@pytest.mark.parametrize('bound_ids,bad_action', [
    (['kb-one'], None),
    (['kb-one'], {'action': 'wiki_search', 'kb_id': 'other-tenant', 'query': '逾期'}),
    (['kb-one', 'kb-two'], {'action': 'wiki_search', 'query': '逾期'}),
])
def test_wiki_test_entry_resolves_single_scope_and_corrects_invalid_ids(monkeypatch, bound_ids, bad_action):
    from semora_agent import model as model_module
    chosen = bound_ids[-1]
    actions = ([bad_action] if bad_action else []) + [
        {'action': 'wiki_search', 'query': '逾期', **({'kb_id': chosen} if len(bound_ids) > 1 else {})},
        {'action': 'wiki_read_page', 'slug': 'loan', **({'kb_id': chosen} if len(bound_ids) > 1 else {})},
        {'action': 'finish', 'summary': '根据已读来源继续分析'},
    ]
    class Model:
        configured = required = True
        observations = []
        async def complete_json(self, system, user):
            self.observations.append(json.loads(user))
            return actions.pop(0)
    model = Model()
    model.observations = []
    monkeypatch.setattr(model_module, 'structured_model', model)
    calls = []
    def respond(request):
        calls.append(str(request.url))
        assert f'/knowledgebase/{chosen}/wiki/' in request.url.path
        if request.url.path.endswith('/search'):
            return httpx.Response(200, json={'pages': [{'slug': 'loan', 'title': '贷款'}]})
        return httpx.Response(200, json={'data': {'slug': 'loan', 'title': '贷款', 'content': '贷款口径'}})
    client = WeKnoraClient('https://wiki.example/api/v1', 'test-key', httpx.MockTransport(respond))
    result = asyncio.run(client.research('贷款逾期', bound_ids))
    assert len(calls) == 2 and len(result.evidence) == 1
    assert all('other-tenant' not in url for url in calls)
    if bad_action:
        assert model.observations[1]['observations'][0]['error'] == 'invalid_knowledge_base'
        assert model.observations[1]['observations'][0]['allowed_kb_ids'] == bound_ids


def test_native_wiki_tool_defaults_only_for_a_single_binding(monkeypatch):
    wiki = Wiki()
    monkeypatch.setattr(agentic, 'knowledge_service', wiki)
    async def scenario():
        workflow = Workflow(Store(), fake_execute, fake_retrieve, fake_snapshot)
        ref, ontology = await fake_snapshot('retail_credit')
        run = QueryRun(session_id='demo-session', question='贷款逾期', ontology=ref)
        tools = {t.name: t for t in agentic.AgentTools(workflow, run, ontology, {}).tools()}
        result = json.loads(await tools['search_wiki'].ainvoke({'query': '逾期'}))
        assert result['count'] == 1
        await tools['read_wiki'].ainvoke({'slug': 'concept/逾期'})
        assert len(run.evidence) == 1
        denied = json.loads(await tools['search_wiki'].ainvoke({'query': '合同', 'kb_id': 'other-tenant'}))
        assert denied['error'] == 'invalid_knowledge_base'
        ontology = {**ontology, 'knowledgeBaseIds': ['kb-one', 'kb-two']}
        tool = next(t for t in agentic.AgentTools(workflow, run, ontology, {}).tools() if t.name == 'search_wiki')
        missing = json.loads(await tool.ainvoke({'query': '合同'}))
        assert missing['allowed_kb_ids'] == ['kb-one', 'kb-two']
        assert wiki.queries == ['逾期']
    asyncio.run(scenario())


def test_client_forwards_sequential_calling_flag_and_filters_private_reasoning(monkeypatch):
    from semora_agent import model as model_module
    original = model_module.httpx.AsyncClient
    def respond(request):
        body = json.loads(request.content)
        assert body['parallel_tool_calls'] is False
        assert body['tool_choice']['function']['name'] == 'AgentSubmission'
        return httpx.Response(200, json={'choices': [{'finish_reason': 'tool_calls', 'message': {'reasoning_content': 'PRIVATE', 'tool_calls': []}}]})
    monkeypatch.setattr(model_module.httpx, 'AsyncClient', lambda **kwargs: original(transport=httpx.MockTransport(respond), **kwargs))
    model = model_module.StructuredModel()
    model.base_url, model.api_key = 'https://model.example/v1', 'test-key'
    result = asyncio.run(model.complete_messages([], [], tool_choice={'type': 'function', 'function': {'name': 'AgentSubmission'}}, parallel_tool_calls=False))
    assert result['finish_reason'] == 'tool_calls'
    assert 'PRIVATE' not in json.dumps(result)
