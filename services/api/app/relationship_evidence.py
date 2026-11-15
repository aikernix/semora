"""Catalog-local relationship recall and evidence-based final decisions."""
from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from .relationship_context import field_references


def meaning_tokens(column):
    text = str(column.get("description") or column.get("comment") or "").lower()
    text = re.split(r"[;；\n]", text, maxsplit=1)[0]
    words = set(re.findall(r"[a-z][a-z0-9_]*", text))
    for phrase in re.findall(r"[\u4e00-\u9fff]+", text):
        words.update(phrase[i:i + 2] for i in range(len(phrase) - 1))
    return words


def recall_field_pairs(sources, targets):
    """Bounded field-level recall; common catalog terms carry no evidence."""
    # Explicit references can point to non-unique fields too. Their cardinality
    # and proof path are resolved later; don't lose them in a key-only registry.
    target_fields = {(str(t.get("name", "")).lower(), c["name"].lower()): (t, c)
                     for t in targets for c in t.get("columns") or []}
    for source in sources:
        for column in source.get("columns") or []:
            text = str(column.get("description") or column.get("comment") or "").lower()
            for ref in field_references(text):
                if ref in target_fields:
                    target, field = target_fields[ref]
                    yield source, column, target, field, True
    keys, postings = [], defaultdict(list)
    for table in targets:
        columns = {c["name"]: c for c in table.get("columns") or []}
        constraints = (table.get("primary_keys") or []) + (table.get("unique_constraints") or [])
        if not constraints and str(table.get("kind", "")).upper() == "VIEW":
            # Views can be recalled by an explicit reference or entity-key name;
            # their uniqueness remains unproven and cannot be auto-confirmed.
            constraints = [{"columns": [name]} for name in columns]
        for constraint in constraints:
            names = constraint.get("columns") or []
            if len(names) != 1 or names[0] not in columns:
                continue
            entry = (table, columns[names[0]], meaning_tokens(columns[names[0]]))
            keys.append(entry)
    references, named_keys = defaultdict(list), defaultdict(list)
    for index, (target, field, _) in enumerate(keys):
        references[(str(target.get("name", "")).lower(), field["name"].lower())].append(index)
        field_name = re.sub(r"[^a-z0-9]", "", field["name"].lower())
        for part in re.findall(r"[a-z0-9]+", str(target.get("name", "")).lower()):
            if len(part) >= 3 and field_name:
                named_keys[part + field_name].append(index)
    frequency = Counter(token for _, _, tokens in keys for token in tokens)
    for index, (_, _, tokens) in enumerate(keys):
        for token in tokens:
            if frequency[token] < max(4, len(keys) * .15):
                postings[token].append(index)
    for source in sources:
        for column in source.get("columns") or []:
            tokens = meaning_tokens(column)
            scores = defaultdict(float)
            for token in tokens:
                for index in postings.get(token, []):
                    scores[index] += math.log(1 + len(keys) / frequency[token])
            # Explicit table.column references use the full comment, including
            # clauses after the field's descriptive title.
            text = str(column.get("description") or column.get("comment") or "").lower()
            explicit = set()
            named = set()
            for table_name, field_name in field_references(text):
                explicit.update(references.get((table_name, field_name), []))
            source_name = re.sub(r"[^a-z0-9]", "", column["name"].lower())
            named.update(named_keys.get(source_name, []) if source_name else [])
            for index in explicit:
                scores[index] += 100
            for index in named:
                target = keys[index][0]
                parts = re.findall(r"[a-z0-9]+", str(target.get("name", "")).lower())
                scores[index] += 20 / max(1, len(parts))
            ranked = []
            for index, score in scores.items():
                target, field, target_tokens = keys[index]
                if source["id"] == target["id"] and source.get("_source_id") == target.get("_source_id"):
                    continue
                overlap = tokens & target_tokens & postings.keys()
                # One meaningful bigram (e.g. an entity name) is sufficient for
                # recall, but never sufficient for automatic confirmation.
                if index not in explicit and index not in named and not overlap:
                    continue
                if index not in explicit and index not in named:
                    table_tokens = meaning_tokens({"description": target.get("description") or target.get("comment")})
                    if not overlap & table_tokens:
                        continue
                if not (target.get("primary_keys") or target.get("unique_constraints")) and index not in explicit and index not in named:
                    continue
                ranked.append((score, target["id"], field["name"], index))
            ranked.sort(key=lambda value: (-value[0], value[1], value[2]))
            # Broad tied collisions remain unresolved discovery hints. Do not
            # choose arbitrary targets merely to satisfy a top-k limit.
            if len(ranked) > 3 and ranked[0][0] - ranked[3][0] < .01 and not explicit:
                continue
            for _, _, _, index in ranked[:3]:
                target, field, _ = keys[index]
                yield source, column, target, field, index in explicit


TECHNICAL_PENDING = {'research_gap', 'tool_unavailable', 'profile_unavailable'}


def technically_pending(candidate):
    return any(e.get('kind') in TECHNICAL_PENDING for e in candidate.get('evidence', []))


def finalize_relationships(candidates, *, cross_source=False):
    """Confirmation runs after both database and semantic evidence are present."""
    for candidate in candidates:
        if candidate["evidence_grade"] in {"foreign_key", "manual"} or candidate.get("reviewed_by"):
            continue
        evidence = candidate["evidence"]
        # A temporary tool/model failure is not evidence against a relationship.
        # Keep a previously accepted relation, but never promote a new pending candidate.
        if technically_pending(candidate):
            candidate['research_pending'] = True
            if candidate.get('status') != 'accepted':
                candidate['status'] = 'unresolved'
            continue
        candidate['research_pending'] = False
        profile = next((e.get("statistics", {}) for e in reversed(evidence)
                        if e.get("kind") == "value_profile"), {})
        review = next((e for e in reversed(evidence)
                       if e.get("kind") == "model_semantic_review"), {})
        independent = next((e for e in reversed(evidence)
                           if e.get("kind") == "independent_review"), {})
        policy = next((e for e in reversed(evidence) if e.get("kind") == "review_policy"), {})
        if not independent and policy.get("required") is False:
            independent = review  # One review is sufficient only under the explicit versioned policy.
        from .query_relationships import triage
        excluded = ("lineage_only" if any(e.get("kind") == "verified_direct_lineage" for e in evidence)
                    else "non_relationship_field" if any(e.get("kind") == "verified_non_relation_field" for e in evidence)
                    else "shared_entity_path" if any(e.get("kind") == "verified_shared_entity_path" for e in evidence)
                    else triage(candidate, review, independent))
        if excluded:
            candidate["status"] = "rejected"
            candidate["evidence"] = [e for e in evidence if e.get("kind") not in {"confirmation", "automatic_exclusion"}]
            candidate["evidence"].append({"kind": "automatic_exclusion", "code": excluded,
                                           "reason": "已按物理结构、字段用途或完整关联路径核验为无关或冗余连接" if excluded in {"lineage_only", "shared_entity_path", "non_relationship_field"} else independent.get("reason")})
            candidate["evidence"].append({"kind": "confirmation", "decision": "rejected", "reasons": [excluded]})
            continue
        documented = any(e.get("kind") == "explicit_field_reference" for e in evidence)
        samples = profile.get("source_nonnull_distinct_keys", 0)
        coverage = profile.get("directional_coverage", 0)
        unique = any(e.get("kind") in {"target_unique_index", "verified_target_uniqueness"} and e.get("confirmed") is True for e in evidence)
        sufficient = samples >= (200 if cross_source else 100) or (profile.get("source_exhausted") is True and samples >= 1)
        data_path = unique and sufficient and coverage >= (.99 if cross_source else .98)
        # A concrete documented field reference is a separate proof path.
        # Empty data is unknown; observed contradictory data still blocks it.
        partial_claim = all(e.get("coverage_expectation") == "partial" for e in (review, independent))
        partial_scope = bool(candidate.get("condition_template", {}).get("guards")) or documented or any(e.get("kind") == "verified_partial_scope" for e in evidence)
        partial = partial_claim and partial_scope
        # Referential completeness is data quality, not schema-reference existence.
        # Positive matches require independently explained direct-reference semantics.
        semantic_partial = unique and bool(profile) and (profile.get('matched_keys', 0) > 0 or all(
            d.get('coverage_assessment') == 'incomplete_reference_data' for d in (review, independent))) and all(
            d.get('supported') is True and d.get('structure_sufficient') is True
            and d.get('query_outcome') == 'usable' and d.get('key_domain') == 'same'
            and d.get('relationship_kind') in {'entity_reference', 'direct_record_reference'}
            and d.get('target_role') == 'reference' and not d.get('alternative_unresolved')
            and not d.get('missing_evidence') and str(d.get('reason', '')).strip()
            for d in (review, next((e for e in reversed(evidence) if e.get('kind') == 'independent_review'), {})))
        partial = partial or semantic_partial
        contradiction = samples > 0 and coverage < .98 and not (semantic_partial or (partial and coverage > 0))
        documented_path = documented and not contradiction
        semantic_structure = any(e.get("kind") == "raw_schema_context" and e.get("source_description", "").strip() and e.get("target_description", "").strip() for e in evidence) or any(e.get("kind") in {"entity_key_name", "matching_identifier_definition"} for e in evidence) or (
            any(e.get("kind") == "same_name" for e in evidence)
            and any(e.get("kind") == "comment_semantics" for e in evidence))
        structural_path = (unique and semantic_structure
                           and review.get("supported") is True
                           and independent.get("supported") is True
                           and not contradiction)
        non_data_path = documented_path or structural_path or (any(e.get("kind") == "structural_acceptance" for e in evidence) and not contradiction)
        reasons = []
        if review.get("query_outcome") not in (None, "usable") or independent.get("query_outcome") not in (None, "usable"):
            reasons.append("query_use_not_confirmed")
        join_contract = next((e for e in reversed(evidence) if e.get("kind") == "join_contract"), None)
        if join_contract and not join_contract.get("valid"):
            reasons.append("join_predicates_unresolved")
        if any(e.get("key_domain", "same") != "same" for e in (review, independent)):
            reasons.append("identifier_domain_unverified")
        if any(e.get('coverage_assessment') == 'identifier_conflict' for e in (review, independent)):
            reasons.append('identifier_domain_conflict')
        if any(e.get('relationship_kind') in {'unknown', 'shared_entity', 'lineage'} for e in (review, independent)):
            reasons.append('direct_reference_unverified')
        if not unique and not non_data_path:
            reasons.append("target_uniqueness_unverified")
        if not profile and not non_data_path:
            reasons.append("value_verification_missing")
        elif not sufficient and not non_data_path:
            reasons.append("insufficient_distinct_samples")
        elif coverage < (.99 if cross_source else .98) and not non_data_path:
            reasons.append("insufficient_value_coverage")
        if not review.get("supported"):
            reasons.append("semantic_evidence_insufficient")
        if review.get("target_role") not in {"reference", "identity"}:
            reasons.append("target_role_unresolved")
        if review.get("alternative_unresolved"):
            reasons.append("alternative_target_unresolved")
        if review.get("target_role") == "replica":
            reasons.append("replica_is_not_business_reference")
        if (not independent.get("supported")
                or independent.get("target_role") not in {"reference", "identity"}
                or independent.get("alternative_unresolved") is not False):
            reasons.append("independent_review_incomplete")
        # A shared source field does not imply targets are mutually exclusive.
        candidate["status"] = "unresolved" if reasons else "accepted"
        if not reasons:
            candidate["evidence_grade"] = "strong"
        candidate["evidence"] = [e for e in evidence if e.get("kind") != "confirmation"]
        candidate["evidence"].append({"kind": "confirmation", "reasons": reasons,
                                      "data_quality": "partial_reference_coverage" if semantic_partial and coverage < .98 else "not_applicable" if not profile else "sampled",
                                      "proof_path": "documented_reference" if documented_path else "structural_semantic" if structural_path else "structural_reference" if any(e.get("kind") == "structural_acceptance" for e in evidence) and not contradiction else "verified_values" if data_path else "insufficient",
                                      "decision": candidate["status"]})


def coverage_report(tables, candidates):
    """Whole-catalog checks run after batching; batches are not isolated schemas."""
    by_name = {str(t.get("name", "")).lower(): t for t in tables}
    mappings = {(c["source_table_id"], tuple(c["source_columns"]), c["target_table_id"], tuple(c["target_columns"]))
                for c in candidates}
    missing, unavailable = [], []
    references = 0
    for table in tables:
        for column in table.get("columns") or []:
            text = str(column.get("description") or column.get("comment") or "").lower()
            for name, field in field_references(text):
                references += 1
                target = by_name.get(name)
                target_column = next((c for c in (target or {}).get("columns", []) if c["name"].lower() == field), None)
                issue = {"source_table": table["id"], "source_field": column["name"], "reference": name + "." + field}
                if not target_column:
                    unavailable.append(issue)
                elif (table["id"], (column["name"],), target["id"], (target_column["name"],)) not in mappings:
                    missing.append(issue)
    return {"tables": len(tables), "explicit_references": references,
            "missing_reference_candidates": missing, "unavailable_reference_targets": unavailable,
            "accepted": sum(c["status"] == "accepted" for c in candidates),
            "researched": sum(any(e.get("kind") == "research_complete" for e in c["evidence"]) for c in candidates),
            "foreign_keys": sum(c["evidence_grade"] == "foreign_key" for c in candidates)}
