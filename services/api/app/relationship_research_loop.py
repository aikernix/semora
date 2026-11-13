"""Evidence-driven batch research with finite tool use and explicit reconciliation."""
from __future__ import annotations

import asyncio
import os

from .relationship_context import complete, relation_batches, relation_payload


async def run(candidates, packet_for, fetch, model, persist, progress):
    from .relationship_research import VERSION, SYSTEM, REVIEW_SYSTEM, decision, factual_evidence
    from .relationship_evidence import finalize_relationships
    todo = []
    for c in candidates:
        if c["evidence_grade"] in {"foreign_key", "manual"} or c.get("reviewed_by"):
            continue
        if any(e.get("kind") == "research_complete" and e.get("version") == VERSION for e in c["evidence"]):
            continue
        if any(e.get("kind") in {"verified_direct_lineage", "verified_non_relation_field"} for e in c["evidence"]):
            finalize_relationships([c])
            c["evidence"].append({"kind": "research_complete", "version": VERSION})
            await persist([c])
            continue
        c["evidence"] = [e for e in factual_evidence(c["evidence"])
                         if not (e.get("kind") == "value_profile" and e.get("statistics", {}).get("guards_applied"))]
        if "condition_template" in c:
            c["condition_template"]["guards"] = []
        todo.append(c)
    todo.sort(key=lambda c: (c.get("source_datasource_id", ""), c.get("source_table_id", ""),
                             tuple(c.get("source_columns", [])), c.get("target_table_id", ""), c["id"]))
    lookup = {c["id"]: c for c in todo}
    workers = max(1, min(16, int(os.getenv("RELATIONSHIP_MODEL_CONCURRENCY", "4"))))
    done = 0

    async def batch(initial, fits):
        nonlocal done
        entries = [lookup[p["candidate_id"]] for p in initial]
        seen = set()
        async def obtain(c, kind):
            if kind not in {"definition", "profile"} or (c["id"], kind) in seen:
                return False
            seen.add((c["id"], kind))
            try:
                evidence = await fetch(c, kind)
            except Exception as exc:
                evidence = {"kind": "tool_unavailable", "tool": kind, "reason": type(exc).__name__}
            c["evidence"].append(evidence)
            return True

        async def assess(values, system, kind, *, repair=False, allow_tools=True):
            decisions = {}
            for group, group_fits in relation_batches([packet_for(c) for c in values], system):
                current = {p["candidate_id"] for p in group}
                if not group_fits:
                    for identifier in current:
                        lookup[identifier]["evidence"].append({"kind": "research_gap", "reason": "single_relation_context_oversized"})
                    continue
                # Two evidence waves plus format repair/finalization. No growing chat history.
                for attempt in range(5 if allow_tools else 2):
                    payload = relation_payload([packet_for(lookup[i]) for i in current], sorted(current),
                                               format_repair=attempt > 0, targeted_recheck=repair,
                                               tools_allowed=allow_tools and attempt < 2)
                    try:
                        response = await complete(model, system, payload)
                        values_out = response.get("decisions", [])
                        if not isinstance(values_out, list):
                            raise ValueError("invalid_decisions")
                    except Exception as exc:
                        if attempt < 1:
                            continue
                        for identifier in current:
                            lookup[identifier]["evidence"].append({"kind": "research_gap", "reason": type(exc).__name__})
                        break
                    for value in values_out:
                        parsed = decision(value, strict=True)
                        if parsed and value.get("candidate_id") in current:
                            decisions[value["candidate_id"]] = parsed
                    added = False
                    requests = response.get("requests", [])
                    if allow_tools and attempt < 2 and isinstance(requests, list):
                        for request in requests:
                            if isinstance(request, dict) and request.get("candidate_id") in current:
                                added = await obtain(lookup[request["candidate_id"]], request.get("kind")) or added
                    if not added and current <= decisions.keys():
                        break
                    if added:
                        # A decision made before new evidence must be revisited.
                        for identifier in current:
                            decisions.pop(identifier, None)
                for identifier in current:
                    if identifier in decisions:
                        lookup[identifier]["evidence"].append({"kind": kind, **decisions[identifier]})
                    else:
                        lookup[identifier]["evidence"].append({"kind": "research_gap", "reason": "missing_valid_structured_decision"})
            return decisions

        if fits:
            first = await assess(entries, SYSTEM, "model_semantic_review")
            for c in entries:
                if first.get(c["id"], {}).get("query_outcome") == "usable" and not any(e.get("kind") == "value_profile" for e in c["evidence"]):
                    await obtain(c, "profile")
            await persist(entries)
            # Uncertain and negative proposals also receive independent assessment.
            second = await assess(entries, REVIEW_SYSTEM, "independent_review")
            disagreement = [c for c in entries if c["id"] in first and c["id"] in second and
                            (first[c["id"]]["query_outcome"] != second[c["id"]]["query_outcome"]
                             or first[c["id"]]["alternative_unresolved"] != second[c["id"]]["alternative_unresolved"]
                             or first[c["id"]]["additional_predicates"] != second[c["id"]]["additional_predicates"])]
            if disagreement:
                await assess(disagreement, SYSTEM, "model_semantic_review", repair=True, allow_tools=False)
            for c in entries:
                if any(e.get("kind") == "independent_review" for e in c["evidence"]):
                    c["evidence"].append({"kind": "research_complete", "version": VERSION})
        else:
            for c in entries:
                c["evidence"].append({"kind": "research_gap", "reason": "single_relation_context_oversized"})
        await persist(entries)
        done += len(entries)
        await progress(done, len(todo))

    queue = asyncio.Queue()
    for group in relation_batches([packet_for(c) for c in todo], SYSTEM):
        queue.put_nowait(group)
    async def worker():
        while not queue.empty():
            group, fits = queue.get_nowait()
            await batch(group, fits)
    await asyncio.gather(*(worker() for _ in range(workers)))
