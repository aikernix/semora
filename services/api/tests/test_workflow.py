import asyncio

from app.store import Store
from app.workflow import Workflow
from semora_contracts import AuthorizationContext, EvidenceRef, ExecutionResult, OntologySnapshotRef, RunStatus


async def fake_retrieve(_: str) -> list[EvidenceRef]:
    return [
        EvidenceRef(
            knowledge_base_id="11111111-1111-4111-8111-111111111111",
            document_id="22222222-2222-4222-8222-222222222222",
            chunk_id="33333333-3333-4333-8333-333333333333",
            locator="指标手册 / 分块 1",
            revision="v1",
            title="零售信贷指标口径手册",
            summary="贷款余额与90+逾期率口径。",
        )
    ]


async def fake_snapshot(domain: str) -> tuple[OntologySnapshotRef, dict]:
    assert domain == "retail_credit"
    reference = OntologySnapshotRef(
        domain="retail_credit",
        version="1.0.0",
        content_hash="sha256:test",
        artifact_version="wren-1.0.0",
    )
    ontology = {
        "domain": "retail_credit",
        "dataSourceId": "retail-credit-pg",
        "knowledgeBaseIds": ["11111111-1111-4111-8111-111111111111"],
        "scopeField": "branch.id",
        "timeField": "loan.as_of_date",
        "defaultFilters": [{"field": "loan.status", "operator": "eq", "value": "active"}],
        "objects": [
            {"name": "branch", "label": "分行", "table": "branches", "primaryKey": "id", "properties": [{"name": "name", "label": "分行名称", "column": "name"}]},
            {"name": "customer", "label": "客户", "table": "customers", "primaryKey": "id", "properties": [{"name": "customer_no", "label": "客户号", "column": "customer_no"}, {"name": "name", "label": "姓名", "column": "name", "sensitive": True}, {"name": "mobile", "label": "手机号", "column": "mobile", "sensitive": True}]},
            {"name": "loan", "label": "贷款", "table": "loans", "primaryKey": "id", "properties": [{"name": "loan_no", "label": "贷款编号", "column": "loan_no"}, {"name": "outstanding_balance", "label": "贷款余额", "column": "outstanding_balance"}, {"name": "status", "label": "状态", "column": "status"}, {"name": "as_of_date", "label": "统计日期", "column": "as_of_date"}]},
            {"name": "payment", "label": "还款表现", "table": "payments", "primaryKey": "id", "properties": [{"name": "days_overdue", "label": "逾期天数", "column": "days_overdue"}, {"name": "overdue_amount", "label": "逾期金额", "column": "overdue_amount"}]},
        ],
        "relations": [
            {"name": "customer_has_loan", "from": "customer", "to": "loan", "condition": "customer.id = loan.customer_id"},
            {"name": "loan_belongs_to_branch", "from": "loan", "to": "branch", "condition": "loan.branch_id = branch.id"},
            {"name": "loan_has_payment", "from": "loan", "to": "payment", "condition": "loan.id = payment.loan_id"},
        ],
        "metrics": [
            {"name": "loan_balance", "label": "贷款余额", "expression": "SUM(loan.outstanding_balance)", "grain": "branch", "format": "currency"},
            {"name": "overdue_90_rate", "label": "90+逾期率", "expression": "SUM(payment.overdue_amount WHERE payment.days_overdue >= 90) / SUM(loan.outstanding_balance)", "grain": "branch", "format": "percentage"},
        ],
    }
    return reference, ontology


async def fake_execute(sql: str, parameters: dict, sensitive_columns: list[str]) -> ExecutionResult:
    if "customer_no" in sql:
        return ExecutionResult(
            columns=["branch", "customer_no", "name", "mobile", "loan_no", "outstanding_balance", "days_overdue"],
            rows=[["上海分行", "C10001", "张明", "13800001001", "L20260001", 1250000, 0]],
            row_count=1,
            duration_ms=8,
            bytes_returned=128,
        )
    return ExecutionResult(
        columns=["branch", "loan_balance", "overdue_90_rate"],
        rows=[["上海分行", 1790000, 4.69], ["深圳分行", 1410000, 5.11]],
        row_count=2,
        duration_ms=7,
        bytes_returned=96,
    )


def test_aggregate_query_completes_and_is_auditable():
    store = Store()
    workflow = Workflow(store, fake_execute, fake_retrieve, fake_snapshot)
    original = workflow.analyst.analyse
    async def staged_analysis(*args):
        from semora_contracts import QueryStage
        outcome = await original(*args)
        stage = QueryStage.model_validate({'name': 'payment_by_loan', 'source': 'payment', 'group_by': ['payment.loan_id'], 'measures': [{'name': 'overdue_total', 'function': 'sum', 'field': 'payment.overdue_amount', 'filters': [{'field': 'payment.days_overdue', 'operator': 'gte', 'value': 90}]}]})
        outcome.plan.stages = [stage]
        outcome.requirement.stages = [stage]
        return outcome
    workflow.analyst.analyse = staged_analysis
    run = asyncio.run(workflow.start("demo-session", "最近12个月各分行贷款余额和90+逾期率"))
    assert run.status == RunStatus.COMPLETED
    assert run.domain == "retail_credit"
    assert run.result and run.result.row_count == 2
    assert run.review and run.review.decision == "approved"
    assert run.answer and "上海分行" in run.answer
    assert store.audit.verify(run.id) == (True, None)
    nodes = set(workflow.graph.get_graph().nodes)
    assert {"retrieve", "analyse", "compile", "gate_and_review", "approval", "execute", "explain"} <= nodes


def test_session_domain_cannot_receive_a_snapshot_from_another_domain():
    async def wrong_snapshot(_: str) -> tuple[OntologySnapshotRef, dict]:
        return await fake_snapshot("retail_credit")

    run = asyncio.run(
        Workflow(Store(), fake_execute, fake_retrieve, wrong_snapshot).start(
            "demo-session", "查询余额", domain="deposit"
        )
    )
    assert run.domain == "deposit"
    assert run.status == RunStatus.FAILED
    assert "业务域与会话不一致" in (run.error or "")
    assert run.sql is None


def test_authorized_branch_scope_is_bound_into_sql_and_run():
    store = Store()
    workflow = Workflow(store, fake_execute, fake_retrieve, fake_snapshot)
    authorization = AuthorizationContext(
        subject="branch-user",
        username="分行分析员",
        roles=["analyst"],
        branch_ids=[2],
        sensitivity_clearance="confidential",
    )
    run = asyncio.run(workflow.start(
        "demo-session",
        "按分行查询贷款余额",
        approval_mode="disabled",
        authorization=authorization,
    ))
    assert run.status == RunStatus.COMPLETED
    assert run.authorization and run.authorization.branch_ids == [2]
    assert run.plan and run.plan.data_scope["branch_ids"] == [2]
    assert run.sql and run.sql.parameters["branch_id_0"] == 2


def test_empty_organization_scope_is_denied_before_retrieval():
    store = Store()
    workflow = Workflow(store, fake_execute, fake_retrieve, fake_snapshot)
    authorization = AuthorizationContext(
        subject="no-scope-user",
        username="无机构用户",
        roles=["business_user"],
        branch_ids=[],
    )
    run = asyncio.run(workflow.start(
        "demo-session", "查询贷款余额", authorization=authorization
    ))
    assert run.status == RunStatus.FAILED
    assert "没有可查询的机构" in (run.error or "")
    assert any(event.event == "authorization.denied" for event in run.events)


def test_internal_clearance_cannot_query_sensitive_fields():
    store = Store()
    workflow = Workflow(store, fake_execute, fake_retrieve, fake_snapshot)
    authorization = AuthorizationContext(
        subject="basic-user",
        username="普通业务用户",
        roles=["business_user"],
        branch_ids=[1],
        sensitivity_clearance="internal",
    )
    run = asyncio.run(workflow.start(
        "demo-session", "查询贷款客户明细和手机号", authorization=authorization
    ))
    assert run.status == RunStatus.FAILED
    assert run.sql is None
    assert "无权查询敏感字段" in (run.error or "")


def test_background_run_can_be_cancelled_and_audited():
    async def slow_retrieve(_: str) -> list[EvidenceRef]:
        await asyncio.sleep(10)
        return await fake_retrieve("")

    async def scenario():
        store = Store()
        workflow = Workflow(store, fake_execute, slow_retrieve, fake_snapshot)
        authorization = AuthorizationContext(
            subject="cancel-user",
            username="取消测试用户",
            roles=["analyst"],
            branch_ids=[1],
            sensitivity_clearance="confidential",
        )
        run = await workflow.start_background("demo-session", "查询贷款余额", authorization)
        await asyncio.sleep(0)
        decision = await workflow.cancel(run.id)
        return store.runs[run.id], decision, store.audit.verify(run.id)

    run, decision, audit = asyncio.run(scenario())
    assert decision["accepted"] is True
    assert decision["task_cancelled"] is True
    assert run.status == RunStatus.FAILED
    assert any(event.event == "execution.cancelled" for event in run.events)
    assert audit == (True, None)


def test_single_metric_result_has_a_safe_generic_explanation():
    async def execute_single(_: str, __: dict, ___: list[str]) -> ExecutionResult:
        return ExecutionResult(
            columns=["branch_name", "loan_balance"],
            rows=[["上海分行", 1790000]],
            row_count=1,
            duration_ms=5,
        )

    run = asyncio.run(
        Workflow(Store(), execute_single, fake_retrieve, fake_snapshot).start(
            "demo-session", "按分行查询贷款余额", approval_mode="disabled"
        )
    )
    assert run.status == RunStatus.COMPLETED
    assert run.answer and "上海分行" in run.answer and "贷款余额" in run.answer


def test_ambiguous_metric_stops_for_clarification():
    run = asyncio.run(Workflow(Store(), fake_execute, fake_retrieve, fake_snapshot).start("demo-session", "客户数"))
    assert run.status == RunStatus.CLARIFYING
    assert run.sql is None
    assert run.result is None


def test_empty_query_result_completes_normally():
    async def empty_execute(*args):
        return ExecutionResult(columns=['branch', 'loan_balance'], rows=[], row_count=0, duration_ms=1, bytes_returned=0)
    run = asyncio.run(Workflow(Store(), empty_execute, fake_retrieve, fake_snapshot).start('demo-session', '各分行贷款余额', approval_mode='disabled'))
    assert run.status == RunStatus.COMPLETED, run.error
    assert run.result.row_count == 0
    assert '没有匹配数据' in run.answer


def test_public_workflow_pauses_for_alignment_before_sql_then_resumes():
    async def scenario():
        store = Store()
        workflow = Workflow(store, fake_execute, fake_retrieve, fake_snapshot)
        authorization = AuthorizationContext(
            subject="alignment-user",
            username="口径确认用户",
            roles=["business_user"],
            branch_ids=[1],
            sensitivity_clearance="confidential",
        )
        run = await workflow.start_background(
            "demo-session",
            "按分行查询贷款余额",
            authorization,
        )
        for _ in range(50):
            if run.status == RunStatus.CLARIFYING:
                break
            await asyncio.sleep(0.01)
        paused = store.runs[run.id].model_copy(deep=True)
        completed = await workflow.align(run, "口径确认用户", {})
        return paused, completed

    paused, completed = asyncio.run(scenario())
    assert paused.status == RunStatus.CLARIFYING
    assert paused.requirement_draft is not None
    assert paused.requirement is None
    assert paused.plan is None
    assert paused.sql is None
    assert completed.status == RunStatus.COMPLETED
    assert completed.requirement is not None
    assert completed.requirement.confirmed_by == "口径确认用户"
    assert completed.plan is not None and completed.sql is not None
    assert completed.sql.requirement_hash == completed.requirement.content_hash
    assert completed.sql.requirement_version == completed.requirement.version
    assert any(event.event == "requirement.aligned" for event in completed.events)


def test_sensitive_query_waits_for_approval_then_executes():
    store = Store()
    workflow = Workflow(store, fake_execute, fake_retrieve, fake_snapshot)
    run = asyncio.run(workflow.start("demo-session", "查询贷款客户明细和手机号"))
    assert run.status == RunStatus.AWAITING_APPROVAL
    assert run.result is None
    assert run.approval and run.approval.status == "pending"

    completed = asyncio.run(workflow.approve(run, "approver-1", True, "用途和范围已核对"))
    assert completed.status == RunStatus.COMPLETED
    assert completed.approval and completed.approval.status == "approved"
    assert completed.result and completed.result.row_count == 1
    assert sum(event.event == "approval.required" for event in completed.events) == 1
    assert sum(event.event == "execution.completed" for event in completed.events) == 1


def test_rejected_approval_never_executes():
    store = Store()
    workflow = Workflow(store, fake_execute, fake_retrieve, fake_snapshot)
    run = asyncio.run(workflow.start("demo-session", "查询贷款客户明细和手机号"))
    rejected = asyncio.run(workflow.approve(run, "approver-1", False, "用途不充分"))
    assert rejected.status == RunStatus.FAILED
    assert rejected.result is None


def test_sql_change_invalidates_pending_approval():
    store = Store()
    workflow = Workflow(store, fake_execute, fake_retrieve, fake_snapshot)
    run = asyncio.run(workflow.start("demo-session", "查询贷款客户明细和手机号"))
    run.sql.artifact_hash = "changed"
    try:
        asyncio.run(workflow.approve(run, "approver-1", True, "批准"))
    except ValueError as exc:
        assert "失效" in str(exc)
    else:
        raise AssertionError("changed SQL must invalidate approval")
    assert run.approval and run.approval.status == "expired"


