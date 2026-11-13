"""Bounded-context, resumable relationship research over a complete catalog."""
from __future__ import annotations

import asyncio
import json

VERSION = "relationship-research-v1"
SYSTEM = """角色：数据结构关系分析员。
任务：根据字段含义、约束和数据库证据逐条判断实体引用或身份映射。资料不足时请求具体资料，获得资料后继续判断。
规则：同名和值相同不能单独证明关联；同一字段可以有多个有效目标。区分引用(reference)、身份映射(identity)、副本(replica)、未知(unknown)。仅在替代目标无法区分时标记alternative_unresolved。空表表示没有数据证据，不表示关系错误。字段注释中的明确引用可以作为文档证据，但不能自行猜测关联条件或唯一性。
可请求：metadata（完整关联字段与约束）、definition（视图定义）、profile（限量键值匹配统计）。只使用给出的candidate_id；不要生成SQL或原始数据请求。重复请求不会带来新资料。
输出JSON：{"requests":[{"candidate_id":"...","kind":"profile"}],"decisions":[{"candidate_id":"...","supported":true,"target_role":"reference","alternative_unresolved":false,"confidence":0.95,"reason":"简短证据和判断"}]}。
每个候选最终都应提交判断；无法证明时supported=false并写清缺口。"""
REVIEW_SYSTEM = """角色：关系证据复核员。
任务：独立核对给出的字段映射是否被资料支持，不把分析员的结论当作事实。
检查实体含义、引用方向、替代目标、数据副本、约束和数据反证。相同字段可以引用多个目标，样本不足或空表不构成反证。不能仅凭相同名称或相同值批准关系。
输出JSON：{"decisions":[{"candidate_id":"...","supported":true,"target_role":"reference","alternative_unresolved":false,"confidence":0.95,"reason":"简短证据或具体缺口"}]}。"""


def encoded(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def batches(packets, max_bytes=24000, max_items=12):
    """Split whole relationship records, never split a composite key or fact."""
    batch = []
    for packet in packets:
        if len(encoded(packet).encode()) > max_bytes:
            yield [packet], False
            continue
        if batch and (len(batch) >= max_items or len(encoded(batch + [packet]).encode()) > max_bytes):
            yield batch, True
            batch = []
        batch.append(packet)
    if batch:
        yield batch, True


def decision(value):
    if not isinstance(value, dict):
        return None
    confidence = value.get("confidence")
    if type(value.get("supported")) is not bool or type(confidence) not in (int, float):
        return None
    if value.get("target_role") not in {"reference", "identity", "replica", "unknown"}:
        return None
    if type(value.get("alternative_unresolved")) is not bool:
        return None
    return {"supported": value["supported"], "confidence": max(0, min(1, confidence)),
            "target_role": value["target_role"], "alternative_unresolved": value["alternative_unresolved"],
            "reason": str(value.get("reason") or "")[:1000]}


async def research(candidates, packet_for, fetch, model, persist, progress):
    """All candidates are covered. Durable evidence replaces growing chat history."""
    todo = [c for c in candidates if c["evidence_grade"] not in {"foreign_key", "manual"}
            and not any(e.get("kind") == "research_complete" and e.get("version") == VERSION
                        for e in c["evidence"])]
    packets = [packet_for(c) for c in todo]
    lookup = {c["id"]: c for c in todo}
    semaphore = asyncio.Semaphore(4)
    completed = 0

    async def run_batch(initial, fits):
        nonlocal completed
        async with semaphore:
            current = {p["candidate_id"]: p for p in initial}
            entries = [lookup[key] for key in current]
            if not fits:
                for c in entries:
                    c["evidence"].append({"kind": "research_gap", "reason": "single_relation_context_oversized"})
                await persist(entries)
                return
            seen = set()
            final = {}
            failures = 0
            while True:
                payload = [packet_for(c) for c in entries]
                if len(encoded(payload).encode()) > 48000:
                    for c in entries:
                        c["evidence"].append({"kind": "research_gap", "reason": "evidence_context_oversized"})
                    break
                try:
                    response = await model.complete_json(SYSTEM, encoded({"candidates": payload}))
                    if not isinstance(response.get("decisions", []), list) or not isinstance(response.get("requests", []), list):
                        raise ValueError("invalid research response")
                except Exception as exc:
                    failures += 1
                    if failures < 2:
                        continue
                    for c in entries:
                        c["evidence"].append({"kind": "research_gap", "reason": type(exc).__name__})
                    break
                for item in response.get("decisions", []):
                    parsed = decision(item)
                    if parsed is not None and item.get("candidate_id") in current:
                        final[item["candidate_id"]] = parsed
                added = False
                for request in response.get("requests", []):
                    if not isinstance(request, dict):
                        continue
                    key = (request.get("candidate_id"), request.get("kind"))
                    if key[0] not in current or key[1] not in {"metadata", "definition", "profile"} or key in seen:
                        continue
                    seen.add(key)
                    c = lookup[key[0]]
                    try:
                        evidence = await fetch(c, key[1])
                    except Exception as exc:
                        evidence = {"kind": "tool_unavailable", "tool": key[1], "reason": type(exc).__name__}
                    c["evidence"].append(evidence)
                    added = True
                if not added:
                    break
                await persist(entries)
            # Give every supported mapping a database check even if the analyst
            # forgot to request one. Foreign keys never enter this path.
            for c in entries:
                value = final.get(c["id"])
                if value:
                    c["evidence"].append({"kind": "model_semantic_review", **value})
                    if value["supported"] and not any(e.get("kind") == "value_profile" for e in c["evidence"]):
                        try:
                            c["evidence"].append(await fetch(c, "profile"))
                        except Exception as exc:
                            c["evidence"].append({"kind": "profile_unavailable", "reason": type(exc).__name__})
            supported = [c for c in entries if final.get(c["id"], {}).get("supported")]
            for group, fits_review in batches([packet_for(c) for c in supported], max_bytes=48000):
                if not fits_review:
                    continue
                try:
                    result = await model.complete_json(REVIEW_SYSTEM, encoded({"candidates": group}))
                    values = result.get("decisions", [])
                    if not isinstance(values, list):
                        continue
                    for item in values:
                        parsed = decision(item)
                        if parsed is not None and item.get("candidate_id") in current:
                            lookup[item["candidate_id"]]["evidence"].append({"kind": "independent_review", **parsed})
                except Exception as exc:
                    for c in supported:
                        c["evidence"].append({"kind": "research_gap", "reason": type(exc).__name__})
            for c in entries:
                if c["id"] in final:
                    c["evidence"].append({"kind": "research_complete", "version": VERSION})
            await persist(entries)
            completed += len(entries)
            await progress(completed, len(todo))

    # A fixed worker pool avoids creating one live task per candidate batch.
    queue = asyncio.Queue()
    for group in batches(packets):
        queue.put_nowait(group)
    async def worker():
        while not queue.empty():
            group, fits = queue.get_nowait()
            await run_batch(group, fits)
    await asyncio.gather(*(worker() for _ in range(4)))
