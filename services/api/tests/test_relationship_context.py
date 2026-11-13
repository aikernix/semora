import asyncio

from app.relationship_context import (REQUEST_LIMIT, field_references, literal_present, protected_fields,
    relationship_columns, table_card, table_batches, request_size, relation_batches, relation_payload)
from app.relationship_lineage import describe_view, direct_lineage, prune_shared_entity_edges


def table(name, columns):
    return {"id": "public." + name, "name": name, "kind": "TABLE", "columns": columns,
            "primary_keys": [], "unique_constraints": []}


def test_multilingual_reference_boundaries_preserve_complete_identifiers():
    assert list(field_references("通常为namespace.records.key_id；其他说明")) == [("records", "key_id")]
    assert literal_present("namespace.records", "类型只有namespace.records；")
    assert not literal_present("namespace.record", "类型namespace.records")


def test_filter_keeps_composite_identity_documented_references_and_discriminators():
    a = table("entries", [
        {"name": "entity_ref", "type": "decimal(20,3)", "description": "引用objects.opaque"},
        {"name": "period", "type": "date"},
        {"name": "category", "type": "text"},
        {"name": "payload", "type": "blob"},
        {"name": "amount", "type": "decimal(20,3)"},
        {"name": "created_at", "type": "varchar(30)"},
        {"name": "precise_number", "type": "numeric(18,0)"},
    ])
    a["primary_keys"] = [{"columns": ["entity_ref", "period"]}]
    b = table("objects", [{"name": "opaque", "type": "decimal(20,3)"}])
    protected = protected_fields([a, b])
    selected = relationship_columns(a, protected[a["id"]], {"field_roles": {"category": "discriminator"}})
    assert {c["name"] for c in selected} == {"entity_ref", "period", "category", "precise_number"}
    assert protected[b["id"]] == {"opaque"}


def test_widened_view_text_is_not_treated_as_long_form_content():
    view = table("projection", [{"name": "classification", "type": "text", "description": "类别；原类型=VARCHAR2(18)"},
                                {"name": "body", "type": "text", "description": "正文；原类型=CLOB"}])
    view["kind"] = "VIEW"
    assert [c["name"] for c in relationship_columns(view)] == ["classification"]


def test_whole_table_batches_use_ten_to_fifteen_tables_without_filling_byte_budget():
    cards = [table_card(table("t" + str(i), [{"name": "id", "type": "bigint"}]), [{"name": "id", "type": "bigint"}]) for i in range(161)]
    batches = list(table_batches(cards, "system"))
    assert len(batches) == 16
    assert all(fits and 10 <= len(group) <= 15 for group, fits in batches)
    assert all(request_size("system", {"tables": group}) <= REQUEST_LIMIT for group, _ in batches)
    assert [len(group) for group, _ in table_batches(cards[:16], "system")] == [10, 6]


def test_large_tables_split_only_when_single_table_exceeds_request_limit():
    card = table_card(table("wide", []), [{"name": "c" + str(i), "type": "varchar", "description": "说明" * 300} for i in range(90)])
    groups = list(table_batches([card], "system"))
    assert len(groups) > 1
    assert sum(len(p["columns"]) for group, _ in groups for p in group) == 90
    assert all(fits and request_size("system", {"tables": group}) <= REQUEST_LIMIT for group, fits in groups)


def test_relationship_packets_share_table_definitions_and_enforce_full_request_budget():
    packets = []
    for i in range(25):
        packets.append({"candidate_id": str(i), "source": {"table_id": "public.e" + str(i), "fields": [{"name": "ref", "description": "实体编号"}]},
                        "target": {"table_id": "public.entities", "fields": [{"name": "id", "description": "实体编号"}]}, "evidence": []})
    for group, fits in relation_batches(packets, "system"):
        payload = relation_payload(group, [p["candidate_id"] for p in group])
        assert fits and len(payload["tables"]) <= 15
        assert request_size("system", payload) <= REQUEST_LIMIT
        assert len(payload["tables"]["public.entities"]["fields"]) == 1


def test_verified_view_identity_is_not_a_business_edge_but_filtered_views_are_not_conflated():
    definition = 'SELECT t.id AS entity_id, SHA2(t.id, 256) AS generated_id FROM public.entities t'
    info = describe_view(definition, "postgres")
    c = {"source_datasource_id": "d", "target_datasource_id": "d", "source_table_id": "public.v", "target_table_id": "public.entities",
         "key_pairs": [{"source": "entity_id", "target": "id"}]}
    assert direct_lineage(c, {("d", "public.v"): info})
    c["key_pairs"] = [{"source": "generated_id", "target": "id"}]
    assert not direct_lineage(c, {("d", "public.v"): info})
    assert not describe_view('SELECT id FROM public.entities WHERE active=1', "postgres")["simple_projection"]


def test_shared_root_pruning_checks_full_mapping_and_preserves_direct_reference():
    def edge(identifier, source, target, a, b):
        return {"id": identifier, "source_table_id": source, "target_table_id": target,
                "source_datasource_id": "d", "target_datasource_id": "d", "status": "accepted",
                "evidence_grade": "strong", "evidence": [], "key_pairs": [{"source": a, "target": b}]}
    a = edge("a", "a", "root", "owner", "id")
    b = edge("b", "b", "root", "member", "id")
    sibling = edge("s", "a", "b", "owner", "member")
    direct = edge("d", "a", "b", "record", "record")
    direct["evidence"] = [{"kind": "explicit_field_reference"}]
    roles = {"root": {"role": "entity_master"}, "a": {"role": "detail"}, "b": {"role": "extension"}}
    prune_shared_entity_edges([a, b, sibling, direct], roles)
    assert sibling["status"] == "rejected"
    assert a["status"] == b["status"] == direct["status"] == "accepted"


def test_table_batch_model_can_propose_differently_named_keys_within_allowed_schema():
    from app.query_relationships import classify_tables
    values = [table("entity", [{"name": "entity_key", "type": "bigint"}]), table("entry", [{"name": "owner_ref", "type": "bigint"}])]
    class Model:
        async def complete_json(self, system, user):
            import json
            assert len(json.loads(user)["tables"]) == 2
            return {"tables": [{"table_id": t["id"], "role": "entity_master", "field_roles": {c["name"]: "identifier" for c in t["columns"]}} for t in values],
                    "relations": [{"source_table_id": "public.entry", "target_table_id": "public.entity", "key_pairs": [{"source": "owner_ref", "target": "entity_key"}], "reason": "记录引用实体"},
                                  {"source_table_id": "public.entry", "target_table_id": "public.entity", "key_pairs": [{"source": "invented", "target": "entity_key"}]}]}
    result = asyncio.run(classify_tables(values, Model()))
    assert len(result["public.entry"]["relationship_proposals"]) == 1
