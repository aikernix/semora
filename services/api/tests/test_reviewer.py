import asyncio

from semora_agent import ReviewerAgent
from semora_contracts import (
    EvidenceRef,
    ExecutionResult,
    GateCheck,
    GateDecision,
    QueryPlan,
    RequirementSpec,
    SQLArtifact,
)


class StubModel:
    configured = True
    required = False

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def complete_json(self, system, user):
        self.calls.append((system, user))
        return self.responses.pop(0)


def plan() -> QueryPlan:
    return QueryPlan(
        domain="retail_credit",
        intent="统计贷款余额",
        metrics=["loan_balance"],
        evidence=[EvidenceRef(
            knowledge_base_id="kb",
            document_id="doc",
            chunk_id="chunk",
            locator="第1节",
            revision="v1",
            title="指标口径",
            summary="贷款余额定义",
        )],
    )


def allowed_gate() -> GateDecision:
    return GateDecision(
        allowed=True,
        risk_level="low",
        requires_approval=False,
        checks=[GateCheck(name="readonly", passed=True, severity="info", message="只读")],
    )


def sql() -> SQLArtifact:
    return SQLArtifact(
        semantic_sql="SELECT loan_balance FROM loan",
        physical_sql="SELECT SUM(outstanding_balance) AS loan_balance FROM loans LIMIT 1000",
        artifact_hash="hash",
    )


def requirement() -> RequirementSpec:
    return RequirementSpec(
        version="req-1-test",
        content_hash="requirement-hash",
        source_draft_version=1,
        summary="统计贷款余额",
        metrics=["loan_balance"],
        confirmed_by="测试用户",
    )


def test_reviewer_uses_isolated_structured_model_for_both_phases():
    model = StubModel([
        {"decision": "approved", "issues": [], "rationale": "口径和 SQL 一致"},
        {"decision": "approved", "issues": [], "rationale": "结果粒度一致"},
    ])
    reviewer = ReviewerAgent(model)
    pre = asyncio.run(reviewer.review(plan(), allowed_gate(), sql(), requirement()))
    post = asyncio.run(reviewer.review_result(
        plan(),
        ExecutionResult(columns=["loan_balance"], rows=[[100]], row_count=1, duration_ms=2),
    ))

    assert pre.reviewer_mode == "llm" and pre.phase == "pre_execution"
    assert post.reviewer_mode == "llm" and post.phase == "post_execution"
    assert len(model.calls) == 2
    assert "Analyst conversation" not in model.calls[0][1]


def test_reviewer_cannot_override_blocking_gate():
    model = StubModel([{"decision": "approved", "issues": [], "rationale": "错误放行"}])
    gate = GateDecision(
        allowed=False,
        risk_level="high",
        requires_approval=False,
        checks=[GateCheck(name="readonly", passed=False, severity="blocking", message="非只读")],
    )
    decision = asyncio.run(ReviewerAgent(model).review(plan(), gate, sql()))
    assert decision.decision == "rejected"
    assert model.calls == []


def test_post_execution_reviewer_rejects_malformed_result_before_model():
    model = StubModel([{"decision": "approved", "issues": [], "rationale": "错误放行"}])
    result = ExecutionResult(
        columns=["branch", "loan_balance"],
        rows=[["上海分行"]],
        row_count=2,
        duration_ms=1,
    )
    decision = asyncio.run(ReviewerAgent(model).review_result(plan(), result))
    assert decision.decision == "rejected"
    assert decision.phase == "post_execution"
    assert model.calls == []
