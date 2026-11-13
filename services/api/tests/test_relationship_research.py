import asyncio

from app.relationship_evidence import finalize_relationships
from app.relationship_research import research, batches


def candidate(identifier):
    return {"id": identifier, "status": "pending", "evidence_grade": "medium", "confidence": .7,
            "evidence": [{"kind": "target_unique_index", "confirmed": True}]}


def verdict(identifier):
    return {"candidate_id": identifier, "supported": True, "confidence": .97,
            "target_role": "reference", "alternative_unresolved": False, "reason": "supported by evidence"}


def test_all_candidates_are_researched_and_foreign_keys_never_reenter_model_or_tools():
    import json
    values = [candidate(str(i)) for i in range(130)]
    foreign = {**candidate("foreign"), "evidence_grade": "foreign_key", "status": "accepted"}
    values.append(foreign)
    calls, fetched, saved = [], [], []

    class Model:
        async def complete_json(self, system, user):
            packets = json.loads(user)["candidates"]
            ids = [p["candidate_id"] for p in packets]
            assert "foreign" not in ids
            assert len(user.encode()) < 48000
            calls.extend(ids)
            if "可请求" in system and not all(any(e["kind"] == "value_profile" for e in p["evidence"]) for p in packets):
                return {"requests": [{"candidate_id": i, "kind": "profile"} for i in ids], "decisions": []}
            return {"decisions": [verdict(i) for i in ids]}

    async def fetch(c, kind):
        fetched.append((c["id"], kind))
        return {"kind": "value_profile", "statistics": {"source_nonnull_distinct_keys": 100, "directional_coverage": 1}}

    async def persist(entries):
        finalize_relationships(entries)
        saved.extend(c["id"] for c in entries)

    async def progress(done, total):
        assert done <= total == 130

    packet = lambda c: {"candidate_id": c["id"], "evidence": c["evidence"]}
    asyncio.run(research(values, packet, fetch, Model(), persist, progress))
    assert all(c["status"] == "accepted" for c in values)
    assert len(fetched) == 130
    assert set(saved) == {str(i) for i in range(130)}
    previous = len(calls)
    asyncio.run(research(values, packet, fetch, Model(), persist, progress))
    assert len(calls) == previous  # completed batches resume without another request


def test_documented_reference_can_confirm_without_fabricating_samples():
    c = candidate("documented")
    c["evidence"] = [{"kind": "explicit_field_reference"},
                     {"kind": "model_semantic_review", **verdict("documented")},
                     {"kind": "independent_review", **verdict("documented")}]
    finalize_relationships([c])
    assert c["status"] == "accepted"
    assert c["evidence"][-1]["proof_path"] == "documented_reference"
    c["evidence"].append({"kind": "value_profile", "statistics": {
        "source_nonnull_distinct_keys": 100, "directional_coverage": 0}})
    finalize_relationships([c])
    assert c["status"] == "pending"


def test_context_batches_keep_complete_records_and_bound_bytes():
    packets = [{"candidate_id": str(i), "description": "字" * 1000,
                "key_pairs": [{"source": "a", "target": "b"}, {"source": "c", "target": "d"}]}
               for i in range(25)]
    groups = list(batches(packets, max_bytes=12000))
    assert sum(len(group) for group, _ in groups) == 25
    assert all(fits and all(len(p["key_pairs"]) == 2 for p in group) for group, fits in groups)


def test_declared_unique_entity_reference_with_independent_semantic_evidence():
    c = candidate("structured")
    c["evidence"].extend([
        {"kind": "entity_key_name"},
        {"kind": "model_semantic_review", **verdict("structured")},
        {"kind": "independent_review", **verdict("structured")}])
    finalize_relationships([c])
    assert c["status"] == "accepted"
    assert c["evidence"][-1]["proof_path"] == "structural_semantic"
    c["evidence"].append({"kind": "value_profile", "statistics": {
        "source_nonnull_distinct_keys": 100, "directional_coverage": .2}})
    finalize_relationships([c])
    assert c["status"] == "pending"


def test_missing_structured_decisions_are_repaired_without_losing_candidate():
    class Model:
        calls = 0
        async def complete_json(self, system, user):
            self.calls += 1
            if self.calls == 1:
                return {"decisions": []}
            return {"decisions": [verdict("repair")]}
    c = candidate("repair")
    async def fetch(c, kind):
        return {"kind": "value_profile", "statistics": {"source_nonnull_distinct_keys": 100, "directional_coverage": 1}}
    async def persist(entries):
        finalize_relationships(entries)
    async def progress(done, total):
        pass
    model = Model()
    asyncio.run(research([c], lambda c: {"candidate_id": c["id"], "evidence": c["evidence"]},
                         fetch, model, persist, progress))
    assert c["status"] == "accepted"
    assert model.calls == 3
