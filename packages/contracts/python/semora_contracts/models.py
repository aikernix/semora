from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator, model_serializer


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class RunStatus(StrEnum):
    RECEIVED = "received"
    RETRIEVING = "retrieving"
    UNDERSTANDING = "understanding"
    ALIGNING = "aligning"
    CLARIFYING = "clarifying"
    ALIGNED = "aligned"
    PLANNING = "planning"
    COMPILING = "compiling"
    GATING = "gating"
    REVIEWING = "reviewing"
    AWAITING_APPROVAL = "awaiting_approval"
    EXECUTING = "executing"
    EXPLAINING = "explaining"
    COMPLETED = "completed"
    FAILED = "failed"


class EvidenceRef(BaseModel):
    knowledge_base_id: str
    document_id: str
    chunk_id: str
    locator: str
    revision: str
    title: str
    summary: str


class KnowledgeResearchStep(BaseModel):
    kind: str
    tool: str = ""
    summary: str
    status: str = "completed"


class KnowledgeResearchResult(BaseModel):
    provider: str
    agent_id: str
    session_id: str
    answer: str = ""
    steps: list[KnowledgeResearchStep] = Field(default_factory=list)
    evidence: list[EvidenceRef] = Field(default_factory=list)


class OntologySnapshotRef(BaseModel):
    domain: str
    version: str
    content_hash: str
    artifact_version: str
    data_source_id: str = ""
    knowledge_base_ids: list[str] = Field(default_factory=list)


class OntologyCandidateEvidence(BaseModel):
    knowledge_base_id: str = ""
    document_id: str = ""
    chunk_id: str = ""
    locator: str = ""
    revision: str = ""
    excerpt: str = ""


class OntologyCandidate(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: str
    domain: str
    term: str
    source: str
    proposal: str
    kind: Literal["definition", "metric", "object", "property", "relation"] = "definition"
    patch: dict[str, Any] = Field(default_factory=dict)
    status: Literal["pending_review", "accepted", "rejected"] = "pending_review"
    evidence: OntologyCandidateEvidence | dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(alias="createdAt")
    reviewed_at: datetime | None = Field(default=None, alias="reviewedAt")
    reviewed_by: str = Field(default="", alias="reviewedBy")
    review_note: str = Field(default="", alias="reviewNote")


class CandidateExtractionResult(BaseModel):
    provider: str = "semora-native"
    extractor_mode: Literal["deterministic", "llm"]
    domain: str
    document_id: str
    count: int
    candidates: list[OntologyCandidate]


class QueryFilter(BaseModel):
    field: str
    operator: Literal["eq", "ne", "gt", "gte", "lt", "lte", "in", "between", "is_null", "is_not_null"]
    value: Any = None

    @model_validator(mode='after')
    def validate_operand(self):
        if self.operator in {'is_null', 'is_not_null'}:
            if self.value is not None:
                raise ValueError('空值运算符不接受非空value')
        elif 'value' not in self.model_fields_set:
            raise ValueError('此筛选运算符必须提供value')
        elif self.value is None:
            if self.operator not in {'eq', 'ne'}:
                raise ValueError('空值只能使用is_null或is_not_null')
            self.operator = 'is_null' if self.operator == 'eq' else 'is_not_null'
        return self


class ClarificationOption(BaseModel):
    id: str
    label: str
    value: str
    description: str = ""


class ClarificationQuestion(BaseModel):
    id: str
    field: str
    question: str = Field(description='面向业务用户的具体问题。')
    reason: str = Field(description='该选择会影响什么结果。')
    options: list[ClarificationOption] = Field(default_factory=list,description='可选业务解释，同时允许用户自由补充。')
    allow_free_text: bool = True
    blocking: bool = True


class QueryOrder(BaseModel):
    field: str = Field(min_length=1, max_length=200)
    direction: Literal['asc', 'desc'] = 'asc'


class IntentMeasure(BaseModel):
    """Business aggregate, never an SQL expression or a physical stage."""
    model_config = ConfigDict(extra='forbid')
    name: str = Field(pattern=r'^[A-Za-z_][A-Za-z0-9_]*$', max_length=64)
    label: str = ''
    function: Literal['count', 'count_distinct', 'sum', 'avg', 'min', 'max']
    field: str = Field(description='用于聚合的完整对象.属性。')
    filters: list[QueryFilter] = Field(default_factory=list,max_length=32,description='仅影响本指标的记录条件，不缩小其他指标或主体集合；指标时间窗口写在此处。')
    null_value: int | float | None = Field(default=None,allow_inf_nan=False,description='本指标聚合结果为空时使用的数值；用户要求无匹配显示0时填0，否则为null。')

    @model_serializer(mode='wrap')
    def serialize_measure(self,handler):
        value=handler(self)
        if not self.filters:value.pop('filters',None)
        if self.null_value is None:value.pop('null_value',None)
        return value


class IntentSelection(BaseModel):
    model_config = ConfigDict(extra='forbid')
    object: str = Field(description='被选取的业务对象类型name；可以与提供时间属性的对象不同。')
    unit: Literal['entities', 'records'] = Field(description='entities按object的唯一标识去重并以每个实体关联时间的MAX/MIN排序；records直接选事件记录，可重复所属实体。')
    time_field: str = Field(description='决定先后的对象.时间属性；关联角色用于确定该时间的业务含义。')
    limit: int = Field(ge=1, le=1000, strict=True)
    direction: Literal['asc', 'desc'] = 'desc'
    filters: list[QueryFilter] = Field(default_factory=list, max_length=32,description='仅用于选取的范围；明确日期用实际起止条件，按已知类型、时区与单位表达。')
    relationship_roles: list[str] = Field(default_factory=list, max_length=16)


class IntentExistence(BaseModel):
    """Keep subjects with/without related records through a published relation."""
    model_config = ConfigDict(extra='forbid')
    relation: str = Field(description='用于判断有无关联记录的已发布关系name。')
    anchor: str = Field(description='该关系的来源端对象name。')
    present: bool = Field(description='true保留有关联的主体；false保留无关联的主体。按subject唯一标识判断，范围内任一记录匹配即视为存在。')
    filters: list[QueryFilter] = Field(default_factory=list,max_length=32,description='仅限定待检查的目标关联记录。')


class QueryIntent(BaseModel):
    """Analyst-owned meaning. Physical execution is exclusively program-owned."""
    model_config = ConfigDict(extra='forbid')
    version: Literal['1.0'] = '1.0'
    summary: str = Field(min_length=1, max_length=1200)
    subject: str = Field(description='本次分析的业务主体对象name。')
    fields: list[str] = Field(default_factory=list, max_length=32,description='输出的完整对象.属性；有汇总时只可重复group_by，不加入额外明细。')
    group_by: list[str] = Field(default_factory=list, max_length=16,description='最终业务汇总维度，使用完整对象.属性。')
    metrics: list[str] = Field(default_factory=list, max_length=32,description='当前已发布指标name；临时聚合使用measures。')
    measures: list[IntentMeasure] = Field(default_factory=list, max_length=32)
    filters: list[QueryFilter] = Field(default_factory=list, max_length=64,description='原始记录的共同筛选，在聚合前应用；仅影响某个指标的条件写该指标filters，分组后保留条件写having。日期用明确起止条件。')
    having: list[QueryFilter] = Field(default_factory=list,max_length=32,description='按聚合结果保留分组；field引用本次输出指标name。保留总量>0的分组应写这里，不用原始行过滤替代。')
    optional_relationships: list[str] = Field(default_factory=list,max_length=16,description='无关联匹配仍保留主体集合的已发布关系name；与record_scope的记录范围含义独立。')
    relationship_roles: list[str] = Field(default_factory=list, max_length=16,description='当前需求使用的已发布业务关系name；只选择关联角色，不表示有/无关联记录。')
    selection: IntentSelection | None = Field(default=None,description='先选前N个对象或记录，再展示/汇总关联数据时填写；仅对最终结果排序限量时为null。')
    existence: IntentExistence | None = None
    record_scope: Literal['selected_records', 'selection_window', 'all_related_records'] = Field(default='selected_records',description='selected_records仅选中的事件记录；selection_window为实体选取后仍限制记录窗口；all_related_records为选中实体的全部关联记录。')
    order_by: list[QueryOrder] = Field(default_factory=list, max_length=8,description='最终展示顺序，与selection的成员选取顺序独立。')
    limit: int | None = Field(default=None, ge=1, le=1000, strict=True)
    time_range: str | None = Field(default=None,description='时间范围说明，不替代filters中的实际条件。')
    assumptions: list[str] = Field(default_factory=list, max_length=24)
    unfulfilled_goals: list[str] = Field(default_factory=list,max_length=16,description='本次能力无法实现、未纳入实际查询的用户目标；明确列出，不用其他指标冒充。')
    analysis_steps: list[str] = Field(default_factory=list, max_length=16,description='简短的可核验结论、依据或待确认项。')

    @model_serializer(mode='wrap')
    def serialize_compatible(self,handler):
        value=handler(self)
        if self.existence is None:value.pop('existence',None)
        for key in ('having','optional_relationships','unfulfilled_goals'):
            if not getattr(self,key):value.pop(key,None)
        return value


class RequirementDraft(BaseModel):
    having: list[QueryFilter] = Field(default_factory=list)
    optional_relationships: list[str] = Field(default_factory=list)
    query_intent: QueryIntent | None = None
    execution_mode: Literal['single_query', 'sequential_queries'] = 'single_query'
    order_by: list[QueryOrder] = Field(default_factory=list, max_length=8)
    limit: int | None = Field(default=None, ge=1, le=1000, strict=True)
    """LLM interpretation shown to the user before any SQL is produced."""

    version: int = 1
    summary: str
    analysis_steps: list[str] = Field(default_factory=list)
    subject: str = ""
    metrics: list[str] = Field(default_factory=list)
    dimensions: list[str] = Field(default_factory=list)
    filters: list[QueryFilter] = Field(default_factory=list)
    time_semantics: str | None = None
    grain: str | None = None
    data_scope: dict[str, Any] = Field(default_factory=dict)
    aggregation_rules: list[str] = Field(default_factory=list)
    stages: list[QueryStage] = Field(default_factory=list)
    source_steps: list[SourceQueryStep] = Field(default_factory=list)
    calculations: list[QueryCalculation] = Field(default_factory=list)
    output_fields: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    evidence: list[EvidenceRef] = Field(default_factory=list)
    confidence: dict[str, float] = Field(default_factory=dict)
    clarifications: list[ClarificationQuestion] = Field(default_factory=list)
    status: Literal["draft", "awaiting_clarification", "ready"] = "draft"

    @model_serializer(mode='wrap')
    def serialize_options(self,handler):
        value=handler(self)
        for key in ('having','optional_relationships'):
            if not getattr(self,key):value.pop(key,None)
        return value


class ConfirmedFact(BaseModel):
    question_id: str
    field: str
    value: str
    label: str


class RequirementSpec(BaseModel):
    having: list[QueryFilter] = Field(default_factory=list)
    optional_relationships: list[str] = Field(default_factory=list)
    query_intent: QueryIntent | None = None
    execution_mode: Literal['single_query', 'sequential_queries'] = 'single_query'
    order_by: list[QueryOrder] = Field(default_factory=list, max_length=8)
    limit: int | None = Field(default=None, ge=1, le=1000, strict=True)
    """Immutable, user-aligned business meaning bound to downstream artifacts."""

    version: str
    content_hash: str
    source_draft_version: int
    summary: str
    subject: str = ""
    metrics: list[str] = Field(default_factory=list)
    dimensions: list[str] = Field(default_factory=list)
    filters: list[QueryFilter] = Field(default_factory=list)
    time_semantics: str | None = None
    grain: str | None = None
    data_scope: dict[str, Any] = Field(default_factory=dict)
    aggregation_rules: list[str] = Field(default_factory=list)
    stages: list[QueryStage] = Field(default_factory=list)
    source_steps: list[SourceQueryStep] = Field(default_factory=list)
    calculations: list[QueryCalculation] = Field(default_factory=list)
    output_fields: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    evidence: list[EvidenceRef] = Field(default_factory=list)
    confirmed_facts: list[ConfirmedFact] = Field(default_factory=list)
    confirmed_by: str
    confirmed_at: datetime = Field(default_factory=utc_now)

    @model_serializer(mode='wrap')
    def serialize_options(self,handler):
        value=handler(self)
        for key in ('having','optional_relationships'):
            if not getattr(self,key):value.pop(key,None)
        return value


class QueryCalculation(BaseModel):
    name: str = Field(pattern=r'^[A-Za-z_][A-Za-z0-9_]*$', max_length=64)
    label: str = ''
    function: Literal['count','count_distinct','sum','avg','min','max']
    field: str | None = None
    filters: list[QueryFilter] = Field(default_factory=list,max_length=32)
    null_value: int | float | None = Field(default=None,allow_inf_nan=False)

    @model_serializer(mode='wrap')
    def serialize_calculation(self,handler):
        value=handler(self)
        if self.__class__ is QueryCalculation and not self.filters:value.pop('filters',None)
        if self.null_value is None:value.pop('null_value',None)
        return value


class StageMeasure(QueryCalculation):
    filters: list[QueryFilter] = Field(default_factory=list, max_length=32)
    null_value: None = None


class QueryStage(BaseModel):
    """One source object reduced to a unique grouping key before joins."""
    name: str = Field(pattern=r'^[A-Za-z_][A-Za-z0-9_]{0,63}$')
    label: str = Field(default='', max_length=200)
    source: str
    group_by: list[str] = Field(min_length=1, max_length=16)
    measures: list[StageMeasure] = Field(min_length=1, max_length=32)
    filters: list[QueryFilter] = Field(default_factory=list, max_length=64)
    join_type: Literal['left', 'inner'] = 'left'


class SourceQueryPlan(BaseModel):
    order_by: list[QueryOrder] = Field(default_factory=list, max_length=8)
    limit: int | None = Field(default=None, ge=1, le=1000, strict=True)
    version: str = "1.0"
    domain: str
    data_source_id: str = ""
    intent: str
    metrics: list[str]
    calculations: list[QueryCalculation] = Field(default_factory=list)
    stages: list[QueryStage] = Field(default_factory=list, max_length=8)
    dimensions: list[str] = Field(default_factory=list)
    fields: list[str] = Field(default_factory=list)
    filters: list[QueryFilter] = Field(default_factory=list)
    having: list[QueryFilter] = Field(default_factory=list,max_length=32)
    optional_relationships: list[str] = Field(default_factory=list,max_length=16)
    time_range: str | None = None
    grain: str | None = None
    relationship_path: list[str] = Field(default_factory=list)
    data_scope: dict[str, Any] = Field(default_factory=dict)
    assumptions: list[str] = Field(default_factory=list)
    evidence: list[EvidenceRef] = Field(default_factory=list)

    @model_serializer(mode='wrap')
    def serialize_source_query(self,handler):
        value=handler(self)
        for key in ('having','optional_relationships'):
            if not getattr(self,key):value.pop(key,None)
        return value


class KeyPair(BaseModel):
    source_field: str
    target_field: str


class StepInput(BaseModel):
    kind: Literal['published_relation', 'identity'] = 'published_relation'
    relation: str
    source_step: str
    source_field: str | None = None
    target_field: str | None = None
    key_pairs: list[KeyPair] = Field(default_factory=list, max_length=16)
    include_source_fields: list[str] = Field(default_factory=list, max_length=32)


    @model_validator(mode='after')
    def validate_keys(self):
        if self.key_pairs:
            if self.source_field is not None or self.target_field is not None:
                raise ValueError('key_pairs不能与旧单键字段同时使用')
            if len({p.source_field for p in self.key_pairs}) != len(self.key_pairs) or len({p.target_field for p in self.key_pairs}) != len(self.key_pairs):
                raise ValueError('关联键字段不能重复')
        elif not self.source_field or not self.target_field:
            raise ValueError('必须提供key_pairs或完整的单键字段')
        return self

    @property
    def pairs(self):
        return self.key_pairs or [KeyPair(source_field=self.source_field, target_field=self.target_field)]


class MergeMeasure(BaseModel):
    name: str
    function: Literal['sum', 'count', 'min', 'max']


class ResultRegroup(BaseModel):
    """Program-owned merge of complete per-boundary aggregates, never AI evaluation."""
    group_by: list[str] = Field(min_length=1, max_length=16)
    measures: list[MergeMeasure] = Field(min_length=1, max_length=32)
    order_by: list[QueryOrder] = Field(default_factory=list, max_length=8)
    limit: int | None = Field(default=None, ge=1, le=1000, strict=True)


class ResultMembership(BaseModel):
    """Final existence/anti-existence check over complete, scoped source rows."""
    source_step: str
    group_by: list[str] = Field(min_length=1,max_length=16)
    fields: list[str] = Field(min_length=1,max_length=32)
    present: bool
    order_by: list[QueryOrder] = Field(default_factory=list,max_length=8)
    limit: int | None = Field(default=None,ge=1,le=1000,strict=True)


class SourceQueryStep(BaseModel):
    name: str = Field(pattern=r'^[A-Za-z_][A-Za-z0-9_]{0,63}$')
    label: str = ''
    data_source_id: str
    query: SourceQueryPlan
    inputs: list[StepInput] = Field(default_factory=list, max_length=1)
    regroup: ResultRegroup | None = None
    membership: ResultMembership | None = None

    @model_serializer(mode='wrap')
    def serialize_compatible(self, handler):
        value=handler(self)
        if self.regroup is None:
            value.pop('regroup',None)
        if self.membership is None:
            value.pop('membership',None)
        return value


class BoundKeyFilter(BaseModel):
    """Executor-generated tuple predicate; never part of Analyst SourceQueryPlan."""
    fields: list[str] = Field(min_length=1, max_length=16)
    rows: list[list[Any]] = Field(default_factory=list, max_length=1000)

    @model_validator(mode='after')
    def validate_rows(self):
        if len(set(self.fields)) != len(self.fields):
            raise ValueError('绑定字段重复')
        if any(len(row) != len(self.fields) or any(v is None or isinstance(v, (list, dict)) for v in row) for row in self.rows):
            raise ValueError('绑定元组长度不一致或包含空值/非标量')
        return self


class QueryPlan(SourceQueryPlan):
    aggregate_precision: Literal['display', 'raw'] = 'display'
    query_intent: QueryIntent | None = None
    execution_mode: Literal['single_query', 'sequential_queries'] = 'single_query'
    bound_key_filters: list[BoundKeyFilter] = Field(default_factory=list, max_length=1)
    source_steps: list[SourceQueryStep] = Field(default_factory=list, max_length=8)

    @model_serializer(mode='wrap')
    def serialize_compatible(self, handler):
        value=handler(self)
        if self.aggregate_precision=='display':
            value.pop('aggregate_precision',None)
        for key in ('having','optional_relationships'):
            if not getattr(self,key):value.pop(key,None)
        return value


class SQLArtifact(BaseModel):
    semantic_sql: str
    physical_sql: str
    dialect: str = "postgres"
    data_source_id: str = ""
    parameters: dict[str, Any] = Field(default_factory=dict)
    sensitive_columns: list[str] = Field(default_factory=list)
    requirement_version: str = ""
    requirement_hash: str = ""
    artifact_hash: str
    compilation: dict[str, Any] = Field(default_factory=dict)


class GateCheck(BaseModel):
    name: str
    passed: bool
    severity: Literal["info", "warning", "blocking"]
    message: str


class GateDecision(BaseModel):
    allowed: bool
    risk_level: Literal["low", "medium", "high"]
    requires_approval: bool
    checks: list[GateCheck]


class ReviewFinding(BaseModel):
    id: str = ""
    kind: Literal["repair", "clarification", "capability"] = Field(default='repair',description='repair为可修复遗漏/错误；clarification为需用户决定的业务含义；capability为已知执行能力缺口。')
    paths: list[str] = Field(default_factory=list, max_length=12, description='问题对应的JSON Pointer。业务声明遗漏定位/query_intent/下字段；声明正确但编译产物错误定位执行计划字段。')
    message: str = Field(min_length=1, max_length=2000)
    evidence: str = Field(default="", max_length=2000)
    acceptance: str = Field(min_length=1, max_length=2000, description="修复后应满足的可核验条件")
    question: str | None = Field(default=None, max_length=1000)
    capability_key: str | None = Field(default=None, max_length=100)


class ReviewResolution(BaseModel):
    finding_id: str
    status: Literal["resolved", "unresolved"]
    rationale: str = Field(min_length=1, max_length=2000)


class ReviewDecision(BaseModel):
    disposition: Literal['blocking','advisory'] = 'blocking'
    findings: list[ReviewFinding] = Field(default_factory=list)
    resolutions: list[ReviewResolution] = Field(default_factory=list)
    decision: Literal["approved", "revise", "rejected"]
    issues: list[str] = Field(default_factory=list)
    rationale: str
    phase: Literal["pre_execution", "post_execution"] = "pre_execution"
    reviewer_mode: Literal["deterministic", "llm", "unavailable"] = "deterministic"

    @model_serializer(mode='wrap')
    def serialize_disposition(self,handler):
        value=handler(self)
        if self.disposition=='blocking':value.pop('disposition',None)
        return value


class ApprovalDecision(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    run_id: str
    policy_version: str
    sql_hash: str
    ontology_hash: str = ""
    permission_hash: str = ""
    binding_hash: str = ""
    status: Literal["pending", "approved", "rejected", "expired"] = "pending"
    actor: str | None = None
    reason: str | None = None
    decided_at: datetime | None = None


class SystemSettings(BaseModel):
    """Versioned, non-secret runtime policy managed by Semora administrators."""
    delivery_mode: Literal['strict','best_effort'] = Field(default='strict',description='best_effort先交付可执行结果，模型复核意见作为建议；确定性权限、只读和执行完整性检查仍生效。')

    approval_mode: Literal["disabled", "all", "risk_based"] = "disabled"
    require_plan_confirmation: bool = False
    approve_sensitive_detail: bool = True
    approve_large_result: bool = True
    approve_cross_organization: bool = True
    large_result_threshold: int = Field(default=100_000, ge=1_000, le=100_000_000)
    query_timeout_seconds: int = Field(default=30, ge=1, le=30)
    max_result_rows: int = Field(default=1_000, ge=1, le=1_000)
    max_result_mb: int = Field(default=10, ge=1, le=10)
    preview_rows: int = Field(default=100, ge=1, le=1_000)
    reviewer_isolated_context: bool = True
    reasoning_display: Literal["structured", "hidden"] = "structured"
    version: int = Field(default=1, ge=1)
    policy_version: str = ""
    updated_at: datetime = Field(default_factory=utc_now)
    updated_by: str = "system"


class AuthorizationContext(BaseModel):
    """Immutable authorization snapshot attached to a query run."""

    subject: str
    username: str
    roles: list[str] = Field(default_factory=list)
    branch_ids: list[int] = Field(default_factory=list)
    sensitivity_clearance: Literal["internal", "confidential", "restricted"] = "internal"
    allowed_data_source_ids: list[str] | None = None
    cross_organization: bool = False
    policy_version: str = "abac-v1"


class ExecutionResult(BaseModel):
    columns: list[str]
    rows: list[list[Any]]
    row_count: int
    duration_ms: int
    truncated: bool = False
    bytes_returned: int = 0
    summary: dict[str, Any] = Field(default_factory=dict)


class AuditEvent(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    run_id: str
    sequence: int
    event_type: str
    actor: str
    payload: dict[str, Any]
    previous_hash: str
    event_hash: str
    trace_id: str
    created_at: datetime = Field(default_factory=utc_now)


class RunEvent(BaseModel):
    event: str
    status: RunStatus
    data: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)


class ReviewCase(BaseModel):
    """Persistent preflight decision, separate from model-private workspace."""
    model_config = ConfigDict(extra='forbid', frozen=True)
    id: str = Field(default_factory=lambda: str(uuid4()))
    operation: Literal['plan_preflight'] = 'plan_preflight'
    plan_hash: str
    binding: str
    input_hash: str = ''
    attempt: int = Field(default=1, ge=1, le=3)
    review: ReviewDecision
    pending: list[ReviewFinding] = Field(default_factory=list)
    updated_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode='after')
    def require_resolved_approval(self):
        if self.review.phase != 'pre_execution':
            raise ValueError('预检案件不能使用执行后复核结果')
        if self.review.decision == 'approved' and (self.pending or self.review.findings or any(r.status!='resolved' for r in self.review.resolutions)):
            raise ValueError('存在未解决问题，预检案件不能标记为通过')
        return self


class ExecutionReceipt(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    step: str
    status: Literal['completed', 'skipped'] = 'completed'
    row_count: int = Field(ge=0)
    duration_ms: int = Field(default=0, ge=0)
    truncated: bool = False
    output_hash: str
    finished_at: datetime = Field(default_factory=utc_now)


class QueryDelivery(BaseModel):
    kind: Literal['result','clarification','blocked','cancelled','interrupted']
    title: str
    summary: str
    warnings: list[str] = Field(default_factory=list)
    next_action: str = ''
    result_verified: bool = False


class QueryRun(BaseModel):
    delivery: QueryDelivery | None = None
    delivery_warnings: list[str] = Field(default_factory=list)
    id: str = Field(default_factory=lambda: str(uuid4()))
    session_id: str
    domain: str = "retail_credit"
    question: str
    message_mode: Literal['auto', 'continue', 'new'] = 'auto'
    continuation: dict[str, Any] | None = None
    user_answers: list[ConfirmedFact] = Field(default_factory=list)
    status: RunStatus = RunStatus.RECEIVED
    trace_id: str = Field(default_factory=lambda: str(uuid4()))
    authorization: AuthorizationContext | None = None
    events: list[RunEvent] = Field(default_factory=list)
    evidence: list[EvidenceRef] = Field(default_factory=list)
    knowledge_research: KnowledgeResearchResult | None = None
    ontology: OntologySnapshotRef | None = None
    requirement_draft: RequirementDraft | None = None
    requirement: RequirementSpec | None = None
    proposed_plan: QueryPlan | None = None
    alignment_rounds: list[dict[str, Any]] = Field(default_factory=list)
    agent_memory: dict[str, list[dict[str, Any]]] = Field(default_factory=dict, exclude=True)
    review_case: ReviewCase | None = Field(default=None, exclude=True)
    execution_binding: str = Field(default='', exclude=True)
    execution_receipts: dict[str, ExecutionReceipt] = Field(default_factory=dict, exclude=True)
    revision_count: int = 0
    plan: QueryPlan | None = None
    sql: SQLArtifact | None = None
    source_artifacts: dict[str, SQLArtifact] = Field(default_factory=dict)
    source_results: dict[str, ExecutionResult] = Field(default_factory=dict, exclude=True)
    source_reviews: dict[str, ReviewDecision] = Field(default_factory=dict)
    sql_explanation: str | None = None
    gate: GateDecision | None = None
    review: ReviewDecision | None = None
    result_review: ReviewDecision | None = None
    approval: ApprovalDecision | None = None
    result: ExecutionResult | None = None
    answer: str | None = None
    error: str | None = None
    created_at: datetime = Field(default_factory=utc_now)


RequirementDraft.model_rebuild()
RequirementSpec.model_rebuild()

