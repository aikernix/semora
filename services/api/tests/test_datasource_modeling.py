from app.datasource_modeling import (
    ModelingRepository,
    discover_candidates,
    package_objects,
    package_relations,
)
from fastapi import HTTPException
import pytest


def table(identifier, name, columns, *, primary=None, indexes=None, description=""):
    return {
        "id": identifier,
        "schema": "public",
        "name": name,
        "description": description,
        "columns": [
            {"name": column, "type": kind, "nullable": False, "description": comment}
            for column, kind, comment in columns
        ],
        "primary_keys": [{"name": "pk", "columns": primary or []}] if primary else [],
        "unique_constraints": [],
        "indexes": [{"name": f"idx_{index}", "columns": [index]} for index in (indexes or [])],
        "foreign_keys": [],
    }


def test_detects_indexed_entity_id_without_foreign_key():
    customers = table("public.customer", "customer", [("id", "bigint", "客户编号")], primary=["id"])
    orders = table(
        "public.orders",
        "orders",
        [("id", "bigint", "订单编号"), ("customer_id", "bigint", "客户编号")],
        primary=["id"],
        indexes=["customer_id"],
    )
    candidates = discover_candidates("source-a", [customers, orders])
    candidate = next(
        item
        for item in candidates
        if item["source_table_id"] == "public.orders"
        and item["target_table_id"] == "public.customer"
    )
    assert candidate["key_pairs"] == [{"source": "customer_id", "target": "id"}]
    assert candidate["status"] == "pending"
    assert any(item["kind"] == "source_code" for item in candidate["evidence"])


def test_generic_integer_id_overlap_does_not_create_relation():
    left = table("public.left", "left", [("id", "bigint", "")], primary=["id"])
    right = table("public.right", "right", [("id", "bigint", "")], primary=["id"])
    assert discover_candidates("source-a", [left, right]) == []


def test_comments_can_recall_differently_named_indexed_fields_without_auto_accepting():
    customer = table(
        "public.customer",
        "customer",
        [("member_key", "bigint", "客户唯一编号")],
        primary=["member_key"],
        description="客户资料",
    )
    activity = table(
        "public.activity",
        "activity",
        [("owner_no", "bigint", "客户唯一编号")],
        indexes=["owner_no"],
        description="活动记录",
    )
    candidates = discover_candidates("source-a", [customer, activity])
    candidate = next(item for item in candidates if item["source_table_id"] == "public.activity")
    assert candidate["key_pairs"] == [{"source": "owner_no", "target": "member_key"}]
    assert candidate["status"] == "pending"


def test_declared_composite_foreign_key_is_accepted_and_compiled_as_tuple_pairs():
    parent = table(
        "public.parent",
        "parent",
        [("tenant_id", "integer", ""), ("id", "bigint", "")],
        primary=["tenant_id", "id"],
    )
    child = table(
        "public.child",
        "child",
        [("tenant_id", "integer", ""), ("parent_id", "bigint", "")],
        indexes=["tenant_id"],
    )
    foreign_key = {
        "source_table": "public.child",
        "columns": ["tenant_id", "parent_id"],
        "target_table": "public.parent",
        "target_columns": ["tenant_id", "id"],
    }
    candidates = discover_candidates(
        "source-a", [parent, child], declared_relationships=[foreign_key]
    )
    accepted = next(item for item in candidates if item["evidence_grade"] == "foreign_key")
    assert accepted["status"] == "accepted"
    assert accepted["key_pairs"] == [
        {"source": "tenant_id", "target": "tenant_id"},
        {"source": "parent_id", "target": "id"},
    ]
    objects, mapping = package_objects("source-a", [parent, child])
    relations = package_relations([accepted], mapping)
    assert len(objects) == 2
    assert " AND " in relations[0]["condition"]


def test_cross_source_candidate_identity_is_domain_scoped():
    customer = table("public.customer", "customer", [("id", "bigint", "客户编号")], primary=["id"])
    event = table(
        "public.event",
        "event",
        [("customer_id", "bigint", "客户编号")],
        indexes=["customer_id"],
    )
    first = discover_candidates("a", [event], "b", [customer], "domain_a")
    second = discover_candidates("a", [event], "b", [customer], "domain_b")
    assert first and second and first[0]["id"] != second[0]["id"]


def test_job_and_package_repository_has_in_memory_fallback():
    repository = ModelingRepository(dsn="")
    job = repository.create_job("source-a")
    assert repository.latest_job("source-a")["id"] == job["id"]
    package = repository.save_package(
        "source-a", "hash", {"objects": [], "relations": []}, "ready", 0
    )
    assert repository.get_package("source-a")["schema_hash"] == package["schema_hash"]


def test_cross_source_results_include_both_relationship_directions():
    repository = ModelingRepository(dsn="")
    base = {
        "scope": "cross_source",
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
        "domain": "demo",
    }
    repository.replace_candidates(
        [
            {**base, "id": "forward", "source_datasource_id": "a", "target_datasource_id": "b"},
            {**base, "id": "reverse", "source_datasource_id": "b", "target_datasource_id": "a"},
            {**base, "id": "other", "source_datasource_id": "a", "target_datasource_id": "c"},
        ],
        "left-hash",
        "right-hash",
    )
    assert {item["id"] for item in repository.list_cross_candidates("a", "b", "demo")} == {
        "forward",
        "reverse",
    }


def test_schema_change_marks_old_relationship_candidates_stale():
    repository = ModelingRepository(dsn="")
    candidate = {
        "id": "candidate",
        "scope": "single_source",
        "source_datasource_id": "a",
        "target_datasource_id": "a",
        "source_table_id": "public.child",
        "target_table_id": "public.parent",
        "source_columns": ["parent_id"],
        "target_columns": ["id"],
        "key_pairs": [{"source": "parent_id", "target": "id"}],
        "condition_template": {"operator": "and_equals"},
        "evidence": [],
        "evidence_grade": "strong",
        "confidence": 0.99,
        "cardinality": "N:1",
        "status": "accepted",
        "domain": None,
    }
    repository.replace_candidates([candidate], "old", "old")
    repository.mark_candidates_stale("a", "new")
    assert repository.list_candidates("a")[0]["status"] == "stale"


def test_rejected_relation_remains_rejected_after_rescan():
    repository = ModelingRepository(dsn="")
    candidate = {
        "id": "candidate",
        "scope": "single_source",
        "source_datasource_id": "a",
        "target_datasource_id": "a",
        "source_table_id": "public.child",
        "target_table_id": "public.parent",
        "source_columns": ["parent_id"],
        "target_columns": ["id"],
        "key_pairs": [{"source": "parent_id", "target": "id"}],
        "condition_template": {"operator": "and_equals"},
        "evidence": [],
        "evidence_grade": "medium",
        "confidence": 0.8,
        "cardinality": "N:1",
        "status": "rejected",
        "domain": None,
    }
    repository.replace_candidates([candidate], "old", "old")
    repository.mark_candidates_stale("a", "new")
    repository.replace_candidates([{**candidate, "status": "pending"}], "new", "new")
    assert repository.list_candidates("a")[0]["status"] == "rejected"


def test_executor_profiles_directional_membership_without_returning_values(monkeypatch):
    from services.executor.app import main as executor

    def fake_execute(body, _token=None):
        if body.parameters:
            return {"columns": ["id"], "rows": [[1], [2]]}
        return {"columns": ["customer_id"], "rows": [[1], [2]]}

    monkeypatch.setattr(executor, "execute_query", fake_execute)
    body = executor.ProfileInput(
        datasource={
            "datasource_id": "a",
            "type": "postgresql",
            "secret_ref": "DATASOURCE_A_URL",
        },
        source_table="public.orders",
        source_columns=["customer_id"],
        target_table="public.customer",
        target_columns=["id"],
        sample_rows=10,
        source_index_confirmed=True,
        target_unique_index_confirmed=True,
    )
    result = executor.profile_relationship(body)
    assert result["directional_coverage"] == 1
    assert result["raw_values_retained"] is False
    assert "rows" not in result

    with pytest.raises(HTTPException, match="索引"):
        executor.profile_relationship(body.model_copy(update={"source_index_confirmed": False}))
