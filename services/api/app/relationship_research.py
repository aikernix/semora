"""Bounded-context, resumable relationship research over a complete catalog."""
from __future__ import annotations

import json

VERSION = "query-relationship-research-v7"


def factual_evidence(evidence):
    """Internal decisions and previous model opinions are not source evidence."""
    excluded = {"source_code", "confirmation", "model_review_deferred", "research_complete",
                "model_semantic_review", "independent_review", "join_contract",
                "automatic_exclusion", "research_gap", "competition", "model_table_proposal"}
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
    contract = next((e for e in reversed(previous.get("evidence", []))
                     if e.get("kind") == "join_contract"), None)
    if contract and contract.get("valid") and "guards" in contract:
        candidate["condition_template"] = {**candidate.get("condition_template", {}), "guards": contract["guards"]}
    return True
CONTRACT = """输出唯一JSON结构：
{"requests":[{"candidate_id":"实际ID","kind":"definition或profile"}],"decisions":[{"candidate_id":"实际ID","supported":true,"target_role":"reference","alternative_unresolved":false,"confidence":0.95,"reason":"可核验的依据","query_outcome":"usable","exclusion_code":"","missing_evidence":[],"query_role":"连接的查询用途","key_domain":"same","relationship_kind":"entity_reference","coverage_expectation":"complete","additional_predicates":[]}]}。
query_outcome只能是usable、exclude、needs_evidence：
- usable：完整条件可用于查询，有实体引用或有意义的跨实体身份映射。
- exclude：明确不相关、错误目标、血缘、副本或冗余连接；必须填exclusion_code。
- needs_evidence：确实无法判断；必须在missing_evidence中列出具体缺口，不能用它代替明确否定。
exclusion_code只能是meaning_mismatch、replica、lineage_only、redundant_identity、transitive_only、wrong_target；其他结论使用空字符串。
target_role只能是reference、identity、replica、unknown。confidence表示对本次结论的把握，明确否定也可高把握。不要把supported=false直接转换为needs_evidence。
additional_predicates是完整JOIN所需的附加条件：[{"side":"source或target","field":"实际字段","operator":"eq","value":"有证据的固定值"}]，没有条件则[]。不能猜字段和值；需要条件而资料缺失时needs_evidence。
每个candidate_id都要返回结论。不得新增表、字段或SQL。"""

CONTRACT += """
key_domain为same、different、unknown，表示完整标识的编号体系是否相同，不是字段词语是否相似；不同系统或不同用途的编号不能凭都叫编号而视为同一体系。different应exclude，unknown应needs_evidence。
relationship_kind为entity_reference、direct_record_reference、identity、lineage、shared_entity、unknown。lineage和无独立引用的shared_entity应exclude；不能仅凭存在其他路径就排除真实直接记录引用。
coverage_expectation为complete、partial、unknown。表行数不同不构成否定；同一编号反复出现是正常N:1。只包含部分实体的子表允许partial，但必须说明范围依据，不能通过假设范围解释明显错误映射。
metadata已提供，不重复请求。tables是共享表卡片，source/target.table_ref指向表卡片，fields限定当前字段。表职责和字段用途是初步推断，须用原始字段说明和证据复核。"""

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
        if value.get("key_domain") not in {"same", "different", "unknown"}:
            return None
        if value.get("relationship_kind") not in {"entity_reference", "direct_record_reference", "identity", "lineage", "shared_entity", "unknown"}:
            return None
        if value.get("coverage_expectation") not in {"complete", "partial", "unknown"}:
            return None
        if outcome == "usable" and (not value["supported"] or value["key_domain"] != "same"
                                     or value["relationship_kind"] in {"lineage", "shared_entity"}
                                     or value["target_role"] == "replica"):
            return None
    return {"supported": value["supported"], "confidence": max(0, min(1, confidence)),
            "target_role": value["target_role"], "alternative_unresolved": value["alternative_unresolved"],
            "reason": str(value.get("reason") or "")[:1000],
            "query_outcome": value.get("query_outcome") if value.get("query_outcome") in {"usable", "exclude", "needs_evidence"} else "usable" if value["supported"] else "needs_evidence",
            "exclusion_code": str(value.get("exclusion_code") or ""),
            "missing_evidence": value.get("missing_evidence") if isinstance(value.get("missing_evidence"), list) else [],
            "query_role": str(value.get("query_role") or "")[:300],
            "key_domain": value.get("key_domain", "unknown"),
            "relationship_kind": value.get("relationship_kind", "unknown"),
            "coverage_expectation": value.get("coverage_expectation", "unknown"),
            "additional_predicates": value.get("additional_predicates", [])}


async def research(candidates, packet_for, fetch, model, persist, progress):
    from .relationship_research_loop import run
    await run(candidates, packet_for, fetch, model, persist, progress)
