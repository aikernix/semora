"""Evidence-driven batch research with finite tool use and explicit reconciliation."""
from __future__ import annotations

import asyncio
import os
import json
import logging

from .relationship_context import complete, relation_batches, relation_payload


async def audit_structural_acceptances(candidates, fetch, persist):
    """Check factual contradictions before publishing, including restored decisions."""
    rechecks = []
    for c in candidates:
        if (c.get('status') != 'accepted' or c.get('evidence_grade') in {'foreign_key', 'manual'}
                or c.get('reviewed_by') or any(e.get('kind') == 'value_profile' for e in c['evidence'])):
            continue
        try:
            evidence = await fetch(c, 'profile')
        except Exception as exc:
            evidence = {'kind': 'tool_unavailable', 'tool': 'profile', 'reason': type(exc).__name__}
        c['evidence'].append(evidence)
        if evidence.get('kind') == 'value_profile':
            from .relationship_review_policy import review_reasons
            first = next((e for e in reversed(c['evidence']) if e.get('kind') == 'model_semantic_review'), {})
            if review_reasons(c, first):
                c['evidence'] = [e for e in c['evidence'] if e.get('kind') != 'research_complete']
                rechecks.append(c)
        await persist([c])
    return rechecks


async def run(candidates, packet_for, fetch, model, persist, progress):
    from .relationship_research import VERSION, SYSTEM, REVIEW_SYSTEM, DATA_GUIDANCE, with_data_guidance, decode_decision, factual_evidence
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
        guards = sorted(c.get('condition_template',{}).get('guards',[]),key=lambda g:json.dumps(g,sort_keys=True))
        c["evidence"] = [e for e in factual_evidence(c["evidence"])
                         if not (e.get("kind") == "value_profile" and (
                             sorted(e.get('statistics',{}).get('guards_applied',[]),key=lambda g:json.dumps(g,sort_keys=True)) != guards
                             or e.get('statistics', {}).get('method') in {'indexed_directional_membership_v1','indexed_directional_membership_v2'}))]
        todo.append(c)
    todo.sort(key=lambda c: (c.get("source_datasource_id", ""), c.get("source_table_id", ""),
                             tuple(c.get("source_columns", [])), c.get("target_table_id", ""), c["id"]))
    lookup = {c["id"]: c for c in todo}
    workers = max(1, min(3, int(os.getenv("RELATIONSHIP_MODEL_CONCURRENCY", "3"))))
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
            for group, group_fits in relation_batches([packet_for(c) for c in values], system + DATA_GUIDANCE):
                current = {p["candidate_id"] for p in group}
                if not group_fits:
                    for identifier in current:
                        lookup[identifier]["evidence"].append({"kind": "research_gap", "reason": "single_relation_context_oversized"})
                    continue
                # Evidence changes start a new request; format retries reuse exactly one payload.
                for attempt in range(3 if allow_tools else 1):
                    payload = relation_payload([packet_for(lookup[i]) for i in current], sorted(current),
                                               targeted_recheck=repair,
                                               tools_allowed=allow_tools and attempt < 2)
                    has_data = any(e.get("kind") == "value_profile" for i in current for e in lookup[i]["evidence"])
                    prompt = with_data_guidance(system) if has_data else system
                    parsed_response = None
                    for retry in range(3):
                        response = None
                        try:
                            response = await complete(model, prompt, payload)
                            from .relationship_contracts import ResearchOutput, CompactResearchOutput
                            if isinstance(response, dict) and all('outcome' in d for d in response.get('decisions',[]) if isinstance(d,dict)):
                                CompactResearchOutput.model_validate(response)
                            else:
                                ResearchOutput.model_validate(response)
                            values_out = response['decisions']
                            values_by_id = {}
                            for value in values_out:
                                identifier = value.get('candidate_id')
                                parsed = decode_decision(value, lookup[identifier].get('condition_template',{}).get('guards',[])) if identifier in current else None
                                if not parsed or identifier not in current or identifier in values_by_id:
                                    raise ValueError('invalid_structured_decision')
                                values_by_id[identifier] = parsed
                            if set(values_by_id) != current:
                                raise ValueError('missing_structured_decisions')
                            parsed_response = values_by_id
                            break
                        except Exception as exc:
                            from pydantic import ValidationError
                            received = response.get('decisions', []) if isinstance(response, dict) else []
                            received_ids = [v.get('candidate_id') for v in received if isinstance(v, dict)] if isinstance(received, list) else []
                            diagnostic = {'kind': 'research_response_invalid', 'review': kind,
                                'attempt': retry + 1, 'error_type': type(exc).__name__,
                                'error_code': str(exc) if str(exc) in {'invalid_structured_decision', 'missing_structured_decisions'} else None,
                                'required_ids': sorted(current),
                                'missing_ids': sorted(current - set(i for i in received_ids if isinstance(i, str))),
                                'extra_ids': sorted(set(i for i in received_ids if isinstance(i, str)) - current),
                                'received_decisions': [{k: v.get(k) for k in ('candidate_id','outcome','kind','code','query_outcome','relationship_kind')}
                                    for v in received if isinstance(v, dict)] if isinstance(received, list) else [],
                                'validation': [{'path': list(e['loc']), 'type': e['type']} for e in exc.errors()] if isinstance(exc, ValidationError) else []}
                            logging.getLogger('semora.model').info(json.dumps({'event': 'relationship.research.invalid', **diagnostic}, ensure_ascii=False))
                            if retry == 2:
                                for identifier in current:
                                    lookup[identifier]['evidence'].append({'kind':'research_gap','reason':type(exc).__name__, 'diagnostic': diagnostic})
                    if parsed_response is None:
                        break
                    decisions.update(parsed_response)
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
            from .relationship_review_policy import structure_is_sufficient
            for c in entries:
                if structure_is_sufficient(c, first.get(c['id'], {})):
                    c['evidence'].append({'kind': 'structural_acceptance', 'reason': first[c['id']]['reason'], 'version': 'structural-v1'})
                    continue
                if first.get(c["id"], {}).get("query_outcome") == "usable" and not any(e.get("kind") == "value_profile" for e in c["evidence"]):
                    await obtain(c, "profile")
            await persist(entries)
            from .relationship_review_policy import review_reasons
            uncertain = []
            for c in entries:
                reasons = review_reasons(c, first.get(c['id'], {}))
                c['review_required'] = bool(reasons)
                c['review_reasons'] = reasons
                c['review_policy_version'] = 'ambiguity-v1'
                c['evidence'].append({'kind': 'review_policy', 'required': bool(reasons),
                                      'reasons': reasons, 'version': 'ambiguity-v1'})
                if reasons:
                    uncertain.append(c)
            second = await assess(uncertain, REVIEW_SYSTEM, 'independent_review') if uncertain else {}
            disagreement = [c for c in uncertain if c['id'] in first and c['id'] in second and
                            (first[c['id']]['query_outcome'] != second[c['id']]['query_outcome']
                             or first[c['id']]['additional_predicates'] != second[c['id']]['additional_predicates'])]
            if disagreement:
                await assess(disagreement, SYSTEM, 'model_semantic_review', repair=True, allow_tools=False)
            for c in entries:
                required = ['model_semantic_review'] + (['independent_review'] if c['review_required'] else [])
                if all(any(e.get('kind') == kind for e in c['evidence']) for kind in required) and not any(e.get('kind') in {'research_gap', 'tool_unavailable'} for e in c['evidence']):
                    c['evidence'].append({'kind': 'research_complete', 'version': VERSION})
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
