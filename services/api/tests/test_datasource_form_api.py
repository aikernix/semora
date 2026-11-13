from fastapi.testclient import TestClient

from app import database_routes, main
from app.datasource_modeling import ModelingRepository, ModelingService, package_objects
from app.datasources import DataSourceRecord, DataSourceRepository, DataSourceService


def _table(identifier, name, columns, primary=None):
    return {
        "id": identifier,
        "schema": "public",
        "name": name,
        "kind": "TABLE",
        "description": f"{name} table",
        "columns": [
            {"name": column, "type": kind, "raw_type": kind, "nullable": False}
            for column, kind in columns
        ],
        "primary_keys": [{"name": "pk", "columns": primary or []}] if primary else [],
        "unique_constraints": [],
        "foreign_keys": [],
        "indexes": [],
    }


def test_table_catalog_is_paginated_from_persisted_snapshot():
    repository = DataSourceRepository(dsn="")
    source = DataSourceRecord("source", "Source", "postgresql", "DATASOURCE_SOURCE_URL")
    source.scan_result["tables"] = [
        _table(f"public.table_{index}", f"table_{index}", [("id", "bigint")], ["id"])
        for index in range(35)
    ]
    repository.save(source)
    page = repository.table_page("source", "table_2", page=1, page_size=5)
    assert page["total"] == 11
    assert len(page["items"]) == 5
    assert repository.table_detail("source", page["items"][0]["id"])["columns"][0]["name"] == "id"


def test_relation_edit_soft_delete_and_restore(monkeypatch):
    tables = [
        _table("public.entity", "entity", [("id", "bigint")], ["id"]),
        _table("public.event", "event", [("id", "bigint"), ("entity_id", "bigint")], ["id"]),
    ]
    source_repository = DataSourceRepository(dsn="")
    record = DataSourceRecord(
        "source", "Source", "postgresql", "DATASOURCE_SOURCE_URL", status="healthy", objects=2
    )
    record.scan_result["tables"] = tables
    source_repository.save(record)
    source_service = DataSourceService(repository=source_repository)
    modeling_repository = ModelingRepository(dsn="")
    modeling = ModelingService(source_service, executor_request=None, repository=modeling_repository)
    objects, _ = package_objects("source", tables)
    from app.datasource_table_storage import tables_digest

    schema_hash = tables_digest(tables)
    modeling_repository.save_package(
        "source",
        schema_hash,
        {"objects": objects, "relations": []},
        "ready_with_pending",
        1,
    )
    candidate = {
        "id": "candidate",
        "scope": "single_source",
        "source_datasource_id": "source",
        "target_datasource_id": "source",
        "source_table_id": "public.event",
        "target_table_id": "public.entity",
        "source_columns": ["entity_id"],
        "target_columns": ["id"],
        "key_pairs": [{"source": "entity_id", "target": "id"}],
        "condition_template": {"operator": "and_equals"},
        "evidence": [],
        "evidence_grade": "medium",
        "confidence": 0.8,
        "cardinality": "N:1",
        "status": "pending",
        "domain": None,
    }
    modeling_repository.replace_candidates([candidate], schema_hash, schema_hash)
    monkeypatch.setattr(database_routes, "datasource_service", source_service)
    monkeypatch.setattr(database_routes, "modeling_service", modeling)
    client = TestClient(main.app)

    assert client.get("/api/v1/datasources/source/relationships").json()["total"] == 1
    edited = client.put(
        "/api/v1/datasources/source/relationships/candidate",
        json={
            "source_table_id": "public.event",
            "target_table_id": "public.entity",
            "key_pairs": [{"source": "entity_id", "target": "id"}],
            "cardinality": "N:1",
        },
    )
    assert edited.status_code == 200
    manual_id = edited.json()["id"]
    assert edited.json()["origin"] == "manual"
    assert edited.json()["modeling_job_id"]
    assert client.get("/api/v1/datasources/source/onboarding").json()["details"]["package_only"]
    assert client.delete(f"/api/v1/datasources/source/relationships/{manual_id}").json()["status"] == "rejected"
    assert client.post(f"/api/v1/datasources/source/relationships/{manual_id}/restore").json()["status"] == "accepted"
