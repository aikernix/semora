import asyncio

import httpx

from app.knowledge import KnowledgeCoreClient, WeKnoraClient


def test_knowledge_client_maps_internal_results_to_evidence():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["token"] = request.headers.get("X-Semora-Internal-Token")
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "id": "chunk-1",
                        "knowledge_base_id": "kb-1",
                        "knowledge_id": "doc-1",
                        "chunk_index": 0,
                        "locator": "第 1 页 / 分块 1",
                        "title": "贷款口径",
                        "content": "贷款余额取有效贷款余额之和。",
                        "metadata": {"revision": "hash-1"},
                    }
                ]
            },
        )

    client = KnowledgeCoreClient("http://knowledge:8003", "internal-secret", httpx.MockTransport(handler))
    evidence = asyncio.run(client.search_evidence("贷款余额", ["kb-1"]))

    assert seen == {"path": "/internal/v1/search", "token": "internal-secret"}
    assert evidence[0].locator == "第 1 页 / 分块 1"
    assert evidence[0].revision == "hash-1"


def test_knowledge_client_routes_manual_retry_to_internal_worker_queue():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["token"] = request.headers.get("X-Semora-Internal-Token")
        return httpx.Response(202, json={"document_id": "doc-1", "state": "queued"})

    client = KnowledgeCoreClient("http://knowledge:8003", "internal-secret", httpx.MockTransport(handler))
    result = asyncio.run(client.retry_document("doc-1"))

    assert seen == {
        "method": "POST",
        "path": "/internal/v1/documents/doc-1/retry",
        "token": "internal-secret",
    }
    assert result["state"] == "queued"


def test_knowledge_client_download_preserves_original_metadata():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/internal/v1/documents/doc-1/content"
        return httpx.Response(
            200,
            content=b"bank-policy",
            headers={
                "Content-Type": "text/markdown",
                "X-Semora-Filename": "policy.md",
                "X-Semora-Content-Hash": "abc123",
            },
        )

    client = KnowledgeCoreClient("http://knowledge:8003", "internal-secret", httpx.MockTransport(handler))
    content, file_name, content_type, content_hash = asyncio.run(client.download_document("doc-1"))

    assert content == b"bank-policy"
    assert file_name == "policy.md"
    assert content_type == "text/markdown"
    assert content_hash == "abc123"


def test_weknora_research_traverses_wiki_pages_and_returns_traceable_evidence():
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path, request.headers.get("X-API-Key")))
        if request.url.path == "/api/v1/knowledgebase/kb-1/wiki/search":
            return httpx.Response(
                200,
                json={"pages": [{"slug": "concept/loan-balance", "title": "贷款余额"}]},
            )
        if request.url.path == "/api/v1/knowledgebase/kb-1/wiki/pages/concept/loan-balance":
            return httpx.Response(
                200,
                json={
                    "id": "page-1",
                    "slug": "concept/loan-balance",
                    "title": "贷款余额",
                    "content": "贷款余额取有效贷款余额之和。",
                    "version": 3,
                    "source_refs": ["doc-1"],
                    "chunk_refs": ["chunk-1"],
                    "out_links": ["entity/loan"],
                    "in_links": [],
                },
            )
        assert request.url.path == "/api/v1/knowledgebase/kb-1/wiki/pages/entity/loan"
        return httpx.Response(
            200,
            json={
                "id": "page-2",
                "slug": "entity/loan",
                "title": "贷款",
                "content": "贷款是授信合同项下的实际放款记录。",
                "version": 2,
                "source_refs": ["doc-2"],
                "chunk_refs": ["chunk-2"],
                "out_links": [],
                "in_links": ["concept/loan-balance"],
            },
        )

    client = WeKnoraClient("https://weknora.example/api/v1", "wk-secret", httpx.MockTransport(handler))
    result = asyncio.run(client.research("贷款余额是多少", ["kb-1"]))

    assert requests == [
        ("GET", "/api/v1/knowledgebase/kb-1/wiki/search", "wk-secret"),
        ("GET", "/api/v1/knowledgebase/kb-1/wiki/pages/concept/loan-balance", "wk-secret"),
        ("GET", "/api/v1/knowledgebase/kb-1/wiki/pages/entity/loan", "wk-secret"),
    ]
    assert result.agent_id == "semora-weknora-wiki-researcher"
    assert result.session_id == ""
    assert "贷款余额" in result.answer
    assert result.evidence[0].chunk_id == "chunk-1"
    assert any(step.tool == "wiki_search" for step in result.steps)
    assert any(step.tool == "wiki_read_page" for step in result.steps)
    assert len(result.evidence) == 2
