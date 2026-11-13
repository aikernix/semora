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


def test_large_shared_primary_names_do_not_become_relationships():
    tables = [table(f"public.t{i}", f"t{i}", [("serialno", "bigint", "流水号")],
                    primary=["serialno"]) for i in range(161)]
    assert discover_candidates("source-a", tables) == []


def test_three_thousand_unrelated_tables_do_not_produce_dense_candidates():
    tables = [table(f"s.t{i}", f"t{i}", [("id", "bigint", "")], primary=["id"])
              for i in range(3000)]
    assert discover_candidates("large-source", tables) == []


def test_storage_annotations_do_not_link_unrelated_meanings():
    parent = table("parent", "alpha", [("a", "varchar", "事件编号；业务主键；原类型=VARCHAR2(40)")], primary=["a"])
    child = table("child", "beta", [("b", "varchar", "对象类型；原类型=VARCHAR2(40)")], indexes=["b"])
    assert discover_candidates("source", [parent, child]) == []


def test_composite_mapping_cannot_reuse_a_source_column():
    parent = table("parent", "alpha", [("a", "bigint", "关联编号"), ("b", "bigint", "关联编号")], primary=["a", "b"])
    child = table("child", "beta", [("c", "bigint", "关联编号")], indexes=["c"])
    assert discover_candidates("source", [parent, child]) == []


def test_unique_composite_prefix_is_not_unique():
    from app.datasource_modeling import _indexed
    value = table("t", "t", [], primary=["tenant_id", "id"])
    assert _indexed(value, ["tenant_id"])
    assert not _indexed(value, ["tenant_id"], unique=True)


@pytest.mark.parametrize("parent_name,key,child_name,reference", [
    ("customer", "id", "orders", "customer_id"),
    ("alpha_registry", "opaque_key", "beta_events", "owner_token"),
    ("资产", "唯一标识", "流水", "归属标识"),
])
def test_discovery_uses_evidence_after_complete_renaming(parent_name, key, child_name, reference):
    parent = table("parent", parent_name, [(key, "bigint", "归属实体唯一标识")], primary=[key])
    child = table("child", child_name, [(reference, "bigint", "归属实体唯一标识")], indexes=[reference])
    candidates = discover_candidates("arbitrary-source", [parent, child])
    assert len(candidates) == 1
    assert candidates[0]["key_pairs"] == [{"source": reference, "target": key}]
    assert candidates[0]["status"] == "pending"


def test_competing_targets_need_semantic_evidence_even_with_matching_values():
    from app.relationship_evidence import finalize_relationships
    targets = [table(f"p{i}", f"p{i}", [("key", "bigint", "实体标识")], primary=["key"])
               for i in range(2)]
    source = table("child", "child", [("key", "bigint", "实体标识")], indexes=["key"])
    candidates = discover_candidates("a", [source] + targets)
    proposals = [c for c in candidates if c["source_table_id"] == "child"]
    assert len(proposals) == 2
    for candidate in proposals:
        candidate["evidence"].append({"kind": "value_profile", "statistics": {
            "source_nonnull_distinct_keys": 200, "directional_coverage": 1}})
    finalize_relationships(proposals)
    assert all(c["status"] == "pending" for c in proposals)


def test_multiple_independent_targets_confirm_after_all_evidence_is_collected():
    from app.relationship_evidence import finalize_relationships
    proposals = []
    for role in ("reference", "identity"):
        proposals.append({"status": "pending", "confidence": .7, "evidence_grade": "medium",
                          "evidence": [
            {"kind": "competition", "ambiguous": True},
            {"kind": "target_unique_index", "confirmed": True},
            {"kind": "value_profile", "statistics": {"source_nonnull_distinct_keys": 100,
                                                       "directional_coverage": .99}},
            {"kind": "model_semantic_review", "supported": True, "confidence": .96,
             "target_role": role, "alternative_unresolved": False},
            {"kind": "independent_review", "supported": True, "confidence": .96,
             "target_role": role, "alternative_unresolved": False}]})
    finalize_relationships(proposals)
    assert all(c["status"] == "accepted" for c in proposals)
    # A later, concrete semantic diagnosis can still withhold confirmation.
    next(e for e in proposals[0]["evidence"] if e["kind"] == "model_semantic_review")["target_role"] = "replica"
    finalize_relationships(proposals)
    assert proposals[0]["status"] == "pending"
    assert proposals[1]["status"] == "accepted"


def test_explicit_comment_reference_and_different_chinese_wording_are_recalled():
    parent = table("registry", "registry", [("opaque", "bigint", "机构编号")], primary=["opaque"], description="机构资料")
    child = table("events", "events", [("owner", "bigint", "经办机构"),
                                         ("ref", "bigint", "外部编号；registry.opaque")])
    candidates = discover_candidates("source", [parent, child])
    assert {tuple(c["source_columns"]) for c in candidates} == {("owner",), ("ref",)}
    explicit = next(c for c in candidates if c["source_columns"] == ["ref"])
    assert any(e["kind"] == "explicit_field_reference" for e in explicit["evidence"])


def test_final_confirmation_keeps_missing_samples_and_unresolved_alternatives_pending():
    from app.relationship_evidence import finalize_relationships
    candidate = {"status": "pending", "confidence": .7, "evidence_grade": "medium", "evidence": [
        {"kind": "model_semantic_review", "supported": True, "confidence": .99,
         "target_role": "reference", "alternative_unresolved": False}]}
    finalize_relationships([candidate])
    assert candidate["status"] == "pending"
    assert "value_verification_missing" in candidate["evidence"][-1]["reasons"]


def test_retire_only_obsolete_unconfirmed_candidates():
    repository = ModelingRepository(dsn="")
    repository.memory_candidates = {
        name: {"source_datasource_id": "a", "scope": "single_source", "status": status}
        for name, status in [("noise", "pending"), ("keep", "pending"),
                             ("accepted", "accepted"), ("rejected", "rejected")]
    }
    repository.retire_pending("a", {"keep"})
    assert set(repository.memory_candidates) == {"keep", "accepted", "rejected"}


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
