from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator


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
    question: str
    reason: str
    options: list[ClarificationOption] = Field(default_factory=list)
    allow_free_text: bool = True
    blocking: bool = True


class RequirementDraft(BaseModel):
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
    calculations: list[QueryCalculation] = Field(default_factory=list)
    output_fields: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    evidence: list[EvidenceRef] = Field(default_factory=list)
    confidence: dict[str, float] = Field(default_factory=dict)
    clarifications: list[ClarificationQuestion] = Field(default_factory=list)
    status: Literal["draft", "awaiting_clarification", "ready"] = "draft"


class ConfirmedFact(BaseModel):
    question_id: str
    field: str
    value: str
    label: str


class RequirementSpec(BaseModel):
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
    calculations: list[QueryCalculation] = Field(default_factory=list)
    output_fields: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    evidence: list[EvidenceRef] = Field(default_factory=list)
    confirmed_facts: list[ConfirmedFact] = Field(default_factory=list)
    confirmed_by: str
    confirmed_at: datetime = Field(default_factory=utc_now)


class QueryCalculation(BaseModel):
    name: str = Field(pattern=r'^[A-Za-z_][A-Za-z0-9_]*$', max_length=64)
    label: str = ''
    function: Literal['count','count_distinct','sum','avg','min','max']
    field: str | None = None


class StageMeasure(QueryCalculation):
    filters: list[QueryFilter] = Field(default_factory=list, max_length=32)


class QueryStage(BaseModel):
    """One source object reduced to a unique grouping key before joins."""
    name: str = Field(pattern=r'^[A-Za-z_][A-Za-z0-9_]{0,63}$')
    label: str = Field(default='', max_length=200)
    source: str
    group_by: list[str] = Field(min_length=1, max_length=16)
    measures: list[StageMeasure] = Field(min_length=1, max_length=32)
    filters: list[QueryFilter] = Field(default_factory=list, max_length=64)
    join_type: Literal['left', 'inner'] = 'left'


class QueryPlan(BaseModel):
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
    time_range: str | None = None
    grain: str | None = None
    relationship_path: list[str] = Field(default_factory=list)
    data_scope: dict[str, Any] = Field(default_factory=dict)
    assumptions: list[str] = Field(default_factory=list)
    evidence: list[EvidenceRef] = Field(default_factory=list)


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


class ReviewDecision(BaseModel):
    decision: Literal["approved", "revise", "rejected"]
    issues: list[str] = Field(default_factory=list)
    rationale: str
    phase: Literal["pre_execution", "post_execution"] = "pre_execution"
    reviewer_mode: Literal["deterministic", "llm"] = "deterministic"


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

    approval_mode: Literal["disabled", "all", "risk_based"] = "risk_based"
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


class QueryRun(BaseModel):
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
    revision_count: int = 0
    plan: QueryPlan | None = None
    sql: SQLArtifact | None = None
    sql_explanation: str | None = None
    gate: GateDecision | None = None
    review: ReviewDecision | None = None
    result_review: ReviewDecision | None = None
    approval: ApprovalDecision | None = None
    result: ExecutionResult | None = None
    answer: str | None = None
    error: str | None = None
    created_at: datetime = Field(default_factory=utc_now)

