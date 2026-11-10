from types import SimpleNamespace

from fastapi.testclient import TestClient


def test_configuration_routes_save_effective_and_validate_sources(monkeypatch):
    from app import main
    calls = []

    class Ontology:
        async def create_domain(self, domain, name):
            calls.append(("create", domain, name))
            return {"domain": domain, "name": name, "objects": []}

        async def save_configuration(self, domain, value):
            calls.append(("save", domain, value))
            return value

    def source(identifier):
        if identifier != "registered":
            raise KeyError(identifier)
        return SimpleNamespace(name="Source", status="healthy", access="read_only")

    monkeypatch.setattr(main, "ontology_service", Ontology())
    monkeypatch.setattr(main, "datasource_service", SimpleNamespace(repository=SimpleNamespace(get=source)))
    monkeypatch.setenv("AUTH_REQUIRED", "false")
    client = TestClient(main.app)
    assert client.post("/api/v1/ontology/domains", json={"domain": "example", "name": " Example "}).status_code == 201
    assert calls == [("create", "example", "Example")]
    assert client.post("/api/v1/ontology/domains", json={"domain": "123", "name": "X"}).status_code == 422
    path = "/api/v1/ontology/configurations/example"
    for body in ({}, {"objects": []}, {"objects": [None]}, {"objects": [{"dataSourceId": "unknown"}]}):
        assert client.put(path, json=body).status_code == 422
    body = {"domain": "example", "objects": [{"name": "record", "dataSourceId": "registered"}]}
    assert client.put(path, json=body).status_code == 200
    assert calls[-1] == ("save", "example", body)
    assert len(calls) == 2  # no separate publish action
