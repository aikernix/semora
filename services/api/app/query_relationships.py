"""Query-oriented table roles, join predicates and exclusion decisions."""
from __future__ import annotations

import asyncio
import json
import math
import re
from collections import Counter

from .relationship_research import batches

TABLE_SYSTEM = """角色：数据模型结构分析员。
任务：识别每张表的职责、每行粒度、核心实体及字段用途，为查询连接选择实体主表提供依据。不要根据主键唯一就认定为主表。
区分entity_master、dimension、transaction、detail、extension、bridge、history、view、replica、unknown。物理VIEW不代替业务职责，视图也可以是实体主表或交易主表；仅提供投影且无法判断业务职责时使用view。扩展表和历史表可能与主表共用主键，但不因此成为所有引用的目标。
仅判断提供的table_id和字段，不新增关联或SQL。保留不同引用角色；时间和金额是业务属性，除非有明确引用证据，不是实体连接键。
若一个标识字段根据伴随的类型字段引用不同实体，将其标记为polymorphic_reference，对应类型鉴别字段标记为discriminator；普通业务类别不等于鉴别字段。
输出JSON：{"tables":[{"table_id":"...","role":"entity_master","entity":"实体含义","grain":"一行代表什么","field_roles":{"字段名":"identifier/reference/polymorphic_reference/discriminator/measure/time/attribute/unknown"},"reason":"简短依据"}]}。"""
ROLES = {"entity_master", "dimension", "transaction", "detail", "extension", "bridge", "history", "view", "replica", "unknown"}
FIELD_ROLES = {"identifier", "reference", "polymorphic_reference", "discriminator", "measure", "time", "attribute", "unknown"}


async def classify_tables(tables, model, saved=None, checkpoint=None):
    """Wide tables are split by columns, retaining identity and table context."""
    result = dict(saved or {})
    packets = []
    for table in tables:
        if (table["id"] in result and {c["name"] for c in table.get("columns", [])}
                <= set(result[table["id"]].get("field_roles", {}))):
            continue
        cols = table.get("columns") or []
        for start in range(0, max(1, len(cols)), 40):
            packets.append({"table_id": table["id"], "name": table.get("name"),
                "description": table.get("description"), "kind": table.get("kind"),
                "primary_keys": table.get("primary_keys", []),
                "columns": [{"name": c["name"], "type": c.get("type"),
                             "description": c.get("description") or c.get("comment")}
                            for c in cols[start:start + 40]]})
    queue = asyncio.Queue()
    for group, fits in batches(packets, max_items=4):
        if fits:
            queue.put_nowait(group)
    async def worker():
        while not queue.empty():
            group = queue.get_nowait()
            allowed = {}
            for p in group:
                allowed.setdefault(p["table_id"], set()).update(c["name"] for c in p["columns"])
            try:
                response = await model.complete_json(TABLE_SYSTEM, json.dumps(group, ensure_ascii=False))
                for value in response.get("tables", []):
                    if not isinstance(value, dict) or value.get("table_id") not in allowed:
                        continue
                    identifier = value["table_id"]
                    entry = result.setdefault(identifier, {"field_roles": {}})
                    role = value.get("role")
                    if role in ROLES:
                        if entry.get("role") not in (None, role):
                            entry["role"] = "unknown"
                        else:
                            entry["role"] = role
                    for key in ("entity", "grain", "reason"):
                        entry[key] = str(value.get(key) or entry.get(key) or "")[:600]
                    fields = value.get("field_roles") or {}
                    if isinstance(fields, dict):
                        entry["field_roles"].update({k: v for k, v in fields.items()
                            if k in allowed[identifier] and v in FIELD_ROLES})
                if checkpoint:
                    await checkpoint(result)
            except Exception:
                # Missing role classification never constitutes rejection evidence.
                continue
    await asyncio.gather(*(worker() for _ in range(8)))
    return result


EXCLUSION_CODES = {"meaning_mismatch", "replica", "lineage_only", "redundant_identity", "transitive_only", "wrong_target"}


def triage(candidate, review, independent):
    """Only a specific, independently corroborated negative becomes exclusion."""
    negative = review.get("query_outcome") == "exclude" and independent.get("query_outcome") == "exclude"
    reason = review.get("exclusion_code")
    if (negative and reason in EXCLUSION_CODES and independent.get("exclusion_code") in EXCLUSION_CODES):
        return reason
    return None


def validated_guards(candidate, guards, table_maps):
    """Model proposes typed predicates; program verifies every field and operator."""
    if not isinstance(guards, list) or len(guards) > 8:
        return None
    result = []
    for guard in guards:
        if not isinstance(guard, dict) or guard.get("side") not in {"source", "target"}:
            return None
        side = guard["side"]
        table = table_maps[candidate[side + "_datasource_id"]][candidate[side + "_table_id"]]
        if guard.get("field") not in {c["name"] for c in table.get("columns", [])}:
            return None
        value = guard.get("value")
        if guard.get("operator") != "eq" or type(value) not in {str, int, float, bool}:
            return None
        if isinstance(value, float) and not math.isfinite(value):
            return None
        descriptions = " ".join(str(c.get("description") or c.get("comment") or "")
            for source_id, table_id in ((candidate["source_datasource_id"], candidate["source_table_id"]),
                                        (candidate["target_datasource_id"], candidate["target_table_id"]))
            for c in table_maps[source_id][table_id].get("columns", []))
        if not re.search(r"(?<![\w.])" + re.escape(str(value)) + r"(?![\w.])", descriptions):
            return None
        if any(g["side"] == side and g["field"] == guard["field"] and g["value"] != value for g in result):
            return None
        result.append({"side": side, "field": guard["field"], "operator": "eq", "value": value})
    return sorted(result, key=lambda g: (g["side"], g["field"], json.dumps(g["value"], ensure_ascii=False)))


def entity_references(tables, profiles):
    """Route reference fields toward entity anchors, not all sibling tables."""
    from .relationship_evidence import meaning_tokens
    anchors = []
    for table in tables:
        profile = profiles.get(table["id"], {})
        if profile.get("role") not in {"entity_master", "dimension", "transaction"}:
            continue
        columns = {c["name"]: c for c in table.get("columns", [])}
        for key in (table.get("primary_keys") or []) + (table.get("unique_constraints") or []):
            names = key.get("columns") or []
            if len(names) == 1 and names[0] in columns:
                column = columns[names[0]]
                tokens = meaning_tokens(column) | meaning_tokens({"description": profile.get("entity")}) | meaning_tokens(table)
                anchors.append((table, column, tokens))
    frequency = Counter(token for _, _, tokens in anchors for token in tokens)
    common = {token for token, count in frequency.items() if count >= max(4, len(anchors) * .5)}
    for source in tables:
        source_profile = profiles.get(source["id"], {})
        field_roles = source_profile.get("field_roles", {})
        for column in source.get("columns", []):
            shared_identifier = (field_roles.get(column["name"]) == "identifier"
                                 and source_profile.get("role") in {"extension", "history", "detail", "bridge"})
            if field_roles.get(column["name"]) not in {"reference", "polymorphic_reference"} and not shared_identifier:
                continue
            tokens = meaning_tokens(column) - common
            ranked = [(len(tokens & terms), target["id"], field["name"], target, field)
                      for target, field, terms in anchors if target["id"] != source["id"] and tokens & terms]
            ranked.sort(key=lambda v: (-v[0], v[1], v[2]))
            for _, _, _, target, field in ranked[:3]:
                yield source, column, target, field


def deduplicate_query_edges(candidates):
    """Same complete join in reverse is one traversable edge, not two joins."""
    seen = {}
    ordered = sorted(candidates, key=lambda c: (c["evidence_grade"] not in {"foreign_key", "manual"},
                                                c.get("cardinality") != "N:1", c["id"]))
    for c in ordered:
        if c["status"] != "accepted":
            continue
        pairs = tuple(sorted(tuple(sorted(((c["source_datasource_id"], c["source_table_id"], p["source"]),
                                           (c["target_datasource_id"], c["target_table_id"], p["target"]))))
                             for p in c["key_pairs"]))
        guards = tuple(sorted((c[g["side"] + "_datasource_id"], c[g["side"] + "_table_id"],
                                g["field"], json.dumps(g["value"], ensure_ascii=False))
                               for g in c.get("condition_template", {}).get("guards", [])))
        key = (pairs, guards)
        if key in seen and c["evidence_grade"] not in {"foreign_key", "manual"}:
            c["status"] = "rejected"
            c["evidence"].append({"kind": "automatic_exclusion", "code": "duplicate_direction",
                                   "canonical_candidate_id": seen[key], "reason": "完整连接条件重复，可沿同一关系双向遍历"})
        else:
            seen[key] = c["id"]


def contextual_references(tables, profiles):
    """Resolve generic reference fields using documented neighboring type hints."""
    names = {}
    for table in tables:
        names.setdefault(str(table.get("name", "")).lower(), []).append(table)
    already_routed = {(s["id"], c["name"]) for s, c, _, _ in entity_references(tables, profiles)}
    for source in tables:
        hints = []
        for column in source.get("columns", []):
            text = str(column.get("description") or column.get("comment") or "")
            targets = {t["id"]: t for token in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", text.lower()) for t in names.get(token, [])}
            for target in targets.values():
                if target["id"] == source["id"]:
                    continue
                keys = (target.get("primary_keys") or []) + (target.get("unique_constraints") or [])
                for key in keys:
                    fields = key.get("columns") or []
                    if len(fields) == 1:
                        field = next((c for c in target.get("columns", []) if c["name"] == fields[0]), None)
                        if field:
                            hints.append((column["name"], text, target, field))
        for column in source.get("columns", []):
            if profiles.get(source["id"], {}).get("field_roles", {}).get(column["name"]) not in {"reference", "polymorphic_reference"}:
                continue
            if (source["id"], column["name"]) in already_routed:
                continue
            for hint_field, text, target, field in hints:
                yield source, column, target, field, {"kind": "contextual_reference_hint", "field": hint_field, "description": text}
