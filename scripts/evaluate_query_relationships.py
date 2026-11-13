"""Read-only offline reference comparison; never used by relationship generation.

Input JSON on stdin: {ontology: {objects, relations}, candidates: [...]}.
"""
import collections
import json
import sys

import sqlglot
from sqlglot import exp


def compare(payload):
    ontology = payload["ontology"]
    objects = {o["name"]: o for o in ontology["objects"]}
    def physical(column):
        obj = objects[column.table]
        fields = {p["name"]: p["column"] for p in obj["properties"]}
        return obj["table"], fields[column.name]

    def reference_key(relation):
        ast = sqlglot.parse_one(relation["condition"], read="mysql")
        pairs, guards = [], []
        for equality in ast.find_all(exp.EQ):
            left, right = equality.this, equality.expression
            if isinstance(left, exp.Column) and isinstance(right, exp.Column):
                pairs.append(tuple(sorted((physical(left), physical(right)))))
            elif isinstance(left, exp.Column) and isinstance(right, exp.Literal):
                guards.append((*physical(left), str(right.this)))
        return tuple(sorted(pairs)), tuple(sorted(guards))

    def candidate_key(c):
        pairs = tuple(sorted(tuple(sorted(((c["source_table_id"], p["source"]),
                                          (c["target_table_id"], p["target"])))) for p in c["key_pairs"]))
        guards = tuple(sorted((c[g["side"] + "_table_id"], g["field"], str(g["value"]))
                              for g in c.get("condition_template", {}).get("guards", [])))
        return pairs, guards

    active = {candidate_key(c): c for c in payload["candidates"] if c["status"] == "accepted"}
    all_pairs = collections.defaultdict(list)
    for c in payload["candidates"]:
        all_pairs[candidate_key(c)[0]].append(c)
    counts = collections.Counter()
    misses, partials = [], []
    reference_keys = set()
    for relation in ontology["relations"]:
        key = reference_key(relation)
        reference_keys.add(key)
        if key in active:
            counts["exact_active"] += 1
        elif any(c["status"] == "accepted" for c in all_pairs[key[0]]):
            counts["active_but_predicates_differ"] += 1
            partials.append({"reference": relation["name"], "condition": relation["condition"]})
        else:
            status = "pending" if any(c["status"] == "pending" for c in all_pairs[key[0]]) else "excluded" if all_pairs[key[0]] else "not_recalled"
            counts[status] += 1
            misses.append({"reference": relation["name"], "status": status, "condition": relation["condition"]})
    exclusions = collections.Counter(e.get("code", "unknown") for c in payload["candidates"]
        if c["status"] == "rejected" for e in c["evidence"] if e.get("kind") == "automatic_exclusion")
    return {"reference_count": len(ontology["relations"]), "statuses": dict(collections.Counter(c["status"] for c in payload["candidates"])),
            "coverage": dict(counts), "exact_coverage": round(counts["exact_active"] / max(1, len(ontology["relations"])), 4),
            "additional_active_not_in_reference": len(set(active) - reference_keys),
            "exclusions": dict(exclusions), "missing": misses, "predicate_differences": partials,
            "note": "Additional edges are not automatically false positives; reference relations are source-backed, not production ground truth."}


if __name__ == "__main__":
    print(json.dumps(compare(json.load(sys.stdin)), ensure_ascii=False, indent=2))
