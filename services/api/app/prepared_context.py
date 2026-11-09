"""Version-bound retrieval of relevant ontology, schema metadata and knowledge."""

import asyncio
import json
import re
from copy import deepcopy
from pydantic import BaseModel, ConfigDict, Field
from semora_agent.prompts import CONTEXT_SELECTION
from .agent_context import compact_definition
from .datasources import datasource_service
from .knowledge import knowledge_service
from .run_state import binding, digest
from .ontology_paths import connect_objects


class ObjectUse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    phrase: str = Field(
        min_length=1, max_length=100, description="需求或确认答案中的原文对象/归属短语"
    )
    object: str = Field(min_length=1, max_length=100, description="承载该短语的目录对象name")


class ContextSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    object_uses: list[ObjectUse] = Field(
        default_factory=list,
        max_length=12,
        description="先逐项映射需求中提到的对象与归属来源，避免只覆盖取数主表而漏掉所属对象。",
    )
    objects: list[str] = Field(
        default_factory=list, max_length=8, description="当前目录中完成需求必需的准确对象name。"
    )
    knowledge_keywords: list[str] = Field(
        default_factory=list,
        max_length=5,
        description="按需求选择必要的检索词，最多5个，每项单独检索，不为凑数量扩展主题。",
    )


class KnowledgeSelection(BaseModel):
    knowledge_keywords: list[str] = Field(
        default_factory=list,
        max_length=5,
        description="按当前需求决定所需数量，最多5个不同的核心检索词，每项单独检索。",
    )


def field_candidates(objects, text, limit=48):
    """Small, exact-label retrieval hints; never ship the complete field catalog."""
    text = text.casefold()
    rows = []
    for obj in objects.values():
        label = obj.get("label") or obj["name"]
        overlap = sum(label[i : i + 2].casefold() in text for i in range(len(label) - 1))
        for prop in obj.get("properties", []):
            aliases = prop.get("aliases") or []
            names = [prop.get("label"), *([aliases] if isinstance(aliases, str) else aliases)]
            matches = []
            for name in names:
                if not isinstance(name, str) or not 2 <= len(name) <= 40:
                    continue
                token = name.casefold()
                matched = (
                    token in text
                    if re.search(r"[\u4e00-\u9fff]", token)
                    else re.search(r"(?<!\w)" + re.escape(token) + r"(?!\w)", text)
                )
                if matched:
                    matches.append(name)
            if matches:
                matched = max(matches, key=len)
                rows.append((len(matched), overlap, obj["name"], prop["name"], matched))
    rows.sort(key=lambda r: (-r[0], -r[1], r[2], r[3]))
    return [{"object": o, "field": f, "label": label} for _, _, o, f, label in rows[:limit]]


def knowledge_excerpt(text, keywords, limit=8000):
    """Select verbatim paragraphs, not a new authoritative model definition."""
    text = str(text or "")
    paragraphs = list(dict.fromkeys(p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()))
    terms = [str(term).casefold() for term in keywords if term]
    matches = {i for i, p in enumerate(paragraphs) if any(t in p.casefold() for t in terms)}
    priority = {0} if paragraphs else set()
    for i in matches:
        priority.update(j for j in (i - 1, i, i + 1) if 0 <= j < len(paragraphs))
    selected = {}
    remaining = limit
    for i in [*sorted(priority), *[j for j in range(len(paragraphs)) if j not in priority]]:
        separator = 2 if selected else 0
        if len(paragraphs[i]) + separator <= remaining:
            selected[i] = paragraphs[i]
            remaining -= len(paragraphs[i]) + separator
    # An oversized single paragraph remains an explicitly marked excerpt.
    if not selected and paragraphs:
        i = min(matches) if matches else 0
        selected[i] = paragraphs[i][:limit]
    content = "\n\n".join(selected[i] for i in sorted(selected))
    return {
        "content": content,
        "excerpted": len(selected) < len(paragraphs)
        or any(selected[i] != paragraphs[i] for i in selected),
        "original_chars": len(text),
        "selected_paragraphs": sorted(selected),
    }


def input_binding(run, ontology, scope):
    facts = {f.question_id: {"field": f.field, "value": f.value} for f in run.user_answers}
    scans = {}
    for source_id in {
        o.get("dataSourceId") or ontology.get("dataSourceId")
        for o in authorized_objects(run, ontology).values()
    }:
        try:
            source = datasource_service.repository.get(source_id)
            scans[source_id] = {
                "at": source.last_scan_at,
                "status": source.status,
                "version": (source.scan_result.get("connection") or {}).get("version"),
            }
        except KeyError:
            scans[source_id] = "not_registered"
    return digest(
        {
            "context_protocol": 11,
            "binding": binding(run, ontology, scope),
            "question": run.question,
            "answers": facts,
            "answer_context": run.agent_memory.get("confirmed_fact_context", []),
            "scans": scans,
        }
    )


def confirmed_fact_context(workflow, run):
    """Keep the meaning of a selected option, including legacy explanations."""
    previous = (run.continuation or {}).get("confirmed_fact_context", [])
    saved = run.agent_memory.get("confirmed_fact_context", [])
    definitions = []
    seen = set()
    current = run
    for _ in range(12):
        if current.id in seen:
            break
        seen.add(current.id)
        drafts = [
            current.requirement_draft.model_dump(mode="json") if current.requirement_draft else {},
            *[r.get("draft") or {} for r in reversed(current.alignment_rounds)],
            (current.continuation or {}).get("draft") or {},
        ]
        definitions.extend(q for d in drafts for q in d.get("clarifications", []))
        parent = getattr(workflow.store, "runs", {}).get(
            (current.continuation or {}).get("source_run_id")
        )
        if parent is None or parent.domain != run.domain:
            break
        current = parent
    rows = []
    for fact in run.user_answers:
        row = fact.model_dump(mode="json")
        inherited = next(
            (
                r
                for r in [*saved, *previous]
                if all(r.get(k) == row.get(k) for k in ("question_id", "field", "value"))
                and r.get("clarification_question")
            ),
            None,
        )
        if inherited:
            row.update(
                {
                    k: inherited[k]
                    for k in ("clarification_question", "selected_option_description")
                    if k in inherited
                }
            )
        else:
            for q in definitions:
                if q.get("id") != fact.question_id or q.get("field") != fact.field:
                    continue
                row["clarification_question"] = q.get("question", "")
                option = next(
                    (
                        o
                        for o in q.get("options", [])
                        if fact.value in (o.get("value"), o.get("id"))
                    ),
                    None,
                )
                if option:
                    row.update(
                        clarification_question=q.get("question", ""),
                        selected_option_description=option.get("description", ""),
                    )
                break
        rows.append(row)
    run.agent_memory["confirmed_fact_context"] = rows
    return rows


def authorized_objects(run, ontology):
    return {o["name"]: o for o in ontology.get("objects", [])}


def context_packet(workflow, run, ontology, names):
    available = authorized_objects(run, ontology)
    selected = set(names)
    if not selected <= available.keys():
        raise ValueError("上下文请求包含未发布或未授权对象")
    relations = [
        r for r in ontology.get("relations", []) if r["from"] in available and r["to"] in available
    ]
    gaps = []
    if len(selected) > 1:
        try:
            # Already connected selected objects need no external bridge.
            # An alternate path through an unrelated object only adds noise.
            seen = {next(iter(selected))}
            while True:
                expanded = seen | {
                    n
                    for r in relations
                    if {r["from"], r["to"]} <= selected and ({r["from"], r["to"]} & seen)
                    for n in (r["from"], r["to"])
                }
                if expanded == seen:
                    break
                seen = expanded
            if seen != selected:
                paths = connect_objects(selected, relations)
                for r in relations:
                    if r["name"] in paths:
                        selected.update([r["from"], r["to"]])
        except ValueError:
            gaps.append("部分对象未配置可连通关系；不得猜测关联条件")
    if len(selected) > 12:
        raise ValueError("所需关联上下文超过12个对象，请缩小本次查询范围")
    objects = []
    schemas = []
    for name in sorted(selected):
        obj = available[name]
        objects.append({"object": compact_definition(obj)})
        source_id = obj.get("dataSourceId") or ontology.get("dataSourceId")
        try:
            source = datasource_service.repository.get(source_id)
            table = obj.get("table", "").split(".")[-1]
            matches = [
                t
                for t in source.scan_result.get("tables", [])
                if str(t.get("name") or t.get("table_name") or "").split(".")[-1] == table
            ]
            # Only scanned structural metadata. Never include secret refs or rows.
            tables = [
                {
                    k: deepcopy(t[k])
                    for k in (
                        "name",
                        "table_name",
                        "schema",
                        "columns",
                        "primary_key",
                        "foreign_keys",
                    )
                    if k in t
                }
                for t in matches
            ]
            schemas.append({"object": name, "tables": tables, "scanned": bool(tables)})
        except KeyError:
            schemas.append({"object": name, "tables": [], "scanned": False})
    metrics = []
    for metric in ontology.get("metrics", []):
        refs = set(re.findall(r"\b([A-Za-z_][A-Za-z0-9_]*)\.", str(metric.get("expression", ""))))
        if metric.get("grain") in selected or refs and refs <= selected:
            metrics.append(compact_definition(metric))
    return {
        "selected_objects": sorted(selected),
        "identity_catalog": [
            {"object": o["name"], "label": o.get("label"), "primaryKey": o["primaryKey"]}
            for o in available.values()
            if o.get("primaryKey")
        ],
        "objects": objects,
        "schemas": schemas,
        "relations": [compact_definition(r) for r in relations if {r["from"], r["to"]} <= selected],
        "metrics": metrics,
        "gaps": gaps,
    }


async def prepare_context(workflow, run, question, ontology, scope):
    key = input_binding(run, ontology, scope)
    saved = (run.agent_memory.get("prepared_context") or [{}])[-1]
    if saved.get("binding") == key:
        workflow.emit(
            run,
            "context.reused",
            run.status,
            {
                "summary": "复用当前需求版本的准备资料，不重新探索",
                "objects": saved["packet"]["selected_objects"],
            },
            "context_service",
        )
        return saved
    available = authorized_objects(run, ontology)
    kb_ids = ontology.get("knowledgeBaseIds") or []
    facts = run.agent_memory.get("confirmed_fact_context") or [
        f.model_dump(mode="json") for f in run.user_answers
    ]
    keywords = []
    packet = {"knowledge": []}
    # Retrieval precedes ontology selection: definitions can reveal which
    # objects are relevant. This extraction sees no schema directory.
    if kb_ids:
        try:
            raw = await workflow.model.complete_json(
                "【任务】从需求与确认口径提取需要查询定义的核心词。\n【要求】按需要决定数量，最多5个不同词，不凑数量，每项一个独立检索词，不拼接查询表达式。\n【输出】按给定JSON契约返回knowledge_keywords。",
                json.dumps(
                    {
                        "request": question,
                        "confirmed_answers": facts,
                        "output_contract": KnowledgeSelection.model_json_schema(),
                    },
                    ensure_ascii=False,
                ),
            )
            if isinstance(raw.get("knowledge_keywords"), list):
                raw = {
                    **raw,
                    "knowledge_keywords": list(
                        dict.fromkeys(
                            t.strip()
                            for t in raw["knowledge_keywords"]
                            if isinstance(t, str) and t.strip() and len(t.strip()) <= 64
                        )
                    )[:5],
                }
            keywords = KnowledgeSelection.model_validate(raw).knowledge_keywords
        except Exception as exc:
            from semora_agent.model import TaskSupersededError

            if isinstance(exc, TaskSupersededError):
                raise
            packet["knowledge_warning"] = "检索词提取暂不可用，按需求原文检索；辅助知识不阻断规划"
        if not keywords:
            keywords = list(
                dict.fromkeys(re.findall(r"[\u4e00-\u9fffA-Za-z0-9_+-]{2,64}", question))
            )[:5]
    keywords = list(
        dict.fromkeys(term.strip() for term in keywords if term.strip() and len(term.strip()) <= 64)
    )
    packet["knowledge_keywords"] = keywords
    packet["knowledge_retrieval"] = []
    discoveries = run.agent_memory.setdefault("knowledge_discovery", [])

    def remember(kb, kind, ref):
        value = {"kb_id": kb, "kind": kind, "ref": ref}
        if ref and value not in discoveries:
            discoveries.append(value)

    async def collect_knowledge():
        pages = set()
        for term in keywords:
            for kb in kb_ids:
                try:
                    async with asyncio.timeout(10):
                        rows = await knowledge_service._wiki_search(kb, re.escape(term))
                except Exception as exc:
                    from semora_agent.model import TaskSupersededError

                    if isinstance(exc, TaskSupersededError):
                        raise
                    packet["knowledge_retrieval"].append(
                        {"keyword": term, "knowledge_base_id": kb, "state": "unavailable"}
                    )
                    packet["knowledge_warning"] = (
                        "部分关键词检索暂不可用；已保留其他检索结果，未将检索失败视为业务缺口"
                    )
                    continue
                packet["knowledge_retrieval"].append(
                    {
                        "keyword": term,
                        "knowledge_base_id": kb,
                        "state": "completed",
                        "count": len(rows),
                    }
                )
                for row in rows:
                    remember(kb, "page", row.get("slug"))
                for row in rows[:2]:
                    slug = row.get("slug")
                    if not slug or (kb, slug) in pages or len(pages) >= 3:
                        continue
                    try:
                        async with asyncio.timeout(10):
                            page = await knowledge_service._wiki_page(kb, slug)
                    except Exception as exc:
                        from semora_agent.model import TaskSupersededError

                        if isinstance(exc, TaskSupersededError):
                            raise
                        packet["knowledge_warning"] = (
                            "部分页面暂不可用；已保留入口与其他证据，可按具体缺口补读"
                        )
                        continue
                    pages.add((kb, slug))
                    for link in (page.get("out_links") or []) + (page.get("in_links") or []):
                        remember(kb, "page", link.get("slug") if isinstance(link, dict) else link)
                    for source in page.get("source_refs") or []:
                        if isinstance(source, str):
                            remember(kb, "source", source)
                    evidence = knowledge_service._page_evidence(kb, page)
                    if not any(
                        e.knowledge_base_id == kb and e.locator == evidence.locator
                        for e in run.evidence
                    ):
                        run.evidence.append(evidence)
                    item = {
                        "knowledge_base_id": kb,
                        "slug": slug,
                        "title": page.get("title"),
                        **knowledge_excerpt(page.get("content") or page.get("summary"), keywords),
                        "read_more": {"knowledge_base_id": kb, "slug": slug},
                        "links": page.get("out_links") or [],
                        "source_refs": page.get("source_refs") or [],
                    }
                    packet["knowledge"].append(item)

    if kb_ids and keywords:
        try:
            await collect_knowledge()
        except Exception as exc:
            from semora_agent.model import TaskSupersededError

            if isinstance(exc, TaskSupersededError):
                raise
            packet["knowledge_warning"] = (
                "辅助知识准备未全部完成，保留已取得证据；不阻断基于本体与用户口径的规划"
            )
    workflow.emit(
        run,
        "knowledge.prepared",
        run.status,
        {
            "summary": "已检索本次需求的辅助定义；根据取得的资料选择相关本体",
            "knowledge_pages": len(packet["knowledge"]),
            "knowledge_bases": len(kb_ids),
        },
        "context_service",
    )
    selection = ContextSelection(objects=list(available)) if len(available) <= 1 else None
    if selection is None:
        raw = await workflow.model.complete_json(
            CONTEXT_SELECTION,
            json.dumps(
                {
                    "request": question,
                    "confirmed_answers": facts,
                    "knowledge": packet["knowledge"],
                    "object_catalog": [
                        {k: o.get(k) for k in ("name", "label", "description", "primaryKey")}
                        for o in available.values()
                    ],
                    "field_candidates": field_candidates(
                        available, question + " " + " ".join(str(f.get("value", "")) for f in facts)
                    ),
                    "relation_catalog": {
                        "columns": ["name", "from", "to"],
                        "rows": [
                            [r.get(k) for k in ("name", "from", "to")]
                            for r in ontology.get("relations", [])
                            if r.get("from") in available and r.get("to") in available
                        ],
                    },
                    "metrics": [
                        {k: m.get(k) for k in ("name", "label", "grain")}
                        for m in ontology.get("metrics", [])
                    ],
                    "output_contract": ContextSelection.model_json_schema(),
                },
                ensure_ascii=False,
            ),
        )
        selection = ContextSelection.model_validate(raw)
    selection.objects = list(
        dict.fromkeys([*selection.objects, *(use.object for use in selection.object_uses)])
    )
    selection = ContextSelection.model_validate(selection.model_dump())
    if not set(selection.objects) <= available.keys():
        raise ValueError("上下文选择包含未发布或未授权对象，已停止准备")
    packet = {**context_packet(workflow, run, ontology, selection.objects), **packet}
    saved = {"binding": key, "packet": packet, "supplements": 0}
    run.agent_memory["prepared_context"] = [saved]
    workflow.emit(
        run,
        "context.prepared",
        run.status,
        {
            "summary": "已准备相关对象、关系、扫描结构及辅助证据；进入规划，不开放自由探索",
            "objects": packet["selected_objects"],
            "relations": len(packet["relations"]),
            "knowledge_pages": len(packet["knowledge"]),
        },
        "context_service",
    )
    return saved


def supplement_context(workflow, run, ontology, state, names, purpose, reason, scope):
    if state.get("binding") != input_binding(run, ontology, scope):
        raise ValueError("准备资料的权限或版本已变化，需要重新准备")
    if not reason.strip():
        raise ValueError("必须说明缺少哪个核心信息及其对结果的影响")
    available = authorized_objects(run, ontology)
    if not names or len(names) > 4 or not set(names) <= available.keys():
        raise ValueError("只允许申请当前授权目录中最多4个核心对象")
    existing = set(state["packet"]["selected_objects"])
    if set(names) <= existing:
        return {
            "available": True,
            "already_prepared": True,
            "unchanged": True,
            "prepared_context": state["packet"],
            "message": "所需资料已在prepared_context中；已直接返回原文，不重新检索。业务口径不明确请澄清，不要重复读取。",
        }
    supplement_limit = 8
    if state["supplements"] >= supplement_limit:
        return {
            "available": False,
            "message": "本次核心上下文补充预算已用完；请交付具体业务澄清或受阻原因，不继续扩展。",
        }
    packet = context_packet(workflow, run, ontology, existing | set(names))
    state["supplements"] += 1
    packet["knowledge"] = state["packet"].get("knowledge", [])
    state["packet"] = packet
    workflow.emit(
        run,
        "context.supplemented",
        run.status,
        {
            "summary": "为完成当前需求补齐核心上下文",
            "objects": names,
            "purpose": purpose,
            "reason": reason[:300],
        },
        "context_service",
    )
    return {
        "available": True,
        "prepared_context": packet,
        "message": "核心资料已补齐。资料足够即提交；业务口径不明确则集中澄清，不扩展可选目标。",
    }
