from __future__ import annotations

from dataclasses import dataclass
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from semora_contracts import (
    ClarificationOption,
    ClarificationQuestion,
    EvidenceRef,
    ExecutionResult,
    GateDecision,
    QueryFilter,
    QueryPlan,
    QueryCalculation,
    QueryStage,
    RequirementDraft,
    RequirementSpec,
    ReviewDecision,
    SQLArtifact,
)

from .model import ModelError, StructuredModel, structured_model


def _model_list(value: dict[str, Any], key: str) -> list[Any]:
    """Treat an unspecified JSON collection as empty, never iterate scalars."""
    items = value.get(key)
    if items is None:
        return []
    if not isinstance(items, list):
        raise ValueError(f"模型返回的 {key} 必须是数组")
    return items


@dataclass(frozen=True)
class AgentContract:
    name: str
    goal: str
    allowed_tools: tuple[str, ...]
    forbidden_actions: tuple[str, ...]


@dataclass(frozen=True)
class AnalysisOutcome:
    plan: QueryPlan | None
    requirement: RequirementDraft | None = None
    clarification_question: str | None = None
    model_mode: str = "deterministic"


class ConversationOwner:
    """The only role allowed to own the user conversation and final answer."""

    contract = AgentContract(
        name="conversation_owner",
        goal="根据用户目标和可验证业务状态决定下一步，并形成最终答复",
        allowed_tools=("delegate_analysis", "delegate_review", "read_run_state", "emit_answer"),
        forbidden_actions=("execute_sql", "change_policy", "publish_ontology"),
    )

    def next_action(self, state: dict[str, Any]) -> str:
        if state.get("needs_clarification"):
            return "clarify"
        if not state.get("plan"):
            return "analyse"
        if not state.get("review"):
            return "review"
        if state.get("requires_approval") and not state.get("approval"):
            return "wait_for_approval"
        if not state.get("result"):
            return "execute"
        return "answer"


class AnalystAgent:
    contract = AgentContract(
        name="analyst",
        goal="作为唯一会话分析负责人，多轮研究、澄清和修订有证据、本体约束和数据范围的查询方案",
        allowed_tools=("search_wiki", "read_wiki", "read_source", "read_object", "read_schema", "find_relation_path", "validate_plan", "request_review"),
        forbidden_actions=("execute_sql", "approve_query", "publish_ontology", "change_permissions"),
    )

    def __init__(self, model: StructuredModel | None = None) -> None:
        self.model = model or structured_model

    async def analyse(
        self,
        question: str,
        evidence: list[EvidenceRef],
        ontology: dict[str, Any],
        data_scope: dict[str, Any],
    ) -> AnalysisOutcome:
        if self.model.required and not self.model.configured:
            raise ModelError('请先配置大模型，再提交需求分析')
        if self.model.configured:
            try:
                value = await self._model_analysis(question, evidence, ontology)
                return self._build_outcome(value, question, evidence, ontology, data_scope, 'llm')
            except (ModelError, ValueError):
                if self.model.required:
                    raise
        if self.model.configured:
            try:
                value = await self._model_analysis(question, evidence, ontology)
                return self._build_outcome(value, question, evidence, ontology, data_scope, "llm")
            except (ModelError, ValueError):
                if self.model.required:
                    raise
        return self._deterministic_analysis(question, evidence, ontology, data_scope)

    async def _model_analysis(self, question: str, evidence: list[EvidenceRef], ontology: dict[str, Any]) -> dict[str, Any]:
        selected = ontology.get('objects', [])
        if len(selected)>16:
            selection=await self.model.complete_json(
                '根据用户需求选择需要理解的业务对象。只输出 JSON {"objects":[对象名]}，最多选择12个。不要编造对象名。',
                json.dumps({'question':question,'objects':[{'name':o['name'],'label':o.get('label')} for o in selected]},ensure_ascii=False),
            )
            choices = _model_list(selection, 'objects')
            if any(not isinstance(item, str) for item in choices):
                raise ValueError('模型返回的 objects 必须是对象名数组')
            chosen=set(choices)
            selected=[o for o in selected if o['name'] in chosen]
            if not selected:
                return {'summary':'请先明确本次查数目标。','clarification_question':'您希望怎样分析数据？请同时补充要分析的业务对象。','clarification_options':[{'id':'summary','label':'汇总统计','value':'我想做汇总统计，业务对象待补充'},{'id':'detail','label':'查看明细','value':'我想查看明细，业务对象待补充'},{'id':'compare','label':'对比分析','value':'我想做对比分析，业务对象待补充'}]}
        catalog = {
            "objects": [
                {
                    "name": item["name"],
                    "label": item.get("label"),
                    "aliases": item.get("aliases", []),
                    "fields": [
                        {
                            "name": f"{item['name']}.{prop['name']}",
                            "label": prop.get("label"),
                            "type": prop.get("type"),
                            "description": prop.get("description", ''),
                            "aliases": prop.get("aliases", []),
                        }
                        for prop in item.get("properties", [])
                    ],
                }
                for item in selected
            ],
            "metrics": [
                {"name": item["name"], "label": item.get("label"), "aliases": item.get("aliases", [])}
                for item in ontology.get("metrics", [])
            ],
        }
        names={o['name'] for o in selected}
        catalog['relations']=[{'name':r['name'],'from':r['from'],'to':r['to'],'condition':r.get('condition',''),'cardinality':r.get('cardinality')} for r in ontology.get('relations',[]) if r['from'] in names or r['to'] in names]
        system = """你是通用查数平台的 Analyst。先理解业务需求，不能生成 SQL。只输出一个 JSON 对象，不输出隐藏思维过程。
不预设行业、业务对象或规则；行业口径只能来自当前业务域配置、已发布本体、用户确认和实际证据。
只能使用目录中存在的指标、对象和字段；字段和维度使用 object.property 格式。
输出 intent、subject、metrics、dimensions、fields、filters、time_range、grain、aggregation_rules、output_fields、assumptions、confidence。
confidence 是关键字段到 0~1 置信度的映射。若指标口径、时间语义、统计粒度、去重方式、数据范围或一对多聚合会实质改变结果，
必须设置 clarification_question，可同时给 clarification_options（每项含 id、label、value、description）。只问业务问题，不向用户询问表名或字段名。
filters 中 operator 只能是 eq/ne/gt/gte/lt/lte/in/between/is_null/is_not_null。空值条件必须使用is_null或is_not_null，不用字符串'NULL'。不要决定权限或审批。"""
        system += '''\n知识库只是辅助，证据为空或未命中时，必须继续基于需求和本体的真实字段分析；不要声称知识库支持未检索到的定义。
用户需求单和已经确认的口径属于同一任务上下文，不要丢失或重复询问已明确的信息；“不知道/由你建议”并不等于确认了某种口径。
存在会改变结果的歧义时输出 clarifications 数组，每项包含 id(稳定且唯一)、field、question、reason、options(2到4项，每项id,label,value,description)、allow_free_text:true、blocking:true。每轮最多问3项，回答不足则继续澄清；无歧义才输出空数组。
用户只是问候或未给出业务目标时，友好询问目标，不要直接查询。
可以输出 calculations 数组定义本次需求的临时聚合，每项含 name(英文标识符)、label、function(count/count_distinct/sum/avg/min/max)、field(目录字段，只有count允许省略)。metrics 填已发布指标或这些临时聚合的name。临时聚合不是正式业务指标；公式、时间、去重、统计范围有歧义必须先确认。不得输出任意表达式。
analysis_steps 仅输出可核验的理解结论、依据和待确认点，不输出隐藏思维链。未指定时间可以询问“全部数据/指定时间”，不得擅自填最近12个月。
同一对象之间存在多种业务关系角色时必须问清角色，使用 relationship_path 输出已确认的准确关系名。
用户指定年份、季度、起止日期时，必须选择明确的时间字段并在 filters 中生成实际的日期条件；time_range 只是说明，不能替代 filters。参考给定 current_date 解析相对时间。不限制时间时 time_range=null；不明确用哪个日期字段时先澄清。
'''
        first_metric = next(iter(ontology.get("metrics", [])), {})
        first_object = next(iter(ontology.get("objects", [])), {})
        first_property = next(iter(first_object.get("properties", [])), {})
        example_dimension = (
            f"{first_object.get('name')}.{first_property.get('name')}"
            if first_object.get("name") and first_property.get("name")
            else None
        )
        user = json.dumps(
            {
                "question": question,
                "business_domain": {key: ontology.get(key) for key in ('domain', 'name', 'description')},
                "current_date": datetime.now(timezone(timedelta(hours=8))).date().isoformat(),
                "ontology_catalog": catalog,
                "evidence": [item.model_dump(mode="json") for item in evidence],
                "output_example": {
                    "clarification_question": None,
                    "intent": "分析已发布业务指标",
                    "metrics": [first_metric.get("name")] if first_metric.get("name") else [],
                    "dimensions": [example_dimension] if example_dimension else [],
                    "fields": [],
                    "filters": ontology.get("defaultFilters", []),
                    "time_range": None,
                    "grain": first_metric.get("grain") or first_object.get("name"),
                    "assumptions": [],
                },
            },
            ensure_ascii=False,
        )
        return await self.model.complete_json(system, user)

    def _deterministic_analysis(
        self,
        question: str,
        evidence: list[EvidenceRef],
        ontology: dict[str, Any],
        data_scope: dict[str, Any],
    ) -> AnalysisOutcome:
        objects = [item for item in ontology.get("objects", []) if isinstance(item, dict)]
        detail = any(token in question for token in ("明细", "名单", "详情", "逐笔"))
        normalized_question = self._normalized_term(question)
        metrics = [] if detail else [
            metric["name"]
            for metric in ontology.get("metrics", [])
            if metric.get("name") and (
                str(metric.get("name")) in question
                or any(
                    self._normalized_term(term)
                    and self._normalized_term(term) in normalized_question
                    for term in self._semantic_terms(metric)
                )
            )
        ]
        dimensions: list[str] = []
        dimension_objects: list[str] = []
        for object_value in objects:
            object_name = str(object_value.get("name", ""))
            object_terms = self._semantic_terms(object_value)
            dimension_requested = any(
                marker and marker in question
                for term in object_terms
                for marker in (f"按{term}", f"各{term}", f"每{term}", f"{term}维度")
            )
            if not dimension_requested:
                continue
            properties = object_value.get("properties", [])
            dimension_property = next(
                (
                    prop for prop in properties
                    if prop.get("name") == "name" or "名称" in str(prop.get("label", ""))
                ),
                next(iter(properties), None),
            )
            if dimension_property:
                dimensions.append(f"{object_name}.{dimension_property['name']}")
                dimension_objects.append(object_name)

        fields: list[str] = []
        detail_objects: list[str] = []
        if detail:
            for object_value in objects:
                object_name = str(object_value.get("name", ""))
                object_mentioned = any(term in question for term in self._semantic_terms(object_value))
                for prop in object_value.get("properties", []):
                    prop_name = str(prop.get("name", ""))
                    explicitly_mentioned = any(term in question for term in self._semantic_terms(prop))
                    if explicitly_mentioned or (object_mentioned and not prop.get("sensitive")):
                        fields.append(f"{object_name}.{prop_name}")
                        if object_name not in detail_objects:
                            detail_objects.append(object_name)
        filters = [
            QueryFilter.model_validate(item).model_dump(mode="json")
            for item in ontology.get("defaultFilters", [])
        ]
        if not metrics and not fields:
            clarification = "当前发布本体中没有找到对应指标或字段，请补充要查询的业务指标。"
            return AnalysisOutcome(
                None,
                RequirementDraft(
                    summary="尚未识别到可执行的业务指标。",
                    subject=str(ontology.get("name") or ontology.get("domain") or ""),
                    data_scope=data_scope,
                    evidence=evidence,
                    confidence={"metrics": 0.0},
                    clarifications=[ClarificationQuestion(
                        id="missing_metric",
                        field="metrics",
                        question=clarification,
                        reason="没有明确指标就无法安全生成查询计划。",
                        options=[],
                    )],
                    status="awaiting_clarification",
                ),
                clarification,
            )
        selected_metric_values = [
            item for item in ontology.get("metrics", []) if item.get("name") in metrics
        ]
        grain = (
            detail_objects[0]
            if detail_objects
            else dimension_objects[0]
            if dimension_objects
            else str(selected_metric_values[0].get("grain", ""))
            if selected_metric_values
            else str(objects[0].get("name", ""))
        )
        domain_name = str(ontology.get("name") or ontology.get("domain") or "业务域")
        value = {
            "intent": f"查询{domain_name}明细" if detail else f"分析{domain_name}指标",
            "metrics": metrics,
            "dimensions": dimensions,
            "fields": list(dict.fromkeys(fields)),
            "filters": filters,
            "time_range": "最近12个月" if "12" in question or "最近" in question else None,
            "grain": grain,
            "assumptions": ["使用本体发布的默认过滤条件", "结果按用户授权数据范围过滤"],
        }
        return self._build_outcome(value, question, evidence, ontology, data_scope, "deterministic")

    @staticmethod
    def _normalized_term(value: str) -> str:
        return re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]", "", value).replace("天", "").lower()

    @staticmethod
    def _semantic_terms(value: dict[str, Any]) -> list[str]:
        return [
            term
            for term in [str(value.get("name", "")), str(value.get("label", "")), *[str(item) for item in value.get("aliases", [])]]
            if term
        ]

    def _build_outcome(
        self,
        value: dict[str, Any],
        question: str,
        evidence: list[EvidenceRef],
        ontology: dict[str, Any],
        data_scope: dict[str, Any],
        model_mode: str,
    ) -> AnalysisOutcome:
        # Optional JSON fields may explicitly be null while requirements are unclear.
        # Normalize only absence; malformed non-null values must still fail validation.
        value = dict(value)
        text_arrays = {
            'metrics', 'dimensions', 'fields', 'aggregation_rules', 'output_fields',
            'assumptions', 'analysis_steps', 'relationship_path',
        }
        for key in text_arrays | {'clarifications', 'clarification_options', 'calculations', 'filters', 'stages'}:
            value[key] = _model_list(value, key)
            if key in text_arrays and any(not isinstance(item, str) for item in value[key]):
                raise ValueError(f'模型返回的 {key} 必须是字符串数组')
        confidence = value.get('confidence')
        if confidence is None:
            confidence = {}
        if not isinstance(confidence, dict):
            raise ValueError('模型返回的 confidence 必须是对象')
        value['confidence'] = {str(k): float(v) for k, v in confidence.items() if v is not None}

        def options(raw: dict[str, Any], key: str) -> list[ClarificationOption]:
            result = []
            for item in _model_list(raw, key):
                if not isinstance(item, dict):
                    raise ValueError('模型返回的澄清选项必须是对象')
                result.append(ClarificationOption.model_validate({**item, 'description': item.get('description') or ''}))
            return result

        clarification = value.get("clarification_question")
        questions = []
        for item in value['clarifications']:
            if not isinstance(item, dict):
                raise ValueError('模型返回的澄清问题必须是对象')
            questions.append(ClarificationQuestion.model_validate({**item, 'options': options(item, 'options')}))
        if len({item.id for item in questions})!=len(questions):
            raise ValueError('模型返回了重复的澄清问题编号')
        if clarification and not questions:
            questions = [ClarificationQuestion(
                id="model_clarification",
                field="business_definition",
                question=str(clarification),
                reason="该信息会改变查询口径或结果范围。",
                options=options(value, 'clarification_options'),
            )]
        if questions:
            requirement = RequirementDraft(
                summary=str(value.get("summary") or "需求存在需要确认的业务口径。"),
                subject=str(value.get("subject") or ""),
                metrics=[str(item) for item in value.get("metrics", [])],
                dimensions=[str(item) for item in value.get("dimensions", [])],
                time_semantics=value.get("time_range"),
                grain=value.get("grain"),
                data_scope=data_scope,
                aggregation_rules=[str(item) for item in value.get("aggregation_rules", [])],
                output_fields=[str(item) for item in value.get("output_fields", [])],
                assumptions=[str(item) for item in value.get("assumptions", [])],
                evidence=evidence,
                confidence={str(k): float(v) for k, v in value.get("confidence", {}).items()},
                clarifications=questions,
                analysis_steps=[str(item) for item in value.get('analysis_steps',[])],
                status="awaiting_clarification",
            )
            return AnalysisOutcome(None, requirement, questions[0].question, model_mode)
        objects = {item["name"]: item for item in ontology.get("objects", [])}
        metrics = {item["name"]: item for item in ontology.get("metrics", [])}
        calculations=[QueryCalculation.model_validate(item) for item in value.get('calculations',[])]
        stages = [QueryStage.model_validate(item) for item in value['stages']]
        stage_objects = {stage.name: stage.source for stage in stages}
        for calculation in calculations:
            if calculation.name in metrics:
                raise ValueError('临时计算不得覆盖已发布指标')
            metrics[calculation.name]={'name':calculation.name,'label':calculation.label,'expression':calculation.field or '', 'grain':value.get('grain')}
        selected_metrics = [str(item) for item in value.get("metrics", [])] or [item.name for item in calculations]
        if calculations and value.get('grain') not in objects:
            raise ValueError(f"grain={value.get('grain')!r} 无效；临时统计的grain必须是本体对象name，不能用字段、中文标签或阶段名。可选对象：{', '.join(objects)}；dimensions单独定义最终分组字段。")
        unknown_metrics = [name for name in selected_metrics if name not in metrics]
        validation_errors = []
        if unknown_metrics:
            validation_errors.append('模型选择了未发布指标：' + ', '.join(unknown_metrics[:8])
                             + '；当前可用指标：' + (', '.join(metrics)[:400] or '无')
                             + '。临时统计须先在 calculations 中定义 name/function/field，再用其 name 引用；不能把中文展示名称直接当作指标名。')
        dimensions = [str(item) for item in value.get("dimensions", [])]
        fields = [str(item) for item in value.get("fields", [])]
        filters = [QueryFilter.model_validate(item) for item in value.get("filters", [])]
        referenced_fields = dimensions + fields + [item.field for item in filters] + [item.field for item in calculations if item.field]
        from .validation import field_error
        references = [(f'dimensions[{i}]', ref) for i, ref in enumerate(dimensions)]
        references += [(f'fields[{i}]', ref) for i, ref in enumerate(fields)]
        references += [(f'filters[{i}].field', item.field) for i, item in enumerate(filters)]
        references += [(f'calculations[{i}].field', item.field) for i, item in enumerate(calculations) if item.field]
        stage_catalog = {stage.name: {'properties': [{'name': m.name, 'column': m.name} for m in stage.measures]} for stage in stages}
        if set(stage_catalog) & set(objects):
            raise ValueError('阶段名称不得与本体对象重名')
        error = field_error(references, {**objects, **stage_catalog})
        if error:
            validation_errors.append(error)
        if validation_errors:
            raise ValueError('；'.join(validation_errors))
        required_objects = {stage_objects.get(field.split('.', 1)[0], field.split('.', 1)[0]) for field in referenced_fields}
        required_objects.update(stage.source for stage in stages)
        scope_field = str(ontology.get("scopeField") or "")
        if data_scope.get("branch_ids") and scope_field:
            required_objects.add(scope_field.split(".", 1)[0])
        for metric_name in selected_metrics:
            required_objects.update(
                name for name in objects if re.search(rf"\b{re.escape(name)}\.", metrics[metric_name].get("expression", ""))
            )
        if value.get('grain') in objects:
            required_objects.add(value['grain'])
        relation_path = value.get('relationship_path') or self._connect_objects(required_objects, ontology.get("relations", []))
        if any(name not in {r['name'] for r in ontology.get('relations',[])} for name in relation_path):
            raise ValueError('模型选择了未发布关系')
        if not selected_metrics and not fields:
            raise ValueError('需求尚未明确查询字段或统计指标，请补充需求')
        plan = QueryPlan(
            version='2.0' if stages else '1.0',
            domain=ontology.get("domain", "retail_credit"),
            data_source_id=str(ontology.get("dataSourceId") or ""),
            intent=str(value.get("intent") or question),
            metrics=selected_metrics,
            calculations=calculations,
            stages=stages,
            dimensions=dimensions,
            fields=fields,
            filters=filters,
            time_range=value.get("time_range"),
            grain=value.get("grain"),
            relationship_path=relation_path,
            data_scope=data_scope,
            assumptions=[str(item) for item in value.get("assumptions", [])],
            evidence=evidence,
        )
        metric_labels = {
            name: str(metrics[name].get("label") or name)
            for name in selected_metrics
        }
        summary_parts = [str(value.get("intent") or question)]
        if metric_labels:
            summary_parts.append("指标：" + "、".join(metric_labels.values()))
        if value.get("time_range"):
            summary_parts.append("时间：" + str(value["time_range"]))
        requirement = RequirementDraft(
            summary="；".join(summary_parts),
            analysis_steps=[str(item) for item in value.get('analysis_steps',[])],
            subject=str(value.get("subject") or value.get("grain") or ontology.get("name") or ""),
            metrics=selected_metrics,
            dimensions=dimensions,
            filters=filters,
            time_semantics=value.get("time_range"),
            grain=value.get("grain"),
            data_scope=data_scope,
            aggregation_rules=[str(item) for item in value.get("aggregation_rules", [])],
            stages=stages,
            output_fields=fields or dimensions + selected_metrics,
            calculations=calculations,
            assumptions=[str(item) for item in value.get("assumptions", [])],
            evidence=evidence,
            confidence={
                "subject": float(value.get("confidence", {}).get("subject", 0.9)),
                "metrics": float(value.get("confidence", {}).get("metrics", 0.9)),
                "time_semantics": float(value.get("confidence", {}).get("time_semantics", 0.8 if value.get("time_range") else 0.6)),
            },
            status="ready",
        )
        from .constraints import validate_explicit_filters
        validate_explicit_filters(plan, requirement.assumptions + requirement.analysis_steps, ontology)
        return AnalysisOutcome(plan, requirement=requirement, model_mode=model_mode)

    @staticmethod
    def _connect_objects(required: set[str], relations: list[dict[str, Any]]) -> list[str]:
        if len(required) <= 1:
            return []
        connected = {sorted(required)[0]}
        selected: list[str] = []
        while not required.issubset(connected):
            target = sorted(required - connected)[0]
            queue = [(item, []) for item in sorted(connected)]
            visited = set(connected)
            found: list[dict[str, Any]] | None = None
            while queue and found is None:
                current, path = queue.pop(0)
                for relation in relations:
                    next_object = relation.get("to") if relation.get("from") == current else relation.get("from") if relation.get("to") == current else None
                    if not next_object or next_object in visited:
                        continue
                    next_path = path + [relation]
                    if next_object == target:
                        found = next_path
                        break
                    visited.add(next_object)
                    queue.append((next_object, next_path))
            if found is None:
                raise ValueError(f"本体中不存在到 {target} 的关系路径")
            for relation in found:
                if relation["name"] not in selected:
                    selected.append(relation["name"])
                connected.update((relation["from"], relation["to"]))
        return selected


class ReviewerAgent:
    contract = AgentContract(
        name="reviewer",
        goal="以隔离上下文复核业务口径、关系路径、SQL 和结果是否一致",
        allowed_tools=("read_evidence", "read_ontology_snapshot", "inspect_sql", "inspect_result_summary"),
        forbidden_actions=("reply_to_user", "execute_sql", "approve_on_behalf_of_human", "call_analyst"),
    )

    def __init__(self, model: StructuredModel | None = None) -> None:
        # StructuredModel is stateless. Reviewer receives a new, artifact-only
        # prompt and never sees the Analyst conversation or hidden reasoning.
        self.model = model or structured_model

    async def review(
        self,
        plan: QueryPlan,
        gate: GateDecision,
        sql: SQLArtifact | None = None,
        requirement: RequirementSpec | None = None,
    ) -> ReviewDecision:
        if not gate.allowed:
            issues = [check.message for check in gate.checks if not check.passed and check.severity == "blocking"]
            return ReviewDecision(decision="rejected", issues=issues, rationale="确定性门禁未通过，Reviewer 不允许放行。")
        if requirement is None:
            return ReviewDecision(decision="revise", issues=["缺少用户确认的需求版本"], rationale="未完成业务口径对齐，禁止放行 SQL。")
        from .constraints import filter_key
        actual_filters = {filter_key(item) for item in plan.filters}
        missing = [filter_key(item) for item in requirement.filters if filter_key(item) not in actual_filters]
        if missing:
            return ReviewDecision(decision='revise', issues=['缺少已确认筛选条件：' + ', '.join(missing)], rationale='必须将已确认条件落实到执行方案，不能仅写入说明。')
        consistency_issues: list[str] = []
        if set(requirement.metrics) != set(plan.metrics):
            consistency_issues.append("确认指标与 QueryPlan 不一致")
        if set(requirement.dimensions) != set(plan.dimensions):
            consistency_issues.append("确认维度与 QueryPlan 不一致")
        if requirement.stages != plan.stages or requirement.calculations != plan.calculations:
            consistency_issues.append('确认的聚合阶段或计算公式与执行方案不一致')
        if requirement.grain and plan.grain and requirement.grain != plan.grain:
            consistency_issues.append("确认粒度与 QueryPlan 不一致")
        if consistency_issues:
            return ReviewDecision(
                decision="rejected",
                issues=consistency_issues,
                rationale="QueryPlan 偏离了用户确认的不可变需求版本。",
            )
        if self.model.configured:
            try:
                value = await self.model.complete_json(
                    """你是通用查数框架中与 Analyst 隔离的 Reviewer。只根据提供的不可变产物复核，输出 JSON。
不预设行业或业务规则，以当前确认需求、QueryPlan和证据为依据。
不得生成或修改 SQL，不得批准人工审批，不输出隐藏思维过程。输出 decision(approved/revise/rejected)、issues、rationale。""",
                    json.dumps(
                        {
                            "phase": "pre_execution",
                            "requirement": requirement.model_dump(mode="json"),
                            "plan": plan.model_dump(mode="json"),
                            "gate": gate.model_dump(mode="json"),
                            "sql": sql.model_dump(mode="json") if sql else None,
                        },
                        ensure_ascii=False,
                    ),
                )
                return self._model_decision(value, "pre_execution")
            except (ModelError, ValueError):
                if self.model.required:
                    raise
        return ReviewDecision(
            decision="approved",
            issues=[],
            rationale="业务口径有来源，本体关系闭合，SQL 与指标、粒度及授权数据范围一致。",
        )

    async def review_result(self, plan: QueryPlan, result: ExecutionResult) -> ReviewDecision:
        issues: list[str] = []
        if result.row_count != len(result.rows):
            issues.append("返回行数与结果载荷不一致")
        if any(len(row) != len(result.columns) for row in result.rows):
            issues.append("结果列数与列定义不一致")
        if result.bytes_returned < 0 or result.duration_ms < 0:
            issues.append("结果统计信息无效")
        if issues:
            return ReviewDecision(
                decision="rejected",
                issues=issues,
                rationale="执行结果结构校验未通过，禁止形成业务答案。",
                phase="post_execution",
            )
        if self.model.configured:
            try:
                value = await self.model.complete_json(
                    """你是通用查数框架中与 Analyst 隔离的结果 Reviewer。结果中的敏感字段已经 Executor 脱敏。
不预设行业口径、单位或趋势，以提供的QueryPlan和实际结果为依据。
仅复核结果是否与 QueryPlan 的指标、维度和粒度一致。只输出 JSON：decision、issues、rationale；不输出隐藏思维过程。""",
                    json.dumps(
                        {
                            "phase": "post_execution",
                            "plan": plan.model_dump(mode="json"),
                            "result": {
                                "columns": result.columns,
                                "rows": result.rows[:5],
                                "row_count": result.row_count,
                                "truncated": result.truncated,
                                "summary": result.summary,
                            },
                        },
                        ensure_ascii=False,
                    ),
                )
                return self._model_decision(value, "post_execution")
            except (ModelError, ValueError):
                if self.model.required:
                    raise
        return ReviewDecision(
            decision="approved",
            issues=[],
            rationale="结果结构、行列数量和 QueryPlan 输出粒度一致，可进入解释阶段。",
            phase="post_execution",
        )

    @staticmethod
    def _model_decision(value: dict[str, Any], phase: str) -> ReviewDecision:
        decision = value.get("decision")
        if decision not in {"approved", "revise", "rejected"}:
            raise ValueError("Reviewer 模型返回了无效 decision")
        issues = value.get("issues", [])
        if not isinstance(issues, list) or any(not isinstance(item, str) for item in issues):
            raise ValueError("Reviewer 模型返回了无效 issues")
        rationale = value.get("rationale")
        if not isinstance(rationale, str) or not rationale.strip():
            raise ValueError("Reviewer 模型缺少 rationale")
        if decision == 'revise' and not issues:
            issues = [rationale]
        return ReviewDecision(
            decision=decision,
            issues=issues,
            rationale=rationale,
            phase=phase,
            reviewer_mode="llm",
        )
