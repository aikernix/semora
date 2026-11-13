"""Bounded-context, resumable relationship research over a complete catalog."""
from __future__ import annotations

import asyncio
import json
import os

VERSION = "query-relationship-research-v6"


def factual_evidence(evidence):
    """Internal decisions and previous model opinions are not source evidence."""
    excluded = {"source_code", "confirmation", "model_review_deferred", "research_complete",
                "model_semantic_review", "independent_review", "join_contract",
                "automatic_exclusion", "research_gap", "competition"}
    return [entry for entry in evidence if entry.get("kind") not in excluded]


def restore_checkpoint(candidate, previous, source_hash, target_hash):
    if (not previous or previous.get("source_schema_hash") != source_hash
            or previous.get("target_schema_hash") != target_hash
            or not any(e.get("kind") == "research_complete" and e.get("version") == VERSION
                       for e in previous.get("evidence", []))):
        return False
    for key in ("evidence", "status", "condition_template", "cardinality", "confidence", "evidence_grade"):
        if key in previous:
            candidate[key] = previous[key]
    return True
CONTRACT = """输出唯一JSON结构：
{"requests":[{"candidate_id":"实际ID","kind":"metadata或definition或profile"}],"decisions":[{"candidate_id":"实际ID","supported":true,"target_role":"reference","alternative_unresolved":false,"confidence":0.95,"reason":"可核验的依据","query_outcome":"usable","exclusion_code":"","missing_evidence":[],"query_role":"连接的查询用途","additional_predicates":[]}]}。
query_outcome只能是usable、exclude、needs_evidence：
- usable：完整条件可用于查询，有实体引用或有意义的跨实体身份映射。
- exclude：明确不相关、错误目标、血缘、副本或冗余连接；必须填exclusion_code。
- needs_evidence：确实无法判断；必须在missing_evidence中列出具体缺口，不能用它代替明确否定。
exclusion_code只能是meaning_mismatch、replica、lineage_only、redundant_identity、transitive_only、wrong_target；其他结论使用空字符串。
target_role只能是reference、identity、replica、unknown。confidence表示对本次结论的把握，明确否定也可高把握。不要把supported=false直接转换为needs_evidence。
additional_predicates是完整JOIN所需的附加条件：[{"side":"source或target","field":"实际字段","operator":"eq","value":"有证据的固定值"}]，没有条件则[]。不能猜字段和值；需要条件而资料缺失时needs_evidence。
每个candidate_id都要返回结论。不得新增表、字段或SQL。"""

SYSTEM = """角色：查询数据模型分析员。
任务：根据表职责、记录粒度、字段用途、约束和数据证据建立可用的SQL连接骨架。
优先判断记录到实体主表、明细到主记录、关联表到两端实体的引用；不要把共享同一主实体的扩展表两两关联。视图到底表血缘、副本及无用途的重复身份映射应排除，不因字段相似就保留。
同一表对的不同引用角色要保留。同一字段可连接多个有效目标，只有替代目标无法区分时alternative_unresolved=true。
polymorphic_reference必须包含discriminator限定。空表、缺少外键或未声明唯一性本身不是否定证据，基数必须有依据。
取样的guards_applied表示验证范围；未限定类型的全量取样不能直接否定带类型条件的连接，应按完整条件验证。
可通过requests请求metadata（关联字段及约束）、definition（视图定义）、profile（限量匹配统计）；每种资料不会重复读取。明确不相关时直接exclude，不做无意义取证。
""" + CONTRACT

REVIEW_SYSTEM = """角色：查询连接独立复核员。
任务：重新核对字段映射、查询用途、完整条件、表职责、基数和证据，不把分析员结论当事实。
明确的度量/标志与实体标识含义不符、错误目标、纯血缘、副本或冗余连接应exclude，不要写成缺证据；只有确实无法判断时才needs_evidence。
同一字段可关联多个目标；不同实体之间有用途的身份映射可以保留。空表不构成反证。检查多态引用附加条件，不批准不完整JOIN。
""" + CONTRACT


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


def decision(value, *, strict=False):
    if not isinstance(value, dict):
        return None
    confidence = value.get("confidence")
    if type(value.get("supported")) is not bool or type(confidence) not in (int, float):
        return None
    if value.get("target_role") not in {"reference", "identity", "replica", "unknown"}:
        return None
    if type(value.get("alternative_unresolved")) is not bool:
        return None
    if strict:
        outcome = value.get("query_outcome")
        from .query_relationships import EXCLUSION_CODES
        if outcome not in {"usable", "exclude", "needs_evidence"}:
            return None
        if outcome == "exclude" and value.get("exclusion_code") not in EXCLUSION_CODES:
            return None
        if outcome == "needs_evidence" and not value.get("missing_evidence"):
            return None
        if not isinstance(value.get("additional_predicates"), list):
            return None
    return {"supported": value["supported"], "confidence": max(0, min(1, confidence)),
            "target_role": value["target_role"], "alternative_unresolved": value["alternative_unresolved"],
            "reason": str(value.get("reason") or "")[:1000],
            "query_outcome": value.get("query_outcome") if value.get("query_outcome") in {"usable", "exclude", "needs_evidence"} else "usable" if value["supported"] else "needs_evidence",
            "exclusion_code": str(value.get("exclusion_code") or ""),
            "missing_evidence": value.get("missing_evidence") if isinstance(value.get("missing_evidence"), list) else [],
            "query_role": str(value.get("query_role") or "")[:300],
            "additional_predicates": value.get("additional_predicates", [])}


async def research(candidates, packet_for, fetch, model, persist, progress):
    """All candidates are covered. Durable evidence replaces growing chat history."""
    todo = [c for c in candidates if c["evidence_grade"] not in {"foreign_key", "manual"}
            and not any(e.get("kind") == "research_complete" and e.get("version") == VERSION
                        for e in c["evidence"])]
    for c in todo:
        c["evidence"] = [e for e in factual_evidence(c["evidence"])
                         if not (e.get("kind") == "value_profile"
                                 and e.get("statistics", {}).get("guards_applied"))]
        if "condition_template" in c:
            c["condition_template"]["guards"] = []
    packets = [packet_for(c) for c in todo]
    lookup = {c["id"]: c for c in todo}
    workers = max(1, min(16, int(os.getenv("RELATIONSHIP_MODEL_CONCURRENCY", "4"))))
    semaphore = asyncio.Semaphore(workers)
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
            format_repairs = 0
            while True:
                payload = [packet_for(c) for c in entries]
                if len(encoded(payload).encode()) > 48000:
                    for c in entries:
                        c["evidence"].append({"kind": "research_gap", "reason": "evidence_context_oversized"})
                    break
                try:
                    response = await model.complete_json(SYSTEM, encoded({"candidates": payload,
                        "required_decision_ids": [key for key in current if key not in final],
                        "format_repair": format_repairs > 0}))
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
                    parsed = decision(item, strict=True)
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
                    missing = [key for key in current if key not in final]
                    if missing and format_repairs < 2:
                        format_repairs += 1
                        continue
                    for key in missing:
                        lookup[key]["evidence"].append({"kind": "research_gap", "reason": "missing_valid_structured_decision"})
                    break
                await persist(entries)
            # Give every supported mapping a database check even if the analyst
            # forgot to request one. Foreign keys never enter this path.
            for c in entries:
                value = final.get(c["id"])
                if value:
                    c["evidence"].append({"kind": "model_semantic_review", **value})
                    if value["query_outcome"] == "usable" and not any(e.get("kind") == "value_profile" for e in c["evidence"]):
                        try:
                            c["evidence"].append(await fetch(c, "profile"))
                        except Exception as exc:
                            c["evidence"].append({"kind": "profile_unavailable", "reason": type(exc).__name__})
            supported = [c for c in entries if final.get(c["id"], {}).get("supported")
                         or final.get(c["id"], {}).get("query_outcome") == "exclude"]
            for group, fits_review in batches([packet_for(c) for c in supported], max_bytes=48000):
                if not fits_review:
                    continue
                allowed_ids = {p["candidate_id"] for p in group}
                reviewed_ids = set()
                for attempt in range(3):
                    try:
                        result = await model.complete_json(REVIEW_SYSTEM, encoded({
                            "candidates": group, "required_decision_ids": sorted(allowed_ids - reviewed_ids),
                            "format_repair": attempt > 0}))
                        values = result.get("decisions", [])
                        if not isinstance(values, list):
                            continue
                        for item in values:
                            parsed = decision(item, strict=True)
                            if parsed is not None and item.get("candidate_id") in allowed_ids:
                                identifier = item["candidate_id"]
                                lookup[identifier]["evidence"].append({"kind": "independent_review", **parsed})
                                reviewed_ids.add(identifier)
                        if reviewed_ids == allowed_ids:
                            break
                    except Exception as exc:
                        if attempt == 2:
                            for identifier in allowed_ids - reviewed_ids:
                                lookup[identifier]["evidence"].append({"kind": "research_gap", "reason": type(exc).__name__})
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
    await asyncio.gather(*(worker() for _ in range(workers)))
