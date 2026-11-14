import asyncio

from app.relationship_evidence import finalize_relationships
from app.relationship_research import research, batches


def test_model_evidence_excludes_internal_decisions_and_old_model_opinions():
    from app.relationship_research import factual_evidence
    facts = [{"kind": "explicit_field_reference"}, {"kind": "target_unique_index", "confirmed": True},
             {"kind": "value_profile", "statistics": {"directional_coverage": .7}}]
    opinions = [{"kind": kind} for kind in ("join_contract", "confirmation", "automatic_exclusion",
                "model_semantic_review", "independent_review", "competition", "research_complete")]
    assert factual_evidence(facts + opinions) == facts


def test_resume_restores_complete_join_and_checks_both_schema_hashes():
    from app.relationship_research import restore_checkpoint, VERSION
    original = {"source_schema_hash": "a", "target_schema_hash": "b", "status": "accepted",
                "condition_template": {"guards": [{"side": "source", "field": "kind", "operator": "eq", "value": "entity"}]},
                "evidence": [{"kind": "research_complete", "version": VERSION}]}
    restored = {}
    assert not restore_checkpoint(restored, original, "a", "changed")
    assert not restored
    assert restore_checkpoint(restored, original, "a", "b")
    assert restored["condition_template"] == original["condition_template"]


def candidate(identifier):
    return {"id": identifier, "status": "pending", "evidence_grade": "medium", "confidence": .7,
            "evidence": [{"kind": "target_unique_index", "confirmed": True}]}


def verdict(identifier):
    return {"candidate_id": identifier, "supported": True, "confidence": .97,
            "structure_sufficient": True,
            "target_role": "reference", "alternative_unresolved": False, "reason": "supported by evidence",
            "key_domain": "same", "relationship_kind": "entity_reference", "coverage_expectation": "complete",
            "query_outcome": "usable", "exclusion_code": "", "missing_evidence": [],
            "query_role": "entity reference", "additional_predicates": []}


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
            from app.relationship_context import request_size, REQUEST_LIMIT
            assert request_size(system, json.loads(user)) <= REQUEST_LIMIT
            calls.extend(ids)
            if "通过requests" in system and not all(any(e["kind"] == "value_profile" for e in p["evidence"]) for p in packets):
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
    async def execute():
        await research(values, packet, fetch, Model(), persist, progress)
        from app.relationship_research_loop import audit_structural_acceptances
        await audit_structural_acceptances(values, fetch, persist)
    asyncio.run(execute())
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
    assert c["status"] == "unresolved"


def test_restored_structural_acceptance_is_audited_before_publication():
    from app.relationship_research_loop import audit_structural_acceptances
    c = candidate('restored')
    c['evidence'].extend([{'kind': 'entity_key_name'},
        {'kind': 'model_semantic_review', **verdict(c['id'])},
        {'kind': 'review_policy', 'required': False}])
    finalize_relationships([c])
    assert c['status'] == 'accepted'
    foreign = {**candidate('foreign'), 'status': 'accepted', 'evidence_grade': 'foreign_key'}
    calls = []
    async def fetch(value, kind):
        calls.append((value['id'], kind))
        return {'kind': 'value_profile', 'statistics': {
            'source_nonnull_distinct_keys': 19, 'source_exhausted': True, 'directional_coverage': 1 / 19}}
    async def persist(values):
        finalize_relationships(values)
    asyncio.run(audit_structural_acceptances([c, foreign], fetch, persist))
    assert c['status'] == 'unresolved'
    assert foreign['status'] == 'accepted'
    asyncio.run(audit_structural_acceptances([c, foreign], fetch, persist))
    assert calls == [('restored', 'profile')]


def test_failed_acceptance_audit_is_explicitly_incomplete():
    from app.relationship_research_loop import audit_structural_acceptances
    c = {**candidate('failed-audit'), 'status': 'accepted'}
    async def fetch(*args):
        raise TimeoutError()
    async def persist(values):
        pass
    asyncio.run(audit_structural_acceptances([c], fetch, persist))
    assert c['evidence'][-1] == {'kind': 'tool_unavailable', 'tool': 'profile', 'reason': 'TimeoutError'}


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
    assert c["status"] == "unresolved"


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
    assert model.calls == 2


def test_uncertain_proposal_gets_independent_review_and_one_bounded_reconciliation():
    import json
    c = candidate("uncertain")
    calls = []
    class Model:
        async def complete_json(self, system, user):
            calls.append(json.loads(user))
            v = verdict("uncertain")
            if len(calls) == 1:
                v.update(supported=False, query_outcome="needs_evidence", key_domain="unknown", missing_evidence=["标识体系"])
            else:
                v.update(supported=False, query_outcome="exclude", key_domain="different", exclusion_code="meaning_mismatch")
            return {"decisions": [v]}
    async def fetch(*args):
        raise AssertionError("明确不同的标识不需要取样")
    async def persist(entries):
        finalize_relationships(entries)
    async def progress(*args):
        pass
    asyncio.run(research([c], lambda c: {"candidate_id": c["id"], "evidence": c["evidence"]}, fetch, Model(), persist, progress))
    assert len(calls) == 3
    assert c["status"] == "rejected"


def test_unknown_identifier_domain_cannot_pass_on_confidence_alone():
    c = candidate("domain")
    v = {**verdict("domain"), "confidence": 1, "key_domain": "unknown"}
    c["evidence"].extend([{"kind": "entity_key_name"}, {"kind": "model_semantic_review", **v}, {"kind": "independent_review", **v}])
    finalize_relationships([c])
    assert c["status"] == "unresolved"
    assert "identifier_domain_unverified" in c["evidence"][-1]["reasons"]


def test_shared_entity_exclusion_has_one_contract_and_is_not_a_format_failure():
    from app.relationship_contracts import ResearchOutput
    from app.relationship_research import decision
    value = {**verdict('shared'), 'supported': False, 'query_outcome': 'exclude',
             'target_role': 'unknown', 'relationship_kind': 'shared_entity', 'exclusion_code': 'shared_entity'}
    ResearchOutput.model_validate({'decisions': [value]})
    assert decision(value, strict=True) is not None
    c = candidate('shared')
    c['evidence'].extend([{'kind': 'model_semantic_review', **value},
                           {'kind': 'review_policy', 'required': False}])
    finalize_relationships([c])
    assert c['status'] == 'rejected'
    assert c['evidence'][-1]['reasons'] == ['shared_entity']


def test_complete_small_source_can_validate_many_rows_to_one_entity():
    c = candidate("small")
    c["evidence"].extend([{"kind": "model_semantic_review", **verdict("small")},
        {"kind": "independent_review", **verdict("small")}, {"kind": "value_profile", "statistics": {
            "source_nonnull_distinct_keys": 50, "source_exhausted": True, "directional_coverage": 1}}])
    finalize_relationships([c])
    assert c["status"] == "accepted"
    assert c["evidence"][-1]["proof_path"] == "verified_values"


def test_clear_structural_reference_skips_profile_and_independent_review():
    c=candidate('clear')
    fetched=[];calls=[]
    class Model:
        async def complete_json(self,system,user):
            calls.append(system)
            return {'decisions':[{**verdict('clear'),'structure_sufficient':True,'reason':'表用途与所属实体编号一致，目标为唯一标识'}]}
    async def fetch(c,kind):
        fetched.append(kind);raise AssertionError('clear reference should not sample')
    async def persist(entries):finalize_relationships(entries)
    async def progress(done,total):pass
    asyncio.run(research([c],lambda c:{'candidate_id':c['id'],'evidence':c['evidence']},fetch,Model(),persist,progress))
    assert c['status']=='accepted'
    assert fetched==[] and len(calls)==1
    assert c['review_required'] is False


def test_structural_shortcut_rejects_ambiguity_incomplete_conditions_and_conflicts():
    from app.relationship_review_policy import structure_is_sufficient
    c=candidate('clear')
    d={**verdict('clear'),'structure_sufficient':True,'reason':'引用用途一致'}
    assert structure_is_sufficient(c,d)
    assert not structure_is_sufficient(c,{**d,'alternative_unresolved':True})
    assert not structure_is_sufficient(c,{**d,'key_domain':'unknown'})
    assert not structure_is_sufficient(c,{**d,'additional_predicates':[{'field':'kind','value':1}]})
    assert not structure_is_sufficient({**c,'evidence':[]},d)
    c['evidence'].append({'kind':'value_profile','statistics':{'source_nonnull_distinct_keys':10,'directional_coverage':0}})
    assert not structure_is_sufficient(c,d)


def test_evidence_review_three_attempts_preserve_original_prompt_and_input():
    c=candidate('retry_contract')
    calls=[]
    class Model:
        async def complete_json(self,system,user):
            calls.append((system,user))
            if len(calls)<3:return {'decisions':[]}
            return {'decisions':[{**verdict('retry_contract'),'structure_sufficient':True}]}
    async def fetch(*args):raise AssertionError('no sampling for clear reference')
    async def persist(entries):finalize_relationships(entries)
    async def progress(*args):pass
    asyncio.run(research([c],lambda c:{'candidate_id':c['id'],'evidence':c['evidence']},fetch,Model(),persist,progress))
    assert c['status']=='accepted'
    assert len(calls)==3
    assert calls[0]==calls[1]==calls[2]


def test_competing_targets_do_not_skip_data_verification():
    from app.relationship_review_policy import structure_is_sufficient
    c=candidate('competing')
    c.update(source_datasource_id='ds',source_table_id='events',source_columns=['owner_id'],target_datasource_id='ds',target_table_id='entities',target_columns=['id'])
    c['other_proposals']=[{'id':'other','source_datasource_id':'ds','source_table_id':'events','source_columns':['owner_id'],'target_datasource_id':'ds','target_table_id':'other_entities','target_columns':['id']}]
    assert not structure_is_sufficient(c,{**verdict('competing'),'structure_sufficient':True})


def test_partial_coverage_cannot_be_explained_by_two_model_opinions_alone():
    c=candidate('partial')
    d={**verdict('partial'),'coverage_expectation':'partial'}
    c['evidence'] += [{'kind':'raw_schema_context','source_description':'所属实体编号','target_description':'实体编号'},
        {'kind':'model_semantic_review',**d},{'kind':'independent_review',**d},
        {'kind':'value_profile','statistics':{'source_nonnull_distinct_keys':3,'source_exhausted':True,'directional_coverage':.333333}}]
    finalize_relationships([c])
    assert c['status']=='unresolved'
