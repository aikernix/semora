import asyncio
import json

import httpx
import pytest

from semora_agent import model as model_module
from semora_agent.model import ModelError, StructuredModel


def client_with_transport(monkeypatch, handler):
    original = httpx.AsyncClient
    monkeypatch.setattr(model_module.httpx, 'AsyncClient', lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs))
    client = StructuredModel()
    client.base_url, client.api_key = 'https://model.example/v1', 'PRIVATE_KEY'
    client.timeout_seconds, client.request_retries = 300, 1
    return client


def test_read_timeout_retries_same_model_request_and_emits_safe_metadata(monkeypatch):
    requests, events = [], []
    def handler(request):
        requests.append(json.loads(request.content))
        if len(requests) == 1:
            raise httpx.ReadTimeout('PRIVATE_TRANSPORT_DETAIL', request=request)
        return httpx.Response(200, json={'choices': [{'message': {'content': '', 'tool_calls': [], 'reasoning_content': 'PRIVATE_REASONING'}, 'finish_reason': 'stop'}]})
    client = client_with_transport(monkeypatch, handler)
    result = asyncio.run(client.complete_messages([{'role': 'user', 'content': 'PRIVATE_QUESTION'}], [], on_request_event=lambda name, data: events.append((name, data))))
    assert len(requests) == 2 and requests[0] == requests[1]
    assert len([e for e in events if e[0] == 'agent.request.retry']) == 1
    assert events[0][1]['request_bytes'] > 0 and events[0][1]['message_count'] == 1
    assert events[-1][0] == 'agent.request.completed'
    public = json.dumps([events, result])
    assert all(value not in public for value in ['PRIVATE_KEY', 'PRIVATE_QUESTION', 'PRIVATE_REASONING', 'PRIVATE_TRANSPORT_DETAIL'])


def test_exhausted_read_timeout_is_not_reported_as_tool_incompatibility(monkeypatch):
    calls = []
    def handler(request):
        calls.append(True)
        raise httpx.ReadTimeout('timeout', request=request)
    client = client_with_transport(monkeypatch, handler)
    with pytest.raises(ModelError, match='ReadTimeout') as error:
        asyncio.run(client.complete_messages([], []))
    assert len(calls) == 2
    assert '300' in str(error.value) and '已重试 1 次' in str(error.value)
    assert 'tool calling' not in str(error.value)


def test_auth_failure_is_not_retried_or_leaked(monkeypatch):
    calls = []
    def handler(request):
        calls.append(True)
        return httpx.Response(401, json={'error': 'PRIVATE_PROVIDER_BODY'})
    client = client_with_transport(monkeypatch, handler)
    with pytest.raises(ModelError, match='HTTP 401') as error:
        asyncio.run(client.complete_messages([], []))
    assert len(calls) == 1 and 'PRIVATE_PROVIDER_BODY' not in str(error.value)
