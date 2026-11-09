"""Understand once, clarify in the existing form, then author SQL directly."""

from copy import deepcopy
import asyncio
from datetime import datetime, timedelta, timezone
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, create_model
from semora_agent.model import ModelError, TaskSupersededError
from .query_models import AnalysisOutcome
from semora_agent.runtime import AgentSubmission, run_agent
from semora_agent.sql_prompts import UNDERSTAND, AUTHOR, REPAIR, COVERAGE
from semora_contracts import (
    SQLProgram,
    QueryPlan,
    RequirementDraft,
    QueryConfigurationGap,
    QueryUnderstanding,
)


class Understanding(QueryUnderstanding):
    """Model boundary shares the persisted understanding contract."""

    model_config = ConfigDict(extra="forbid")
    target_state: Literal["known", "needs_direction"] = "known"
    technical_gaps: list[QueryConfigurationGap] = Field(default_factory=list, max_length=16)


class StepRepair(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sql: str = Field(min_length=1, max_length=100000, description="修复后的完整PostgreSQL基准SQL，参数使用:name；不是目标方言SQL。")
    parameters: dict = Field(default_factory=dict)
    change_summary: str = Field(
        min_length=1,
        max_length=500,
        description="执行记录：简短说明改了什么、为什么改，可使用表列名、函数名和SQL表达式；不声称尚未完成的校验或执行已成功。",
    )


class CoverageEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    step: str
    sql: str = Field(min_length=1, max_length=100000, description="业务冲突修正后的完整PostgreSQL基准SQL，参数使用:name；不做目标方言适配。")
    parameters: dict | None = None
    requirement_quote: str = Field(min_length=1, max_length=500)
    change_summary: str = Field(
        min_length=1,
        max_length=500,
        description="用业务语言说明原先和现在的含义差别，不出现表列名或SQL；具体修改放sql。",
    )


class CoverageReview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    edits: list[CoverageEdit] = Field(default_factory=list, max_length=4)
    summary: str = Field(
        default="",
        max_length=1200,
        description="修正后最终查询的业务说明，不是修复过程。不含字段标识、SQL或技术实现，不编造未执行的数据结论。",
    )


def apply_coverage_edits(program, review):
    candidate = program.model_copy(deep=True)
    seen = set()
    for edit in review.edits:
        step = next((s for s in candidate.steps if s.name == edit.step), None)
        if step is None or edit.step in seen:
            raise ValueError("修正必须指向已有且不重复的步骤name")
        seen.add(edit.step)
        step.sql = edit.sql
        if edit.parameters is not None:
            step.parameters = edit.parameters
        step.purpose = edit.change_summary
    if review.edits and review.summary:
        candidate.summary = review.summary
    return candidate


def material(run):
    """One copy of definitions, without the retired compiler's instructions."""
    from .model_context import project_knowledge

    packet = deepcopy((run.agent_memory.get("prepared_context") or [{}])[-1].get("packet", {}))
    # Physical scan comments duplicate published descriptions. Keep types and
    # nullability, including information not yet represented in the ontology.
    for schema in packet.get("schemas", []):
        obj = next(
            (
                o["object"]
                for o in packet.get("objects", [])
                if o["object"]["name"] == schema["object"]
            ),
            {},
        )
        columns = {p.get("column", p["name"]) for p in obj.get("properties", [])}
        for table in schema.get("tables", []):
            table["columns"] = [
                {k: c[k] for k in ("name", "type", "nullable", "primary_key") if k in c}
                for c in table.get("columns", [])
                if c.get("name") in columns
            ]
    return project_knowledge(packet, run)


def context(workflow, run, question):
    from .prepared_context import confirmed_fact_context
    from .datasources import datasource_service

    packet = material(run)
    ids = {o["object"].get("dataSourceId") for o in packet.get("objects", [])}
    sources = []
    for source_id in ids - {None, ""}:
        source = datasource_service.repository.get(source_id)
        from .database_profile import source_profile

        sources.append({"id": source_id, "name": source.name, **source_profile(source)})
    return {
        "sql_contract": {"authoring_dialect": "postgres", "parameter_style": ":name"},
        "request": question,
        "confirmed_answers": confirmed_fact_context(workflow, run),
        "materials": packet,
        "data_sources": sources,
        "current_time": datetime.now(timezone(timedelta(hours=8))).isoformat(),
        "timezone": "Asia/Shanghai",
    }


async def structured(workflow, run, prompt, payload, schema):
    """A malformed response gets one format correction, never a fresh ReAct."""
    from .run_state import revision

    initial = revision(run)
    value = None
    contract_error = ""
    for attempt in range(2):
        try:
            raw = await workflow.model.complete_json(
                prompt,
                json.dumps(
                    {
                        **payload,
                        "output_contract": schema.model_json_schema(),
                        **(
                            {
                                "invalid_response": value,
                                "contract_error": contract_error,
                                "correction_required": "前次响应不可执行。按错误修正完整结构，不得原样重复提交。",
                            }
                            if attempt
                            else {}
                        ),
                    },
                    ensure_ascii=False,
                    default=str,
                ),
            )
            if revision(run) != initial:
                raise TaskSupersededError("需求已更新，丢弃过期模型响应")
            value = raw
            parsed = schema.model_validate(raw)
            candidate = parsed if schema is SQLProgram else None
            if schema is CoverageReview and parsed.edits:
                original = SQLProgram(
                    summary="SQL核对",
                    steps=[{**s, "purpose": "原步骤"} for s in payload["program_steps"]],
                )
                candidate = apply_coverage_edits(original, parsed)
            if candidate is not None:
                allowed = {s["id"] for s in payload.get("data_sources", [])}
                if any(
                    s.engine == "source" and s.data_source_id not in allowed
                    for s in candidate.steps
                ):
                    raise ValueError("source步骤必须使用资料中给定的完整data_source_id")
                from .sql_dialect import readonly_tree, validate_grouping

                previous = []
                objects = [o["object"] for o in payload.get("materials", {}).get("objects", [])]
                for step in candidate.steps:
                    tables = (
                        previous
                        if step.engine == "local"
                        else [
                            o["table"]
                            for o in objects
                            if o.get("dataSourceId") == step.data_source_id
                        ]
                    )
                    try:
                        tree = readonly_tree(step.sql, tables)
                        validate_grouping(tree, objects)
                        if step.engine == "source":
                            from .database_profile import validate_features

                            profile = next(
                                s for s in payload["data_sources"] if s["id"] == step.data_source_id
                            )
                            validate_features(tree, profile)
                    except ValueError as exc:
                        raise ValueError(
                            f"步骤{step.name}的{step.engine}查询校验未通过：{exc}。"
                            f"本步骤可用表：{tables}。保持确认目标，针对错误修正完整SQLProgram；"
                            "同源关联直接在source SQL中完成，local不能访问物理表。"
                        ) from exc
                    previous.append(step.name)
            return parsed
        except TaskSupersededError:
            raise
        except (ValueError, ModelError) as exc:
            # Transport timeouts/auth failures are not malformed JSON.
            if isinstance(exc, ModelError) and str(exc) not in {
                "模型返回的 JSON 无法解析",
                "模型返回必须是 JSON 对象",
                "模型未返回 JSON 文本",
            }:
                raise
            if attempt:
                raise
            contract_error = str(exc)[:1600]


async def analyse(workflow, run, question, ontology, scope):
    from .prepared_context import prepare_context
    from .clarifications import validate_questions
    from .clarifications import (
        clarification_policy,
        prune_answered_questions,
        route_business_definitions,
    )
    from .run_state import revision

    initial = revision(run)
    # Reuse and supplementary tools must agree on the authoritative revision.
    from .prepared_context import confirmed_fact_context

    confirmed_fact_context(workflow, run)
    saved = await prepare_context(workflow, run, question, ontology, scope)
    # Root dataSourceId is inherited by objects in single-source domains.
    for obj in saved["packet"].get("objects", []):
        obj["object"].setdefault("dataSourceId", ontology.get("dataSourceId", ""))
    if revision(run) != initial:
        raise TaskSupersededError("资料准备期间需求已更新")
    policy = clarification_policy(run)
    previous = run.agent_memory.get("sql_understanding", [])
    if not policy["business_allowed"]:
        legacy = run.query_rehearsal or (run.continuation or {}).get("query_rehearsal")
        legacy = legacy.model_dump(mode="json") if hasattr(legacy, "model_dump") else legacy or {}
        snapshot = (
            previous[-1]
            if previous
            else {k: v for k, v in legacy.items() if k in Understanding.model_fields}
        )
        understood = Understanding.model_validate(snapshot or {"summary": question})
        understood.questions = []
        understood.technical_gaps = [
            g
            for g in understood.technical_gaps
            if all(
                any(
                    f.question_id == c.question_id and f.value in c.values for f in run.user_answers
                )
                for c in g.depends_on
            )
        ]
        for gap in understood.technical_gaps:
            gap.depends_on = []
    else:
        from .research_tools import ResearchTools

        toolkit = ResearchTools(workflow, run, ontology, scope)
        toolkit.start_phase("需求理解与口径确认")
        toolkit.context_state = saved

        def guard():
            if revision(run) != initial:
                raise TaskSupersededError("需求已更新，停止旧分析")

        toolkit.task_guard = guard
        task = context(workflow, run, question)
        task["clarification_policy"] = policy
        task["object_catalog"] = [
            {k: o.get(k) for k in ("name", "label", "description")}
            for o in ontology.get("objects", [])
        ]
        task["knowledge_base_ids"] = ontology.get("knowledgeBaseIds", [])
        submission = create_model(
            "AgentSubmission", __base__=AgentSubmission, payload=(Understanding, ...)
        )

        def parse(value):
            report = Understanding.model_validate(value)
            # Reuse form dependency validation, not the query compiler.
            validate_questions(report.questions)
            return report

        tools = toolkit.tools()
        value, memory = await run_agent(
            workflow.model,
            UNDERSTAND,
            task,
            tools,
            [],
            toolkit.event,
            parse,
            submission,
            max_models=12,
            max_tools=10,
            task_guard=guard,
            bounded_protocol=True,
            track_progress=False,
        )
        understood = parse(value)
        run.agent_memory["analyst"] = memory
    report = QueryUnderstanding(**understood.model_dump(mode="json"))
    route_business_definitions(report)
    prune_answered_questions(report, run.user_answers)
    validate_questions(report.questions)
    from .business_copy import polish_public_text

    await polish_public_text(workflow, run, report)
    if (
        report.questions
        and not policy["business_allowed"]
        and report.target_state != "needs_direction"
    ):
        raise ValueError("已提交业务答案，生成过程不能再次新增同阶段问题")
    run.query_rehearsal = report
    run.agent_memory["sql_understanding"] = [report.model_dump(mode="json")]
    draft = RequirementDraft(
        summary=report.summary,
        analysis_steps=report.skeleton,
        evidence=run.evidence,
        clarifications=report.questions,
        technical_gaps=report.technical_gaps,
        clarification_stage="direction" if report.target_state == "needs_direction" else "business",
        clarification_round=policy["answered_rounds"],
        status="awaiting_clarification" if report.questions or report.technical_gaps else "ready",
    )
    workflow.emit(
        run,
        "rehearsal.ready",
        run.status,
        {
            "summary": report.summary,
            "questions": len(report.questions),
            "technical_gaps": len(report.technical_gaps),
            "skeleton": report.skeleton,
        },
        "analyst",
    )
    if report.questions or report.technical_gaps:
        return AnalysisOutcome(plan=None, requirement=draft, model_mode="sql-native")
    payload = context(workflow, run, question)
    run.agent_memory["sql_request_context"] = [{"request": question}]
    program = await structured(workflow, run, AUTHOR, payload, SQLProgram)
    program = await check_coverage(workflow, run, payload, program)
    await polish_public_text(workflow, run, program)
    plan = QueryPlan(
        domain=run.domain,
        intent=program.summary,
        sql_program=program,
        execution_mode="sequential_queries" if len(program.steps) > 1 else "single_query",
        assumptions=program.assumptions,
        evidence=run.evidence,
    )
    return AnalysisOutcome(plan=plan, requirement=draft, model_mode="sql-native")


async def check_coverage(workflow, run, payload, program):
    """One independent comparison, no tools, no review/author negotiation loop."""
    from sqlglot import exp
    import sqlglot

    # A plain projection has no scope/aggregation interaction to reconcile.
    trees = [sqlglot.parse_one(s.sql, read="postgres") for s in program.steps]
    if len(trees) == 1 and not any(trees[0].find(t) for t in (exp.AggFunc, exp.Join, exp.Subquery)):
        return program
    try:
        packet = payload.get("materials", {})
        comparison = {
            "sql_contract": payload["sql_contract"],
            "request": payload["request"],
            "confirmed_answers": payload.get("confirmed_answers", []),
            "data_sources": payload.get("data_sources", []),
            "materials": {
                "objects": [
                    {
                        "object": {
                            **{
                                k: o["object"].get(k)
                                for k in (
                                    "name",
                                    "table",
                                    "primaryKey",
                                    "dataSourceId",
                                    "projection",
                                )
                            },
                            "properties": [
                                {k: p.get(k) for k in ("name", "column", "label", "nullable")}
                                for p in o["object"].get("properties", [])
                            ],
                        },
                    }
                    for o in packet.get("objects", [])
                ],
                "relations": packet.get("relations", []),
                "identity_catalog": packet.get("identity_catalog", []),
            },
            "program_steps": [
                s.model_dump(mode="json", exclude={"purpose"}) for s in program.steps
            ],
        }
        reviewed = await asyncio.wait_for(
            structured(
                workflow,
                run,
                COVERAGE,
                comparison,
                CoverageReview,
            ),
            timeout=60,
        )
    except TaskSupersededError:
        raise
    except (ModelError, ValueError, TimeoutError) as exc:
        workflow.emit(
            run,
            "sql.coverage.unavailable",
            run.status,
            {
                "summary": "一致性核对未完成，保留原SQL进入数据库校验",
                "error_type": type(exc).__name__,
                "reason": str(exc)[:400],
            },
            "sql_author",
        )
        run.delivery_warnings.append("SQL已生成，一致性复查未完成；后续继续数据库校验与只读执行。")
        return program
    if reviewed.edits:
        workflow.emit(
            run,
            "sql.semantic.corrected",
            run.status,
            {"summary": "；".join(e.change_summary for e in reviewed.edits)[:1200]},
            "sql_author",
        )
        return apply_coverage_edits(program, reviewed)
    return program


async def repair_step(workflow, run, name, issue):
    count = int((run.agent_memory.get("sql_repair_count") or [{}])[-1].get("attempts", 0))
    if count >= 2:
        raise ValueError("SQL 经两次定向修复仍未通过：" + str(issue))
    run.agent_memory["sql_repair_count"] = [{"attempts": count + 1}]
    step = next(s for s in run.plan.sql_program.steps if s.name == name)
    saved_request = (run.agent_memory.get("sql_request_context") or [{}])[-1]
    payload = context(workflow, run, saved_request.get("request", run.question))
    artifact = run.source_artifacts.get(name)
    # A compile failure may leave the previous artifact in memory. Never
    # present that stale target statement as the output of the current source.
    current_artifact = artifact if artifact and artifact.semantic_sql == step.sql else None
    from .sql_dialect import dialect_for

    payload.update(
        confirmed_requirement=run.requirement.model_dump(mode="json") if run.requirement else None,
        program=run.plan.sql_program.model_dump(mode="json"),
        current_step=name,
        editable_baseline={"dialect": "postgres", "sql": step.sql, "parameters": step.parameters},
        execution_diagnostic={
            "target_dialect": "duckdb" if step.engine == "local" else dialect_for(step.data_source_id),
            "target_sql": current_artifact.physical_sql if current_artifact else None,
            "error": issue,
        },
        available_results={
            key: {"columns": r.columns, "column_types": r.column_types}
            for key, r in run.source_results.items()
        },
        prior_repairs=run.agent_memory.get("sql_repairs", []),
    )
    fixed = await structured(workflow, run, REPAIR, payload, StepRepair)
    if fixed.sql.strip() == step.sql.strip() and fixed.parameters == step.parameters:
        raise ValueError("修复未改变产生错误的SQL，已停止重复处理")
    from .run_state import digest

    before_hash = digest([step.sql, step.parameters])
    step.sql, step.parameters = fixed.sql, fixed.parameters
    run.agent_memory.setdefault("sql_repairs", []).append(
        {"step": name, "error": issue, "change_summary": fixed.change_summary, "attempt": count + 1}
    )
    workflow.emit(
        run,
        "sql.repaired",
        run.status,
        {"step": name, "summary": fixed.change_summary, "attempt": count + 1,
         "error_stage": issue.get("error_stage"), "error_type": issue.get("error_type"),
         "baseline_before_hash": before_hash, "baseline_after_hash": digest([step.sql, step.parameters])},
        "sql_author",
    )
