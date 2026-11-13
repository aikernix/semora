"""Catalog-local relationship recall and evidence-based final decisions."""
from __future__ import annotations

import math
import re
from collections import Counter, defaultdict


def meaning_tokens(column):
    text = str(column.get("description") or column.get("comment") or "").lower()
    text = re.split(r"[;；\n]", text, maxsplit=1)[0]
    words = set(re.findall(r"[a-z][a-z0-9_]*", text))
    for phrase in re.findall(r"[\u4e00-\u9fff]+", text):
        words.update(phrase[i:i + 2] for i in range(len(phrase) - 1))
    return words


def recall_field_pairs(sources, targets):
    """Bounded field-level recall; common catalog terms carry no evidence."""
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
            for table_name, field_name in re.findall(r"(?<![\w])([a-z_][a-z0-9_]*)\.([a-z_][a-z0-9_]*)(?![\w])", text):
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


def finalize_relationships(candidates, *, cross_source=False):
    """Confirmation runs after both database and semantic evidence are present."""
    for candidate in candidates:
        if candidate["evidence_grade"] in {"foreign_key", "manual"}:
            continue
        evidence = candidate["evidence"]
        profile = next((e.get("statistics", {}) for e in reversed(evidence)
                        if e.get("kind") == "value_profile"), {})
        review = next((e for e in reversed(evidence)
                       if e.get("kind") == "model_semantic_review"), {})
        reasons = []
        if not any(e.get("kind") == "target_unique_index" and e.get("confirmed") is True for e in evidence):
            reasons.append("target_uniqueness_unverified")
        if not profile:
            reasons.append("value_verification_missing")
        elif profile.get("source_nonnull_distinct_keys", 0) < (200 if cross_source else 100):
            reasons.append("insufficient_distinct_samples")
        elif profile.get("directional_coverage", 0) < (.99 if cross_source else .98):
            reasons.append("insufficient_value_coverage")
        if not review.get("supported") or review.get("confidence", 0) < .9:
            reasons.append("semantic_evidence_insufficient")
        if review.get("target_role") not in {"reference", "identity"}:
            reasons.append("target_role_unresolved")
        if review.get("alternative_unresolved"):
            reasons.append("alternative_target_unresolved")
        if review.get("target_role") == "replica":
            reasons.append("replica_is_not_business_reference")
        # A shared source field does not imply targets are mutually exclusive.
        candidate["status"] = "pending" if reasons else "accepted"
        if not reasons:
            candidate["evidence_grade"] = "strong"
            candidate["confidence"] = min(review["confidence"], profile["directional_coverage"])
        candidate["evidence"] = [e for e in evidence if e.get("kind") != "confirmation"]
        candidate["evidence"].append({"kind": "confirmation", "reasons": reasons,
                                      "decision": candidate["status"]})
