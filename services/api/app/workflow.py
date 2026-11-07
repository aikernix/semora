from __future__ import annotations
from semora_agent.prompts import SQL_EXPLANATION, ANSWER_EXPLANATION
from semora_agent.legacy_prompts import INITIAL_UNDERSTANDING

import asyncio
import hashlib
import json
import os
import re
import copy
from datetime import datetime, timezone
from typing import Awaitable, Callable

import httpx
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command, interrupt
from semora_agent import AgentState, AnalystAgent, ReviewerAgent, build_query_graph
from semora_agent.model import TaskSupersededError
from semora_contracts import (
    ApprovalDecision,
    AuthorizationContext,
    EvidenceRef,
    ExecutionResult,
    OntologySnapshotRef,
    QueryPlan,
    QueryRun,
    ConfirmedFact,
    RequirementSpec,
    RequirementDraft,
    RunEvent,
    RunStatus,
    ReviewDecision,
    SystemSettings,
)
from semora_telemetry import traced_operation

from .gate import review_sql
from .knowledge import knowledge_service
from .ontology import ontology_service
from .planner import compile_plan
from .datasources import datasource_service
from .continuation import handoff
from .evidence_policy import EVIDENCE_REUSE_TTL_SECONDS
from .store import Store
from .settings import settings_service

Executor = Callable[[str, dict, list[str], dict], Awaitable[ExecutionResult]]
InjectedExecutor = Callable[[str, dict, list[str]], Awaitable[ExecutionResult]]
Estimator = Callable[[str, dict, str], Awaitable[dict]]
Retriever = Callable[[str], Awaitable[list[EvidenceRef]]]
SnapshotLoader = Callable[[str], Awaitable[tuple[OntologySnapshotRef, dict]]]


class Workflow:
    def __init__(
        self,
        store: Store,
        execute: InjectedExecutor | None = None,
        retrieve: Retriever | None = None,
        snapshot_loader: SnapshotLoader | None = None,
        checkpointer=None,
        estimate: Estimator | None = None,
    ) -> None:
        self.store = store
        if execute is None:
            self.execute: Executor = self._remote_execute
        else:
            async def execute_adapter(
                sql: str, parameters: dict, sensitive_columns: list[str], _: dict
            ) -> ExecutionResult:
                return await execute(sql, parameters, sensitive_columns)

            self.execute = execute_adapter
        self.estimate = estimate or (self._static_estimate if execute is not None else self._remote_estimate)
        self.retrieve = retrieve
        self.snapshot_loader = snapshot_loader or ontology_service.latest_snapshot_bundle
        self.analyst = AnalystAgent()
        self.reviewer = ReviewerAgent()
        self._tasks: dict[str, asyncio.Task] = {}
        self._checkpointer = checkpointer or InMemorySaver()
        self._build_graph()

    def _build_graph(self) -> None:
        self.graph = build_query_graph(
            retrieve=self._retrieve_node,
            analyse=self._analyse_node,
            align=self._align_node,
            compile_sql=self._compile_node,
            gate_and_review=self._gate_review_node,
            approval=self._approval_node,
            execute=self._execute_node,
            explain=self._explain_node,
            revise=self._revise_node,
            route_after_step=self._route_after_step,
            checkpointer=self._checkpointer,
        )

    def configure_checkpointer(self, checkpointer) -> None:
        self._checkpointer = checkpointer
        self._build_graph()

    def runtime_info(self) -> dict[str, str]:
        return {
            "engine": "langgraph",
            "agents": "langchain.create_agent / tool-calling",
            "checkpointer": type(self._checkpointer).__name__,
        }

    async def _compile(self, plan: QueryPlan, ontology: dict):
        # A multi-source domain can still produce a single-source query. Wren
        # consumes one local MDL, never the sourceProjects envelope.
        if 'sourceProjects' in (ontology.get('_wrenArtifact') or {}) or len({str(o.get('dataSourceId') or ontology.get('dataSourceId') or '') for o in ontology.get('objects', [])}) > 1:
            from semora_query_core.sources import source_ontology
            ontology = source_ontology(ontology, plan.data_source_id)
        url = os.getenv("SEMANTIC_PLANNER_URL")
        if not url:
            return compile_plan(plan, ontology)
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                response = await client.post(
                    f"{url.rstrip('/')}/internal/v1/compile",
                    json={"plan": plan.model_dump(mode="json"), "ontology": ontology},
                    headers={"X-Semora-Internal-Token": os.getenv("INTERNAL_TOKEN", "semora-dev-internal")},
                )
                response.raise_for_status()
                from semora_contracts import SQLArtifact

                return SQLArtifact.model_validate(response.json())
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 422:
                try:
                    detail = exc.response.json().get('detail', '查询计划或本体产物校验失败')
                    if isinstance(detail, list):
                        detail = '; '.join(str(item.get('msg', '参数校验失败')) for item in detail)
                except ValueError:
                    detail = '查询计划或本体产物校验失败'
                raise ValueError(str(detail)[:2000]) from exc
            if os.getenv("ALLOW_LOCAL_PLANNER_FALLBACK", "true").lower() == "true":
                return compile_plan(plan, ontology)
            raise
        except Exception:
            if os.getenv("ALLOW_LOCAL_PLANNER_FALLBACK", "true").lower() == "true":
                return compile_plan(plan, ontology)
            raise

    async def _ontology_snapshot(self, domain: str) -> tuple[OntologySnapshotRef, dict]:
        try:
            reference, ontology = await self.snapshot_loader(domain)
        except Exception as exc:
            from .ontology import OntologyCoreError
            if not isinstance(exc,OntologyCoreError) or exc.status_code!=404:
                raise
            ontology=await ontology_service.get_draft(domain)
            digest=hashlib.sha256(json.dumps(ontology,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
            reference=OntologySnapshotRef(domain=domain,version=ontology['version'],content_hash='sha256:'+digest,artifact_version='draft',data_source_id=ontology.get('dataSourceId',''))
        if reference.domain != domain or ontology.get("domain") != domain:
            raise ValueError("本体快照业务域与会话不一致")
        return reference, ontology

    def emit(
        self,
        run: QueryRun,
        event: str,
        status: RunStatus,
        data: dict | None = None,
        actor: str = "conversation_owner",
    ) -> None:
        run.status = status
        from .delivery import build_delivery,best_effort
        run.delivery=build_delivery(run)
        if status==RunStatus.FAILED and best_effort(run) and not run.answer and run.delivery:
            run.answer=run.delivery.summary
        payload = data or {}
        run.events.append(RunEvent(event=event, status=status, data=payload))
        self.store.audit.append(run.id, run.trace_id, event, actor, payload)
        self.store.save_run(run)

    def _run(self, state: AgentState) -> QueryRun:
        run_id = state["run_id"]
        if run_id not in self.store.runs:
            raise RuntimeError(f"run state not found: {run_id}")
        return self.store.runs[run_id]

    def _route_after_step(self, state: AgentState) -> str:
        if state.get('task_superseded'):return 'end'
        status = self._run(state).status
        return "end" if status in {RunStatus.FAILED, RunStatus.CLARIFYING, RunStatus.COMPLETED} else "continue"

    def _discard_superseded_task(self,run):
        self.emit(run,'task.result.superseded',run.status,{'summary':'输入或权限已更新，旧任务结果未写入当前方案，也未继续执行'},'conversation_owner')
        return {'task_superseded':True}

    async def _retrieve_node(self, state: AgentState) -> dict:
        run = self._run(state)
        self.emit(run, "retrieval.started", RunStatus.RETRIEVING)
        try:
            run.ontology, ontology_model = await self._ontology_snapshot(run.domain)
            knowledge_base_ids = [str(item) for item in ontology_model.get("knowledgeBaseIds", [])]
            data_source_id = str(ontology_model.get("dataSourceId") or "")
            allowed = run.authorization.allowed_data_source_ids if run.authorization else None
            if allowed is not None and data_source_id not in allowed:
                raise ValueError('当前身份无权访问业务域绑定的数据源')
            if not data_source_id:
                raise ValueError("本体未绑定数据源")
            if run.ontology.data_source_id and run.ontology.data_source_id != data_source_id:
                raise ValueError("本体快照数据源绑定不一致")
            if run.continuation:
                source = self.store.runs.get(run.continuation['source_run_id'])
                same_binding = run.continuation.get('ontology') == run.ontology.model_dump(mode='json')
                run.continuation['bindings_changed'] = not same_binding
                origin = datetime.fromisoformat(run.continuation['evidence_origin_at'])
                if source and same_binding and (datetime.now(timezone.utc) - origin).total_seconds() <= EVIDENCE_REUSE_TTL_SECONDS:
                    run.evidence = [e.model_copy(deep=True) for e in source.evidence if e.knowledge_base_id in knowledge_base_ids]
                if not run.evidence:
                    run.continuation['evidence_origin_at'] = run.created_at.isoformat()
                self.emit(run, 'conversation.context.restored', RunStatus.UNDERSTANDING,
                          {'summary': '已接续上次需求、补充口径和复核意见；执行授权将重新校验',
                           'source_run_id': run.continuation['source_run_id'], 'reused_evidence': len(run.evidence),
                           'bindings_changed': not same_binding}, 'conversation_owner')
            interpretation={}
            model=getattr(self.analyst,'model',None)
            if model is not None and model.configured and hasattr(model, 'complete_messages'):
                self.emit(run, 'ontology.bound', RunStatus.UNDERSTANDING, {'domain': run.domain, 'version': run.ontology.version, 'data_source_id': data_source_id}, 'ontology')
                # The Analyst now decides whether and when to research knowledge.
                return {'ontology_model': ontology_model}
            if model is not None and model.configured and self.retrieve is None:
                interpretation=await model.complete_json(
                    INITIAL_UNDERSTANDING,
                    json.dumps({'request':self._analysis_question(run),'domain':ontology_model.get('name'),'knowledge_available':bool(knowledge_base_ids)},ensure_ascii=False),
                )
                self.emit(run,'intent.understood',RunStatus.UNDERSTANDING,{'summary':str(interpretation.get('summary') or '已理解查数目标'),'needs_clarification':bool(interpretation.get('clarifications'))},'analyst')
                if interpretation.get('clarifications'):
                    return {'ontology_model':ontology_model,'initial_clarification':interpretation}
            self.emit(
                run,
                "ontology.bound",
                RunStatus.RETRIEVING,
                {
                    "domain": run.domain,
                    "version": run.ontology.version,
                    "data_source_id": data_source_id,
                    "knowledge_base_ids": knowledge_base_ids,
                },
                "ontology",
            )
            try:
                if run.evidence:
                    pass
                elif self.retrieve is not None:
                    run.evidence = await self.retrieve(self._analysis_question(run))
                elif knowledge_base_ids and interpretation.get('requires_knowledge',True):
                    run.knowledge_research = await knowledge_service.research(str(interpretation.get('knowledge_query') or self._analysis_question(run)), knowledge_base_ids)
                    run.evidence = run.knowledge_research.evidence
            except Exception:
                self.emit(run,'knowledge.warning',RunStatus.RETRIEVING,{'message':'知识库暂不可用，继续基于需求和本体分析。'},'knowledge')
        except Exception as exc:
            run.error = f"业务域绑定或知识检索不可用：{exc}"
            self.emit(run, "run.failed", RunStatus.FAILED, {"error": run.error}, "knowledge_core")
            return {}
        if not run.evidence:
            self.emit(run,'knowledge.warning',RunStatus.RETRIEVING,{'message':'未检索到辅助知识，继续基于需求、本体和真实字段分析。'},'knowledge')
        if run.knowledge_research is not None:
            self.emit(
                run,
                "knowledge.research.completed",
                RunStatus.RETRIEVING,
                run.knowledge_research.model_dump(mode="json"),
                run.knowledge_research.provider,
            )
        self.emit(
            run,
            "evidence.collected",
            RunStatus.RETRIEVING,
            {"evidence": [item.model_dump(mode="json") for item in run.evidence]},
            "analyst",
        )
        return {"ontology_model": ontology_model}

    def _analysis_question(self, run: QueryRun) -> str:
        if run.message_mode == 'new':
            return run.question
        if run.continuation:
            c = run.continuation
            return json.dumps({'需求单': c.get('requirement_form', {}), '原需求及后续补充': c['question_history'],
                               '此前确认上下文':c.get('prior_confirmed_context',[]),
                               '已确认口径': c['confirmed_facts'], '历史补充确认': c.get('legacy_confirmations', ''),
                               '本轮补充': run.question}, ensure_ascii=False)
        session=self.store.sessions.get(run.session_id)
        form=session.requirement_form if session else {}
        history=self._confirmed_history(run)
        if not form and not history:
            return run.question
        return json.dumps({'需求单':form,'已确认上下文':history[-3:],'本轮问题':run.question},ensure_ascii=False)

    def _confirmed_history(self, run):
        session=self.store.sessions.get(run.session_id)
        history=[]
        if session:
            for run_id in session.run_ids:
                previous=self.store.runs.get(run_id)
                if previous and previous.id != run.id and previous.message_mode == 'new':
                    history = []
                if previous and previous.id!=run.id and previous.requirement and previous.domain == run.domain and previous.authorization == run.authorization:
                    history.append({'question':previous.question,'confirmed_requirement':previous.requirement.model_dump(mode='json')})
        return history

    async def _analyse_node(self, state: AgentState) -> dict:
        run = self._run(state)
        self.emit(run, "understanding.started", RunStatus.UNDERSTANDING)
        ontology_model = state.get("ontology_model")
        if not ontology_model or run.ontology is None:
            run.error = "会话本体快照缺失，已安全停止分析"
            self.emit(run, "run.failed", RunStatus.FAILED, {"error": run.error}, "ontology")
            return {}
        try:
            scope={
                'branch_ids':run.authorization.branch_ids if run.authorization and (ontology_model.get('scopeField') or os.getenv('AUTH_REQUIRED','false').lower()=='true') else [],
                'cross_organization':run.authorization.cross_organization if run.authorization else False,
            }
            if state.get('initial_clarification'):
                outcome=self.analyst._build_outcome(state['initial_clarification'],self._analysis_question(run),run.evidence,ontology_model,scope,'llm')
            else:
                outcome = await self._analyse_with_tools(run,
                self._analysis_question(run),
                run.evidence,
                ontology_model,
                scope,
            )
        except Exception as exc:
            if isinstance(exc,TaskSupersededError):return self._discard_superseded_task(run)
            label='Reviewer 方案预检失败' if getattr(exc,'role',None)=='reviewer' else 'Analyst 推理失败'
            run.error = f"{label}：{getattr(exc, 'public_message', str(exc))}"
            self.emit(run, "run.failed", RunStatus.FAILED, {"error": run.error}, "analyst")
            return {}
        if outcome.requirement is None:
            run.error = "Analyst 未返回结构化需求，已安全停止分析"
            self.emit(run, "run.failed", RunStatus.FAILED, {"error": run.error}, "analyst")
            return {}
        run.requirement_draft = outcome.requirement
        run.proposed_plan = outcome.plan
        self.emit(
            run,
            "requirement.draft.ready",
            RunStatus.ALIGNING,
            {
                "requirement": run.requirement_draft.model_dump(mode="json"),
                "analyst_mode": outcome.model_mode,
            },
            "analyst",
        )
        return {"ontology_model": ontology_model, 'alignment_initial_draft':run.requirement_draft.model_dump(mode='json')}

    def _agentic_enabled(self, role='analyst') -> bool:
        model = getattr(getattr(self, role), 'model', None)
        return bool(model and model.configured and hasattr(model, 'complete_messages'))

    @staticmethod
    def _rule_reviewer():
        from semora_agent.model import StructuredModel
        model = StructuredModel()
        model.base_url = model.api_key = ''
        model.mode = 'optional'
        return ReviewerAgent(model)

    async def _analyse_with_tools(self, run, question, evidence, ontology, scope, feedback=None):
        self.emit(run, 'analysis.started', RunStatus.UNDERSTANDING, {'summary': '正在结合当前需求与补充信息分析'}, 'analyst')
        if self._agentic_enabled():
            from .agentic import analyse
            return await analyse(self, run, question, ontology, scope, feedback)
        outcome = await self.analyst.analyse(question, evidence, ontology, scope)
        if outcome.plan:
            from .query_stages import validate_query_shape
            validate_query_shape(outcome.plan, ontology)
        return outcome

    async def _align_node(self, state: AgentState) -> dict:
        run = self._run(state)
        draft = run.requirement_draft
        if state.get('alignment_initial_draft'):
            draft=RequirementDraft.model_validate(state['alignment_initial_draft'])
        ontology_model = state.get("ontology_model")
        if draft is None or not ontology_model:
            run.error = "需求理解或本体上下文缺失，无法进入口径确认"
            self.emit(run, "run.failed", RunStatus.FAILED, {"error": run.error}, "conversation_owner")
            return {}

        automatic = not SystemSettings.model_validate(state['settings']).require_plan_confirmation
        if (state.get("auto_align") or automatic) and not draft.clarifications:
            decision = {
                "confirmed": True,
                "answers": {
                    item.id: item.options[0].value if item.options else "测试环境自动确认"
                    for item in draft.clarifications
                },
                "actor": "conversation-owner" if automatic else "test-auto-align",
            }
        else:
            self.emit(
                run,
                "alignment.required",
                RunStatus.CLARIFYING,
                {
                    "requirement": draft.model_dump(mode="json"),
                    "blocking_count": sum(1 for item in draft.clarifications if item.blocking),
                },
                "conversation_owner",
            )
            decision = interrupt(
                {
                    "run_id": run.id,
                    "requirement_draft_version": draft.version,
                    "requirement": draft.model_dump(mode="json"),
                }
            )

        gathered_facts: list[ConfirmedFact] = []
        while True:
            answers = decision.get("answers") if isinstance(decision, dict) else {}
            answers = answers if isinstance(answers, dict) else {}
            missing = [
                item.id
                for item in draft.clarifications
                if item.blocking and not str(answers.get(item.id, "")).strip()
            ]
            if isinstance(decision, dict) and decision.get("confirmed") and not missing:
                if draft.clarifications:
                    for item in draft.clarifications:
                        value=str(answers.get(item.id,'')).strip()
                        selected=next((option for option in item.options if option.id==value or option.value==value),None)
                        gathered_facts.append(ConfirmedFact(question_id=item.id,field=item.field,value=selected.value if selected else value,label=selected.label if selected else value))
                    # Persist answers before starting model work, including work
                    # that later times out or fails preflight.
                    saved = {fact.question_id: fact for fact in run.user_answers}
                    saved.update({fact.question_id: fact for fact in gathered_facts})
                    run.user_answers = list(saved.values())
                    self.store.save_run(run)
                    text='；'.join(f'{item.field}={item.value}' for item in gathered_facts)
                    cache_key=hashlib.sha256(text.encode()).hexdigest()
                    cached=next((item for item in run.alignment_rounds if item['key']==cache_key),None)
                    try:
                        if cached:
                            next_draft=RequirementDraft.model_validate(cached['draft'])
                            next_plan=QueryPlan.model_validate(cached['plan']) if cached['plan'] else None
                        else:
                            outcome=await self._analyse_with_tools(run,self._analysis_question(run)+'。用户补充确认：'+text,run.evidence,ontology_model,draft.data_scope)
                            if outcome.requirement is None:
                                raise ValueError('模型未返回补充分析')
                            next_draft=outcome.requirement
                            next_draft.version=draft.version+1
                            next_plan=outcome.plan
                            run.alignment_rounds.append({'key':cache_key,'answers':[item.model_dump(mode='json') for item in gathered_facts],'draft':next_draft.model_dump(mode='json'),'plan':next_plan.model_dump(mode='json') if next_plan else None})
                    except Exception as exc:
                        if isinstance(exc,TaskSupersededError):return self._discard_superseded_task(run)
                        run.error=f"补充需求分析失败：{getattr(exc, 'public_message', str(exc))}"
                        self.emit(run,'run.failed',RunStatus.FAILED,{'error':run.error},'analyst')
                        return {}
                    draft=next_draft
                    run.requirement_draft=draft
                    run.proposed_plan=next_plan
                    if automatic and not draft.clarifications:
                        self.emit(run, 'alignment.resolved', RunStatus.ALIGNING,
                                  {'summary': '业务歧义已澄清，自动进入SQL生成和独立复核', 'requirement': draft.model_dump(mode='json')}, 'conversation_owner')
                        decision = {'confirmed': True, 'answers': {}, 'actor': decision.get('actor', 'conversation-owner')}
                        continue
                    self.emit(run,'alignment.required',RunStatus.CLARIFYING,{'requirement':draft.model_dump(mode='json'),'message':'请继续补充，或确认更新后的完整口径。'},'conversation_owner')
                    decision=interrupt({'run_id':run.id,'requirement_draft_version':draft.version,'requirement':draft.model_dump(mode='json')})
                    continue
                break
            self.emit(
                run,
                "alignment.required",
                RunStatus.CLARIFYING,
                {
                    "requirement": draft.model_dump(mode="json"),
                    "blocking_count": len(missing),
                    "missing_question_ids": missing,
                    "message": "请先回答所有关键业务口径。",
                },
                "conversation_owner",
            )
            decision = interrupt(
                {
                    "run_id": run.id,
                    "requirement_draft_version": draft.version,
                    "requirement": draft.model_dump(mode="json"),
                    "missing_question_ids": missing,
                }
            )

        confirmed_facts: list[ConfirmedFact] = gathered_facts
        for question in draft.clarifications:
            value = str(answers.get(question.id, "")).strip()
            option = next(
                (item for item in question.options if item.value == value or item.id == value),
                None,
            )
            confirmed_facts.append(
                ConfirmedFact(
                    question_id=question.id,
                    field=question.field,
                    value=option.value if option else value,
                    label=option.label if option else value,
                )
            )

        plan = run.proposed_plan
        if plan is None:
            confirmed_text = "；".join(f"{item.field}={item.value}" for item in confirmed_facts)
            try:
                outcome = await self._analyse_with_tools(run,
                    f"{run.question}。用户已确认：{confirmed_text}",
                    run.evidence,
                    ontology_model,
                    draft.data_scope,
                )
                plan = outcome.plan
            except Exception as exc:
                if isinstance(exc,TaskSupersededError):return self._discard_superseded_task(run)
                run.error = f"根据确认口径重新规划失败：{exc}"
                self.emit(run, "run.failed", RunStatus.FAILED, {"error": run.error}, "analyst")
                return {}
        if plan is None:
            run.error = "确认后的业务口径仍无法映射到已发布本体"
            self.emit(run, "run.failed", RunStatus.FAILED, {"error": run.error}, "analyst")
            return {}

        actor = str(decision.get("actor") or "business-user")
        requirement_payload = {
            "query_intent": plan.query_intent.model_dump(mode="json") if plan.query_intent else None,
            "execution_mode": plan.execution_mode,
            "source_draft_version": draft.version,
            "summary": draft.summary,
            "subject": draft.subject,
            "metrics": draft.metrics or plan.metrics,
            "dimensions": draft.dimensions or plan.dimensions,
            "order_by": [item.model_dump(mode="json") for item in plan.order_by],
            "limit": plan.limit,
            "filters": [item.model_dump(mode="json") for item in (draft.filters or plan.filters)],
            "time_semantics": draft.time_semantics or plan.time_range,
            "grain": draft.grain or plan.grain,
            "data_scope": draft.data_scope,
            "aggregation_rules": draft.aggregation_rules,
            "stages": [stage.model_dump(mode='json') for stage in plan.stages],
            "source_steps": [step.model_dump(mode='json') for step in plan.source_steps],
            "calculations": [calculation.model_dump(mode='json') for calculation in plan.calculations],
            "output_fields": draft.output_fields or plan.fields or plan.dimensions + plan.metrics,
            "assumptions": draft.assumptions,
            "evidence": [item.model_dump(mode="json") for item in draft.evidence],
            "confirmed_facts": [item.model_dump(mode="json") for item in confirmed_facts],
            "confirmed_by": actor,
        }
        if plan.having:requirement_payload['having']=[f.model_dump(mode='json') for f in plan.having]
        if plan.optional_relationships:requirement_payload['optional_relationships']=list(plan.optional_relationships)
        canonical = json.dumps(requirement_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
        content_hash = hashlib.sha256(canonical.encode()).hexdigest()
        run.requirement = RequirementSpec(
            version=f"req-{draft.version}-{content_hash[:10]}",
            content_hash=content_hash,
            **requirement_payload,
        )
        run.plan = plan
        run.proposed_plan = None
        self.emit(
            run,
            "requirement.aligned",
            RunStatus.ALIGNED,
            {"requirement": run.requirement.model_dump(mode="json")},
            actor,
        )

        sensitive_fields = {
            f"{item['name']}.{prop['name']}"
            for item in ontology_model.get("objects", [])
            for prop in item.get("properties", [])
            if prop.get("sensitive")
        }
        requested_fields = set(run.plan.fields + run.plan.dimensions)
        requested_fields.update(item.field for item in run.plan.filters)
        requested_fields.update(item.field for item in run.plan.calculations if item.field)
        for step in run.plan.source_steps:
            requested_fields.update(step.query.fields + step.query.dimensions)
            requested_fields.update(f.field for f in step.query.filters)
            requested_fields.update(c.field for c in step.query.calculations if c.field)
            for binding in step.inputs:
                requested_fields.update(f for pair in binding.pairs for f in (pair.source_field, pair.target_field))
                requested_fields.update(binding.include_source_fields)
            for stage in step.query.stages:
                requested_fields.update(stage.group_by)
                requested_fields.update(m.field for m in stage.measures if m.field)
                requested_fields.update(f.field for f in stage.filters)
                requested_fields.update(f.field for m in stage.measures for f in m.filters)
        for stage in run.plan.stages:
            requested_fields.update(stage.group_by)
            requested_fields.update(item.field for item in stage.measures if item.field)
            requested_fields.update(item.field for item in stage.filters)
            requested_fields.update(item.field for measure in stage.measures for item in measure.filters)
        clearance = run.authorization.sensitivity_clearance if run.authorization else "internal"
        if requested_fields & sensitive_fields and clearance == "internal":
            run.error = "当前身份无权查询敏感字段"
            self.emit(
                run,
                "authorization.denied",
                RunStatus.FAILED,
                {"fields": sorted(requested_fields & sensitive_fields), "clearance": clearance},
                "abac_policy",
            )
            return {"ontology_model": ontology_model}
        self.emit(
            run,
            "plan.ready",
            RunStatus.PLANNING,
            {
                "plan": run.plan.model_dump(mode="json"),
                "requirement_version": run.requirement.version,
            },
            "analyst",
        )
        return {"ontology_model": ontology_model}

    async def _compile_node(self, state: AgentState) -> dict:
        run = self._run(state)
        if run.plan is None:
            run.error = "QueryPlan 缺失，已停止编译"
            self.emit(run, "run.failed", RunStatus.FAILED, {"error": run.error}, "semantic_planner")
            return {}
        self.emit(run, "compilation.started", RunStatus.COMPILING)
        try:
            if run.ontology and run.ontology.artifact_version=='draft':
                raise ValueError('需求已确认；当前本体仍为草稿，请到本体工作室发布后重新生成 SQL')
            if run.plan.source_steps:
                from .source_execution import SourceExecution
                await SourceExecution(self, run, state['ontology_model'], state['settings']).compile_all()
                run.sql_explanation = '跨源步骤先由Reviewer整体复核，程序提取并去重关联键后逐步绑定参数、重新过门禁和只读执行；各数据源不是同一个全局事务。'
                self.emit(run, 'source.plan.compiled', RunStatus.COMPILING,
                          {'steps': {key: value.model_dump(mode='json') for key, value in run.source_artifacts.items()}}, 'semantic_planner')
                self.emit(run, 'sql.ready', RunStatus.COMPILING, {'sql': run.sql.model_dump(mode='json')}, 'semantic_planner')
                return {'needs_revision':False}
            run.sql = await self._compile(run.plan, state["ontology_model"])
            from .planner import adapt_mysql_artifact
            try:
                source = datasource_service.repository.get(run.sql.data_source_id)
            except KeyError:
                source = None
            if source is not None and source.type == "mysql":
                run.sql = adapt_mysql_artifact(run.sql)
        except ValueError as exc:
            if (os.getenv('AGENT_BOUNDED_CONTEXT','false').lower()=='true'
                and not run.plan.query_intent
                and run.requirement is not None and run.ontology is not None
                and run.ontology.artifact_version!='draft' and run.revision_count<3):
                run.review=ReviewDecision(decision='revise',issues=[str(exc)[:1000]],
                    rationale='编译器返回具体校验错误，只修订该执行缺口：'+str(exc)[:1000],reviewer_mode='deterministic')
                self.emit(run,'compilation.revision.required',RunStatus.REVIEWING,
                    {'summary':'编译器发现具体错误，进入有限修订；不重新自由探索','error':str(exc)[:1000]},'semantic_planner')
                return {'needs_revision':True}
            run.error = f"查询方案编译未通过：{exc}"
            self.emit(run, "run.failed", RunStatus.FAILED, {"error": run.error}, "semantic_planner")
            return {}
        except Exception as exc:
            run.error = f"语义规划服务不可用：{exc}"
            self.emit(run, "run.failed", RunStatus.FAILED, {"error": run.error}, "semantic_planner")
            return {}
        if run.requirement is None:
            run.error = "用户确认的需求版本缺失，已停止 SQL 生成"
            self.emit(run, "run.failed", RunStatus.FAILED, {"error": run.error}, "semantic_planner")
            return {}
        run.sql.requirement_version = run.requirement.version
        run.sql.requirement_hash = run.requirement.content_hash
        run.sql.artifact_hash = hashlib.sha256(
            f"{run.sql.artifact_hash}\n{run.requirement.content_hash}".encode()
        ).hexdigest()
        from semora_agent.model import structured_model
        if structured_model.configured:
            from .model_outputs import SQLExplanation
            from .model_context import output_definitions
            try:
                description=await structured_model.complete_json(
                    SQL_EXPLANATION,
                    json.dumps({'requirement':run.requirement.summary,'sql':run.sql.physical_sql,
                        'parameters':run.sql.parameters,'output_definitions':output_definitions(run.plan,state['ontology_model'])},ensure_ascii=False))
                run.sql_explanation=SQLExplanation.model_validate(description).explanation
            except Exception as exc:
                run.sql_explanation='查询SQL已生成，具体口径见确认需求与执行方案。'
                self.emit(run,'sql.explanation.unavailable',RunStatus.COMPILING,
                    {'error_type':type(exc).__name__,'summary':'模型说明暂不可用，保留已编译SQL继续复核'},'conversation_owner')
        self.emit(
            run,
            "sql.ready",
            RunStatus.COMPILING,
            {"sql": run.sql.model_dump(mode="json")},
            "semantic_planner",
        )
        return {'needs_revision':False}

    async def _gate_review_node(self, state: AgentState) -> dict:
        run = self._run(state)
        if run.sql is None or run.plan is None:
            run.error = "SQL 或 QueryPlan 缺失，已停止复核"
            self.emit(run, "run.failed", RunStatus.FAILED, {"error": run.error})
            return {}
        self.emit(run, "gate.started", RunStatus.GATING)
        try:
            from .cost_estimation import estimate_query
            estimate = {'estimated_rows': None, 'source': 'runtime_per_source_step'} if run.plan.source_steps else await estimate_query(self,run,run.sql)
        except Exception as exc:
            run.error = f"查询成本估算失败，已安全停止执行：{exc}"
            self.emit(run, "run.failed", RunStatus.FAILED, {"error": run.error}, "executor")
            return {}
        self.emit(run, "cost.estimated", RunStatus.GATING, estimate, "executor")
        settings = SystemSettings.model_validate(state["settings"])
        if run.plan.source_steps:
            from .source_execution import SourceExecution
            run.gate = SourceExecution(self, run, state['ontology_model'], state['settings']).template_gate()
        else:
            run.gate = review_sql(
                run.sql.physical_sql,
                dialect=run.sql.dialect,
                require_organization_scope=bool(run.plan.data_scope.get('branch_ids')),
                approval_mode=settings.approval_mode,
                sensitive_columns=set(run.sql.sensitive_columns),
                approve_sensitive_detail=settings.approve_sensitive_detail,
                estimated_rows=estimate["estimated_rows"],
                estimate_unavailable=estimate.get('source')=='unavailable',
                large_result_threshold=settings.large_result_threshold,
                approve_large_result=settings.approve_large_result,
                cross_organization=bool(run.plan.data_scope.get("cross_organization", False)),
                approve_cross_organization=settings.approve_cross_organization,
            )
        self.emit(
            run,
            "gate.completed",
            RunStatus.GATING,
            {"gate": run.gate.model_dump(mode="json")},
            "sql_gate",
        )
        self.emit(run, "review.started", RunStatus.REVIEWING)
        from .delivery import best_effort,advisory,unavailable_review,RECOVERABLE_REVIEW_ERRORS
        checks=None
        try:
            # Deterministic requirement/permission checks remain mandatory even
            # when the Reviewer is allowed to investigate with tools.
            if self._agentic_enabled('reviewer'):
                from .agentic import review
                checks = await self._rule_reviewer().review(run.plan, run.gate, run.sql, run.requirement)
                if checks.decision == 'approved':
                    run.review = await review(self, run, state['ontology_model'])
                    if best_effort(run):run.review=advisory(self,run,run.review,'执行前复核')
                else:
                    run.review = checks
            else:
                run.review = await self.reviewer.review(run.plan, run.gate, run.sql, run.requirement)
        except Exception as exc:
            if isinstance(exc,TaskSupersededError):return self._discard_superseded_task(run)
            if isinstance(exc,RECOVERABLE_REVIEW_ERRORS) and best_effort(run) and checks is not None and checks.decision=='approved' and run.gate.allowed:
                run.review=advisory(self,run,unavailable_review(exc),'执行前复核')
            else:
                run.error = f"Reviewer 复核失败：{exc}"
                self.emit(run, "run.failed", RunStatus.FAILED, {"error": run.error}, "reviewer")
                return {}
        self.emit(
            run,
            "review.completed",
            RunStatus.REVIEWING,
            {"review": run.review.model_dump(mode="json")},
            "reviewer",
        )
        if run.review.disposition=='advisory' and run.gate.allowed:
            return {'needs_revision':False}
        if run.review.decision == 'revise' and run.gate.allowed:
            if run.revision_count >= 3:
                run.error = '方案已达到 3 轮修订上限，请补充需求或本体配置后重试'
                self.emit(run, 'run.failed', RunStatus.FAILED, {'error': run.error})
            else:
                self.emit(run, 'review.revision.required', RunStatus.REVIEWING, {'issues': run.review.issues, 'summary': run.review.rationale, 'revision': run.revision_count + 1}, 'reviewer')
                return {'needs_revision': True}
        elif run.review.decision != "approved" or not run.gate.allowed:
            run.error = "SQL 或业务口径复核未通过"
            self.emit(run, "run.failed", RunStatus.FAILED, {"error": run.error})
        return {'needs_revision': False}

    async def _revise_node(self, state: AgentState) -> dict:
        run = self._run(state)
        if run.review is None or run.review.decision != 'revise':
            raise ValueError('缺少有效修订意见')
        if run.plan.query_intent:
            from semora_agent.review_policy import intent_repair_route
            if intent_repair_route(run.review)=='technical':
                if run.revision_count>=3:
                    run.error='执行方案经过3轮内部修复仍未通过复核：'+'；'.join(run.review.issues)[:900]
                    self.emit(run,'planning.technical_gap',RunStatus.FAILED,{'summary':run.error,'issues':run.review.issues},'conversation_owner')
                    return {'needs_revision':False,'internal_repair':False}
                run.agent_memory['owner_repair_diagnosis']={
                    'original_review':run.review.model_dump(mode='json'),'operation':'recompile_frozen_plan',
                    'business_intent_unchanged':True,'instruction':'重新核对具体SQL及执行契约，确认问题是否仍成立；不能无证据放行。'}
                run.revision_count+=1
                if run.approval:run.approval.status='expired'
                run.sql=run.gate=run.review=run.approval=None
                run.result=run.result_review=run.sql_explanation=run.answer=None
                run.source_artifacts={};run.source_results={}
                from .control_state import invalidate_execution
                invalidate_execution(run)
                self.emit(run,'plan.repaired',RunStatus.PLANNING,{'revision':run.revision_count,
                    'summary':'保留已确认口径，内部重新编译SQL并定向复核'},'conversation_owner')
                return {'needs_revision':False,'internal_repair':True}
        feedback = run.review.model_dump(mode='json')
        self.emit(run, 'analysis.revision.started', RunStatus.UNDERSTANDING, {'summary': 'Analyst 根据独立复核意见重新分析', 'revision': run.revision_count + 1}, 'analyst')
        try:
            request = self._analysis_question(run)
            if not run.plan.query_intent:
                request += '。独立复核修改意见：' + json.dumps(feedback, ensure_ascii=False)
            outcome = await self._analyse_with_tools(run, request, run.evidence, state['ontology_model'], run.plan.data_scope, feedback)
            if outcome.requirement is None:
                raise ValueError('修订未形成需求草稿')
            outcome.requirement.version = (run.requirement_draft.version if run.requirement_draft else 0) + 1
        except Exception as exc:
            if isinstance(exc,TaskSupersededError):return self._discard_superseded_task(run)
            run.error = f'方案修订失败：{exc}'
            self.emit(run, 'run.failed', RunStatus.FAILED, {'error': run.error}, 'analyst')
            return {'needs_revision': False}
        run.revision_count += 1
        if run.approval:
            run.approval.status = 'expired'
        from semora_agent.constraints import compatible_repair
        old_plan = run.plan
        candidate = outcome.plan
        same_execution = {'metrics', 'dimensions', 'fields', 'filters', 'grain', 'time_range', 'calculations', 'stages', 'source_steps', 'relationship_path', 'data_scope','order_by','limit','having','optional_relationships'}
        if candidate and old_plan.model_dump(include=same_execution) == candidate.model_dump(include=same_execution):
            run.error = '修订未改变执行方案，已停止重复生成；请根据复核原因补充口径或本体配置'
            self.emit(run, 'run.failed', RunStatus.FAILED, {'error': run.error}, 'conversation_owner')
            return {'needs_revision': False, 'internal_repair': False}
        sensitive_fields = {f"{obj['name']}.{prop['name']}" for obj in state['ontology_model'].get('objects', []) for prop in obj.get('properties', []) if prop.get('sensitive')}
        safe_filters = candidate and not ({f.field for f in candidate.filters} - {f.field for f in old_plan.filters}) & sensitive_fields
        if candidate and not old_plan.query_intent and safe_filters and run.requirement and compatible_repair(old_plan, candidate, run.requirement, state['ontology_model']):
            # Preserve the user's immutable meaning, but invalidate every downstream
            # artifact and approval. Compilation, gate and independent review rerun.
            run.plan = candidate
            run.proposed_plan = None
            run.sql = run.gate = run.review = None
            run.approval = run.result = run.result_review = None
            run.sql_explanation = run.answer = None
            self.emit(run, 'plan.repaired', RunStatus.PLANNING, {'plan': candidate.model_dump(mode='json'), 'revision': run.revision_count, 'requirement_version': run.requirement.version, 'summary': '已按用户确认的业务条件内部修订，重新编译并独立复核'}, 'conversation_owner')
            return {'needs_revision': False, 'internal_repair': True}
        run.requirement_draft, run.proposed_plan = outcome.requirement, outcome.plan
        if old_plan.query_intent:
            run.source_artifacts = {}
            run.source_results = {}
            from .control_state import invalidate_execution
            invalidate_execution(run)
        # Every revised proposal is re-confirmed. No old confirmation, review,
        # approval, SQL or execution artifact can authorize the new version.
        run.requirement = run.plan = run.sql = run.gate = run.review = None
        run.approval = run.result = run.result_review = None
        run.sql_explanation = run.answer = None
        self.emit(run, 'requirement.revised', RunStatus.ALIGNING, {'requirement': run.requirement_draft.model_dump(mode='json'), 'revision': run.revision_count}, 'analyst')
        return {'needs_revision': False, 'internal_repair': False, 'initial_clarification': None, 'alignment_initial_draft': run.requirement_draft.model_dump(mode='json')}

    async def _approval_node(self, state: AgentState, *, source_step=False) -> dict:
        run = self._run(state)
        if run.plan and run.plan.source_steps and not source_step:
            # Runtime key parameters are unknown until A completes. Approval,
            # if enabled, must bind the actual SQL/parameters of each step.
            return {}
        assert run.gate is not None and run.sql is not None
        settings = SystemSettings.model_validate(state["settings"])
        approval_mode = settings.approval_mode
        requires_approval = approval_mode == "all" or run.gate.requires_approval
        if not requires_approval or approval_mode == "disabled":
            return {}
        if run.approval is None:
            run.approval = ApprovalDecision(
                run_id=run.id,
                policy_version=settings.policy_version,
                sql_hash=run.sql.artifact_hash,
                **self._approval_binding(run, settings.policy_version),
            )
            self.store.approvals[run.approval.id] = run.approval
            self.emit(
                run,
                "approval.required",
                RunStatus.AWAITING_APPROVAL,
                {"approval": run.approval.model_dump(mode="json")},
                "approval_policy",
            )
        decision = interrupt(
            {
                "run_id": run.id,
                "approval_id": run.approval.id,
                "sql_hash": run.approval.sql_hash,
                "risk_level": run.gate.risk_level,
            }
        )
        if run.approval.sql_hash != run.sql.artifact_hash:
            run.approval.status = "expired"
            self.store.save_run(run)
            raise ValueError("SQL 已变化，原审批已失效")
        approved = bool(decision.get("approved"))
        run.approval.status = "approved" if approved else "rejected"
        run.approval.actor = str(decision.get("actor") or "unknown")
        run.approval.reason = str(decision.get("reason") or "")
        run.approval.decided_at = datetime.now(timezone.utc)
        self.emit(
            run,
            "approval.completed",
            RunStatus.AWAITING_APPROVAL,
            {"approval": run.approval.model_dump(mode="json")},
            run.approval.actor,
        )
        if not approved:
            run.error = "人工审批拒绝执行"
            self.emit(run, "run.failed", RunStatus.FAILED, {"error": run.error})
        return {}

    async def _execute_node(self, state: AgentState) -> dict:
        run = self._run(state)
        if run.plan and run.plan.source_steps:
            from .source_execution import SourceExecution
            from langgraph.errors import GraphInterrupt
            execution=None
            try:
                execution=SourceExecution(self, run, state['ontology_model'], state['settings'])
                await execution.execute(state)
            except GraphInterrupt:
                raise
            except Exception as exc:
                run.result = None
                from .executor_errors import ExecutorOperationError
                if isinstance(exc,ExecutorOperationError):run.agent_memory['execution_failure']=[exc.public()]
                detail=str(exc).strip() or type(exc).__name__
                step=getattr(execution,'active_step',None)
                operation=getattr(execution,'active_operation',None)
                location=' / '.join(v for v in (step,operation) if v)
                run.error = f'跨源查询未完成：{location}：{detail}'
                self.emit(run, 'run.failed', RunStatus.FAILED, {'error': run.error,'step':step,'operation':operation,'error_type':type(exc).__name__}, 'executor')
            return {}
        assert run.sql is not None
        self.emit(run, "execution.started", RunStatus.EXECUTING, actor="executor")
        try:
            run.result = await self.execute(
                run.sql.physical_sql,
                run.sql.parameters,
                run.sql.sensitive_columns,
                {
                    **state["settings"],
                    "execution_id": run.id,
                    "data_source_id": run.sql.data_source_id,
                },
            )
        except Exception as exc:
            run.error = f"执行失败：{exc}"
            from .executor_errors import ExecutorOperationError
            if isinstance(exc,ExecutorOperationError):run.agent_memory['execution_failure']=[exc.public()]
            self.emit(run, "run.failed", RunStatus.FAILED, {"error": run.error}, "executor")
            return {}
        self.emit(
            run,
            "execution.completed",
            RunStatus.EXECUTING,
            {"result": run.result.model_dump(mode="json")},
            "executor",
        )
        return {}

    async def _explain_node(self, state: AgentState) -> dict:
        run = self._run(state)
        assert run.result is not None
        self.emit(run, "explanation.started", RunStatus.EXPLAINING)
        assert run.plan is not None
        from .delivery import best_effort,advisory,unavailable_review,RECOVERABLE_REVIEW_ERRORS
        checks=None
        try:
            if self._agentic_enabled('reviewer'):
                from .agentic import review
                checks = await self._rule_reviewer().review_result(run.plan, run.result)
                run.result_review = await review(self, run, state['ontology_model'], 'post_execution') if checks.decision == 'approved' else checks
                if checks.decision=='approved' and best_effort(run):run.result_review=advisory(self,run,run.result_review,'结果复核')
            else:
                run.result_review = await self.reviewer.review_result(run.plan, run.result)
        except Exception as exc:
            if isinstance(exc,TaskSupersededError):return self._discard_superseded_task(run)
            if isinstance(exc,RECOVERABLE_REVIEW_ERRORS) and best_effort(run) and checks is not None and checks.decision=='approved':
                run.result_review=advisory(self,run,unavailable_review(exc,'post_execution'),'结果复核')
            else:
                run.error = f"Reviewer 结果复核失败：{exc}"
                self.emit(run, "run.failed", RunStatus.FAILED, {"error": run.error}, "reviewer")
                return {}
        self.emit(
            run,
            "result_review.completed",
            RunStatus.EXPLAINING,
            {"review": run.result_review.model_dump(mode="json")},
            "reviewer",
        )
        if run.result_review.decision != "approved" and run.result_review.disposition!='advisory':
            run.error = "执行结果复核未通过，已禁止形成业务答案"
            self.emit(run, "run.failed", RunStatus.FAILED, {"error": run.error}, "reviewer")
            return {}
        if run.plan and run.plan.fields:
            run.answer = f"查询返回 {run.result.row_count} 条结果。"
            if run.result.summary.get('masked_columns'):
                run.answer+=' 已配置的敏感字段已由执行器脱敏。'
        else:
            run.answer = self._aggregate_answer(run, state['ontology_model'])
        from semora_agent.model import structured_model
        if structured_model.configured:
            from .task_handoff import revision
            from .model_context import business_requirement, output_definitions
            from .model_outputs import AnswerExplanation
            summary_revision=revision(run)
            try:
                explanation=await structured_model.complete_json(
                    ANSWER_EXPLANATION,
                    json.dumps({'business_domain':{key:state['ontology_model'].get(key) for key in ('domain','name','description')},'requirement':business_requirement(run),'output_definitions':output_definitions(run.plan,state['ontology_model']),'columns':run.result.columns,'rows':run.result.rows[:100],'row_count':run.result.row_count,'truncated':run.result.truncated,'sample_rows':min(100,len(run.result.rows)),
                        'source_execution_receipts':[{'step':s.name,'row_count':run.source_results[s.name].row_count,'truncated':run.source_results[s.name].truncated} for s in run.plan.source_steps if s.name in run.source_results]},ensure_ascii=False,default=str),
                )
                if revision(run)!=summary_revision:return self._discard_superseded_task(run)
                run.answer=AnswerExplanation.model_validate(explanation).answer
            except Exception as exc:
                if revision(run)!=summary_revision:return self._discard_superseded_task(run)
                run.answer+=' 模型说明暂不可用；以上为实际查询结果。'
                self.emit(run,'answer.explanation.unavailable',RunStatus.EXPLAINING,{'error_type':type(exc).__name__,'summary':'已保留实际查询结果，未生成额外模型结论'},'conversation_owner')
        omissions=run.plan.query_intent.unfulfilled_goals if run.plan.query_intent else []
        if omissions:run.answer+=' 本次未完成：'+'；'.join(omissions)+'。'
        if run.delivery_warnings:run.answer+=' 当前结果附有待核对项，可继续对话调整口径。'
        self.emit(run, "answer.completed", RunStatus.COMPLETED, {"answer": run.answer})
        return {}

    def _config(self, run_id: str) -> dict:
        return {"configurable": {"thread_id": run_id}, "recursion_limit": 30}

    def _prepare_run(
        self,
        session_id: str,
        question: str,
        domain: str,
        approval_mode: str | None,
        authorization: AuthorizationContext | None,
        mode: str = 'auto',
    ) -> tuple[QueryRun, SystemSettings]:
        authorization = authorization or AuthorizationContext(
            subject="test-user",
            username="测试分析员",
            roles=["analyst"],
            branch_ids=[1, 2, 3],
            sensitivity_clearance="confidential",
        )
        if mode not in {'auto', 'continue', 'new'}:
            raise ValueError('未知消息模式')
        if mode == 'auto' and re.match(r'^\s*(?:新需求|新的需求|换个需求|换一个需求|另一个需求|另起一个需求)(?:[：:，,\s]|$)', question):
            mode = 'new'
        run = QueryRun(session_id=session_id, domain=domain, question=question, authorization=authorization, message_mode=mode)
        session = self.store.sessions[session_id]
        source = self.store.runs.get(session.run_ids[-1]) if session.run_ids else None
        unchanged_retry=question.strip().rstrip('。！!') in {'重试','继续','再试一次','重新执行','按原口径继续'}
        # Recover a legacy retry that was incorrectly started as a fresh attempt.
        # Never cross an explicit new-demand boundary or authorization change.
        if mode!='new' and unchanged_retry and source and source.message_mode!='new' and not source.continuation and not source.user_answers and source.question.strip().rstrip('。！!') in {'重试','继续','再试一次','重新执行','按原口径继续'}:
            for previous_id in reversed(session.run_ids[:-1]):
                previous=self.store.runs[previous_id]
                if previous.domain!=domain or previous.authorization!=authorization:break
                if previous.requirement is not None and previous.plan is not None:
                    source=previous;break
                if previous.message_mode=='new':break
        if mode != 'new' and source and (source.status == RunStatus.FAILED or (source.status==RunStatus.COMPLETED and (mode=='continue' or unchanged_retry))):
            if source.domain == domain and source.authorization == authorization:
                run.continuation = handoff(source, session)
                prior=self._confirmed_history(source) if source.message_mode != 'new' else []
                if prior:
                    run.continuation['prior_confirmed_context']=prior[-3:]
                    if not run.continuation.get('confirmed_requirement'):
                        run.continuation['confirmed_requirement']=prior[-1]['confirmed_requirement']
                run.user_answers = copy.deepcopy(source.user_answers)
            else:
                # Changed permissions must never inherit old field/evidence data.
                run.message_mode = 'new'
        settings = settings_service.get()
        # Kept only for unit/integration callers. The public API deliberately
        # does not expose this override to query users.
        if approval_mode is not None:
            settings.approval_mode = approval_mode
        self.store.add_run(run)
        self.emit(
            run,
            "run.started",
            RunStatus.RECEIVED,
            {
                "question": question,
                "domain": domain,
                "authorization": authorization.model_dump(mode="json"),
            },
            authorization.username,
        )
        if run.continuation:
            self.emit(run, 'conversation.continued', RunStatus.RECEIVED,
                      {'source_run_id': source.id, 'summary': '继续上次失败的需求分析，保留已确认口径，不沿用旧SQL或审批'}, 'conversation_owner')
        if not authorization.branch_ids:
            run.error = "当前身份没有可查询的机构数据范围"
            self.emit(
                run,
                "authorization.denied",
                RunStatus.FAILED,
                {"reason": "empty organization scope"},
                "abac_policy",
            )
        return run, settings

    async def _invoke(self, run: QueryRun, settings: SystemSettings, *, auto_align: bool = False) -> QueryRun:
        run.agent_memory['delivery_policy']=[{'mode':settings.delivery_mode,'policy_version':settings.policy_version}]
        if run.status == RunStatus.FAILED:
            return run
        with traced_operation(
            "semora.query.run",
            {
                "semora.run_id": run.id,
                "semora.session_id": run.session_id,
                "semora.domain": run.domain,
                "semora.audit_trace_id": run.trace_id,
            },
        ):
            try:
                await self.graph.ainvoke(
                    {
                        "run_id": run.id,
                        "approval_mode": settings.approval_mode,
                        "settings": settings.model_dump(mode="json"),
                        "auto_align": auto_align,
                    },
                    config=self._config(run.id),
                )
            except asyncio.CancelledError:
                if run.status not in {RunStatus.COMPLETED, RunStatus.FAILED}:
                    if run.agent_memory.get('user_cancelled'):run.error = "查询已由用户取消"
                    else:
                        run.agent_memory['interruption']=[{'reason':'task_cancelled_without_user_request'}]
                        run.error='服务处理被中断；已有口径和产物已保留，可以继续。'
                    self.emit(
                        run,
                        "execution.cancelled",
                        RunStatus.FAILED,
                        {"error": run.error},
                        "conversation_owner",
                    )
                raise
            except Exception as exc:
                if run.status not in {RunStatus.FAILED, RunStatus.AWAITING_APPROVAL}:
                    run.error = f"工作流编排失败：{exc}"
                    self.emit(run, "run.failed", RunStatus.FAILED, {"error": run.error}, "conversation_owner")
        return self.store.runs[run.id]

    async def start(
        self,
        session_id: str,
        question: str,
        approval_mode: str | None = None,
        authorization: AuthorizationContext | None = None,
        domain: str = "retail_credit",
    ) -> QueryRun:
        run, settings = self._prepare_run(session_id, question, domain, approval_mode, authorization)
        return await self._invoke(run, settings, auto_align=True)

    async def start_background(
        self,
        session_id: str,
        question: str,
        authorization: AuthorizationContext,
        domain: str = "retail_credit",
        mode: str = 'auto',
    ) -> QueryRun:
        run, settings = self._prepare_run(session_id, question, domain, None, authorization, mode)
        if run.status != RunStatus.FAILED:
            task = asyncio.create_task(self._invoke(run, settings, auto_align=False), name=f"semora-run-{run.id}")
            self._tasks[run.id] = task
            def completed(finished):
                if self._tasks.get(run.id) is finished:
                    self._tasks.pop(run.id, None)
            task.add_done_callback(completed)
        return run

    async def cancel(self, run_id: str) -> dict:
        run = self.store.runs[run_id]
        if run.status in {RunStatus.COMPLETED, RunStatus.FAILED}:
            return {
                "run_id": run_id,
                "accepted": False,
                "executor_cancelled": False,
                "task_cancelled": False,
                "status": run.status,
            }
        if run.status in {RunStatus.AWAITING_APPROVAL, RunStatus.CLARIFYING}:
            if run.approval and run.approval.status == "pending":
                run.approval.status = "expired"
            run.error = "查询已由用户取消"
            self.emit(
                run,
                "execution.cancelled",
                RunStatus.FAILED,
                {"error": run.error},
                "conversation_owner",
            )
            return {
                "run_id": run_id,
                "accepted": True,
                "executor_cancelled": False,
                "task_cancelled": False,
                "status": run.status,
            }
        executor_cancelled = False
        executor_cancel_error = None
        task = self._tasks.get(run_id)
        task_cancelled = bool(task and not task.done())
        # Stop scheduling further source queries before waiting on the remote
        # cancellation endpoint, which may be unavailable or slow.
        if task_cancelled:
            run.agent_memory['user_cancelled']=[{'requested':True}]
            task.cancel()
        if run.status == RunStatus.EXECUTING:
            url = os.getenv("EXECUTOR_URL", "http://localhost:8002")
            try:
                async with httpx.AsyncClient(timeout=5) as client:
                    response = await client.post(
                        f"{url}/internal/v1/executions/{run_id}/cancel",
                        headers={"X-Semora-Internal-Token": os.getenv("INTERNAL_TOKEN", "semora-dev-internal")},
                    )
                    response.raise_for_status()
                    executor_cancelled = bool(response.json().get("accepted"))
            except (httpx.HTTPError, ValueError, AttributeError) as exc:
                executor_cancel_error = type(exc).__name__
        if task_cancelled:
            try:
                await task
            except asyncio.CancelledError:
                pass
        return {
            "run_id": run_id,
            "accepted": executor_cancelled or task_cancelled,
            "executor_cancelled": executor_cancelled,
            "executor_cancel_error": executor_cancel_error,
            "task_cancelled": task_cancelled,
            "status": self.store.runs[run_id].status,
        }

    async def approve(self, run: QueryRun, actor: str, approved: bool, reason: str) -> QueryRun:
        if not run.approval or run.status != RunStatus.AWAITING_APPROVAL:
            raise ValueError("该查询当前不处于待审批状态")
        if not run.sql or run.approval.sql_hash != run.sql.artifact_hash:
            run.approval.status = "expired"
            self.store.save_run(run)
            raise ValueError("SQL 已变化，原审批已失效")
        current_policy = settings_service.get().policy_version
        if run.approval.policy_version != current_policy:
            run.approval.status = "expired"
            self.store.save_run(run)
            raise ValueError("审批策略已变化，原审批已失效")
        expected = self._approval_binding(run, run.approval.policy_version)
        if any(getattr(run.approval, key) != value for key, value in expected.items()):
            run.approval.status = "expired"
            self.store.save_run(run)
            raise ValueError("本体版本或权限范围已变化，原审批已失效")
        await self.graph.ainvoke(
            Command(resume={"approved": approved, "actor": actor, "reason": reason}),
            config=self._config(run.id),
        )
        return self.store.runs[run.id]

    async def align(
        self,
        run: QueryRun,
        actor: str,
        answers: dict[str, str],
    ) -> QueryRun:
        if run.status != RunStatus.CLARIFYING or run.requirement_draft is None:
            raise ValueError("该查询当前不处于需求对齐状态")
        await self.graph.ainvoke(
            Command(resume={"confirmed": True, "answers": answers, "actor": actor}),
            config=self._config(run.id),
        )
        return self.store.runs[run.id]

    def start_alignment(self, run: QueryRun, actor: str, answers: dict[str, str]) -> QueryRun:
        if run.status != RunStatus.CLARIFYING or run.requirement_draft is None:
            raise ValueError('该查询当前不处于需求对齐状态，请查看任务的最新结果')
        missing = [q.id for q in run.requirement_draft.clarifications if q.blocking and not answers.get(q.id, '').strip()]
        if missing:
            raise ValueError('请先回答所有必填的澄清问题')
        run.agent_memory['pending_alignment'] = [{'actor': actor, 'answers': answers}]
        self.emit(run, 'alignment.submitted', RunStatus.UNDERSTANDING, {'summary': '已收到补充，后台继续分析', 'draft_version': run.requirement_draft.version}, 'analyst')
        self._queue_alignment(run, actor, answers)
        return run

    def _queue_alignment(self, run, actor, answers):
        async def resume():
            try:
                await self.graph.ainvoke(Command(resume={'confirmed': True, 'answers': answers, 'actor': actor}), config=self._config(run.id))
            except asyncio.CancelledError:
                if run.status not in {RunStatus.COMPLETED, RunStatus.FAILED}:
                    if run.agent_memory.get('user_cancelled'):run.error = '补充分析已取消'
                    else:
                        run.agent_memory['interruption']=[{'reason':'alignment_task_interrupted'}]
                        run.error='补充分析被中断；已保存回答，可以继续当前需求。'
                    self.emit(run, 'run.failed', RunStatus.FAILED, {'error': run.error})
                raise
            except Exception as exc:
                run.error = f'补充需求后台分析失败：{exc}'
                self.emit(run, 'run.failed', RunStatus.FAILED, {'error': run.error}, 'analyst')
            finally:
                run.agent_memory.pop('pending_alignment', None)
                self.store.save_run(run)
        task = asyncio.create_task(resume(), name=f'semora-alignment-{run.id}')
        self._tasks[run.id] = task
        def completed(finished):
            if self._tasks.get(run.id) is finished:
                self._tasks.pop(run.id, None)
        task.add_done_callback(completed)

    def resume_pending_alignments(self):
        for run in self.store.runs.values():
            pending = run.agent_memory.get('pending_alignment')
            if pending and run.status not in {RunStatus.COMPLETED, RunStatus.FAILED}:
                self._queue_alignment(run, pending[0]['actor'], pending[0]['answers'])
            elif run.status not in {RunStatus.COMPLETED,RunStatus.FAILED,RunStatus.CLARIFYING,RunStatus.AWAITING_APPROVAL}:
                run.agent_memory['interruption']=[{'reason':'process_restart_without_running_task'}]
                run.error='上次处理因服务重启中断；已保存需求和已有产物，请继续当前需求。'
                self.emit(run,'run.interrupted',RunStatus.FAILED,{'summary':run.error,'recoverable':True},'conversation_owner')

    @staticmethod
    def _aggregate_answer(run: QueryRun, ontology: dict | None = None) -> str:
        assert run.result is not None
        if not run.result.rows:
            return "查询已完成，在当前时间、过滤条件和授权数据范围内没有匹配数据。完整口径、SQL 和复核记录见本次查询详情。"
        ontology = ontology or {}
        labels = {item['name']: item.get('label') or item['name'] for item in ontology.get('metrics', [])}
        labels.update({item.name: item.label or item.name for item in run.plan.calculations} if run.plan else {})
        for obj in ontology.get('objects', []):
            for prop in obj.get('properties', []):
                labels[f"{obj['name']}_{prop['name']}"] = prop.get('label') or prop['name']
        percentage_columns = {item['name'] for item in ontology.get('metrics', []) if item.get('format') == 'percentage'}
        summaries: list[str] = []
        for row in run.result.rows[:2]:
            values: list[str] = []
            for column, value in zip(run.result.columns, row, strict=False):
                suffix = "%" if column in percentage_columns and value is not None else ""
                values.append(f"{labels.get(column, column)} {value}{suffix}")
            summaries.append("，".join(values))
        detail = "；".join(summaries)
        return f"已按查询口径和授权数据范围完成统计。{detail}。完整口径、SQL 和复核记录见本次查询详情。"

    def _datasource_payload(self, data_source_id: str) -> dict[str, str]:
        try:
            item = datasource_service.repository.get(data_source_id)
        except KeyError as exc:
            raise RuntimeError("本体绑定的数据源未登记") from exc
        if item.status != "healthy" or item.access != "read_only":
            raise RuntimeError("本体绑定的数据源尚未通过只读连接验证")
        return {"datasource_id": item.id, "type": item.type, "secret_ref": item.secret_ref}

    async def _remote_execute(
        self,
        sql: str,
        parameters: dict,
        sensitive_columns: list[str],
        settings: dict,
    ) -> ExecutionResult:
        from .executor_errors import request_executor,ExecutorOperationError
        url = os.getenv("EXECUTOR_URL", "http://localhost:8002")
        async with httpx.AsyncClient(timeout=35) as client:
            response = await request_executor(client,
                f"{url}/internal/v1/query", operation="query",
                json={
                    "sql": sql,
                    "parameters": parameters,
                    "sensitive_columns": sensitive_columns,
                    "timeout_ms": int(settings["query_timeout_seconds"]) * 1000,
                    "max_rows": int(settings["max_result_rows"]),
                    "max_bytes": int(settings["max_result_mb"]) * 1024 * 1024,
                    "execution_id": settings.get("execution_id"),
                    "datasource": self._datasource_payload(str(settings.get("data_source_id") or "")),
                },
                headers={"X-Semora-Internal-Token": os.getenv("INTERNAL_TOKEN", "semora-dev-internal")},
            )
            return ExecutionResult.model_validate(response.json())

    async def _remote_estimate(self, sql: str, parameters: dict, data_source_id: str) -> dict:
        from .executor_errors import request_executor,ExecutorOperationError
        url = os.getenv("EXECUTOR_URL", "http://localhost:8002")
        async with httpx.AsyncClient(timeout=10) as client:
            response = await request_executor(client,
                f"{url}/internal/v1/estimate", operation="estimate",
                json={
                    "sql": sql,
                    "parameters": parameters,
                    "timeout_ms": 5_000,
                    "datasource": self._datasource_payload(data_source_id),
                },
                headers={"X-Semora-Internal-Token": os.getenv("INTERNAL_TOKEN", "semora-dev-internal")},
            )
            try:value = response.json()
            except ValueError as exc:raise ExecutorOperationError() from exc
            if not isinstance(value,dict) or "estimated_rows" not in value:
                raise ExecutorOperationError()
            return value

    @staticmethod
    async def _static_estimate(_: str, __: dict, ___: str) -> dict:
        # Injected test executors do not own a database planner.
        return {"estimated_rows": 1_000, "total_cost": 0.0, "source": "test-static"}

    @staticmethod
    def _approval_binding(run: QueryRun, policy_version: str) -> dict[str, str]:
        assert run.sql is not None and run.ontology is not None and run.plan is not None
        permission_payload = (
            run.authorization.model_dump(mode="json")
            if run.authorization is not None
            else run.plan.data_scope
        )
        permission_json = json.dumps(
            permission_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        permission_hash = hashlib.sha256(permission_json.encode()).hexdigest()
        ontology_hash = run.ontology.content_hash
        binding_payload = "\n".join(
            [run.sql.artifact_hash, ontology_hash, permission_hash, policy_version]
        )
        return {
            "ontology_hash": ontology_hash,
            "permission_hash": permission_hash,
            "binding_hash": hashlib.sha256(binding_payload.encode()).hexdigest(),
        }
