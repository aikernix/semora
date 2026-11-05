import asyncio

import pytest

from app.ontology import OntologyCoreClient, OntologyCoreError
from semora_agent import AnalystAgent
from services.api.tests.test_analyst import StubModel


@pytest.mark.parametrize('published', [False, True])
def test_null_go_collections_are_usable_by_analyst(published):
    raw = {
        'domain': 'bank', 'version': '1', 'dataSourceId': 'mysql',
        'objects': [{'name': 'loan', 'properties': [{'name': 'id'}]}],
        'metrics': None, 'relations': None, 'knowledgeBaseIds': None,
        'defaultFilters': None,
    }
    client = OntologyCoreClient()

    async def request(*args, **kwargs):
        if published:
            return {'ontology': raw, 'domain': 'bank', 'version': '1', 'contentHash': 'hash', 'artifactVersion': '1'}
        return raw

    client._request = request

    async def scenario():
        if published:
            reference, ontology = await client.latest_snapshot_bundle('bank')
            assert reference.knowledge_base_ids == []
        else:
            ontology = await client.get_draft('bank')
        assert ontology['metrics'] == [] and ontology['relations'] == []
        model = StubModel({'metrics': [], 'fields': ['loan.id'], 'grain': 'loan'})
        model.required = True
        outcome = await AnalystAgent(model).analyse('贷款明细', [], ontology, {})
        assert outcome.plan.fields == ['loan.id']
        assert outcome.model_mode == 'llm'
        assert raw['metrics'] is None

    asyncio.run(scenario())


def test_invalid_non_null_ontology_collection_fails_closed():
    with pytest.raises(OntologyCoreError, match='metrics'):
        OntologyCoreClient._normalize_collections({'metrics': 'count'})


def test_publication_status_distinguishes_draft_from_query_snapshot(monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app, ontology_service
    from semora_contracts import OntologySnapshotRef

    async def draft(domain):
        return {'version': '0.2.0', 'objects': [{}] * 107, 'relations': [{}] * 201, 'metrics': []}

    async def snapshot(domain):
        return OntologySnapshotRef(domain=domain, version='0.1.0', content_hash='old', artifact_version='1'), {'objects': [{}] * 3, 'relations': [], 'metrics': []}

    monkeypatch.setattr(ontology_service, 'get_draft', draft)
    monkeypatch.setattr(ontology_service, 'latest_snapshot_bundle', snapshot)
    response = TestClient(app).get('/api/v1/ontology/publication-status?domain=example')
    assert response.status_code == 200
    assert response.json()['draft_counts']['objects'] == 107
    assert response.json()['published_counts']['objects'] == 3
    assert response.json()['published_version'] == '0.1.0'


def test_publication_status_reports_unpublished_domain(monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app, ontology_service

    async def draft(domain):
        return {'version': '1', 'objects': [{}], 'relations': [], 'metrics': []}

    async def snapshot(domain):
        raise OntologyCoreError('not published', 404)

    monkeypatch.setattr(ontology_service, 'get_draft', draft)
    monkeypatch.setattr(ontology_service, 'latest_snapshot_bundle', snapshot)
    response = TestClient(app).get('/api/v1/ontology/publication-status?domain=example')
    assert response.status_code == 200
    assert response.json()['published_version'] is None
