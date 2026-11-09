import pytest
import asyncio
import httpx

from app.ontology import OntologyCoreClient, OntologyCoreError


@pytest.mark.parametrize("legacy", [False, True])
def test_snapshot_consumes_ontology_without_wren_dependency(legacy):
    def handler(request):
        assert request.url.params["domain"] == "example"
        payload = {
            "domain": "example",
            "version": "1",
            "contentHash": "hash",
            "artifactVersion": "old-mdl" if legacy else "semora-ontology-v1",
            "ontology": {"domain": "example", "objects": [], "relations": []},
        }
        if legacy:
            payload["wrenArtifact"] = {"models": [{"name": "obsolete"}]}
        return httpx.Response(200, json=payload)

    client = OntologyCoreClient()

    async def request(method, path, **kwargs):
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="http://test"
        ) as transport:
            return (await transport.request(method, path, **kwargs)).json()

    client._request = request
    ref, ontology = asyncio.run(client.latest_snapshot_bundle("example"))
    assert ref.domain == "example" and ref.content_hash == "hash"
    assert ontology["objects"] == [] and ontology["metrics"] == []
    assert "_wrenArtifact" not in ontology and "_wrenArtifactVersion" not in ontology


def test_invalid_non_null_ontology_collection_fails_closed():
    with pytest.raises(OntologyCoreError, match="metrics"):
        OntologyCoreClient._normalize_collections({"metrics": "count"})


def test_publication_status_distinguishes_draft_from_query_snapshot(monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app, ontology_service
    from semora_contracts import OntologySnapshotRef

    async def draft(domain):
        return {"version": "0.2.0", "objects": [{}] * 107, "relations": [{}] * 201, "metrics": []}

    async def snapshot(domain):
        return OntologySnapshotRef(
            domain=domain, version="0.1.0", content_hash="old", artifact_version="1"
        ), {"objects": [{}] * 3, "relations": [], "metrics": []}

    monkeypatch.setattr(ontology_service, "get_draft", draft)
    monkeypatch.setattr(ontology_service, "latest_snapshot_bundle", snapshot)
    response = TestClient(app).get("/api/v1/ontology/publication-status?domain=example")
    assert response.status_code == 200
    assert response.json()["draft_counts"]["objects"] == 107
    assert response.json()["published_counts"]["objects"] == 3
    assert response.json()["published_version"] == "0.1.0"


def test_publication_status_reports_unpublished_domain(monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app, ontology_service

    async def draft(domain):
        return {"version": "1", "objects": [{}], "relations": [], "metrics": []}

    async def snapshot(domain):
        raise OntologyCoreError("not published", 404)

    monkeypatch.setattr(ontology_service, "get_draft", draft)
    monkeypatch.setattr(ontology_service, "latest_snapshot_bundle", snapshot)
    response = TestClient(app).get("/api/v1/ontology/publication-status?domain=example")
    assert response.status_code == 200
    assert response.json()["published_version"] is None
