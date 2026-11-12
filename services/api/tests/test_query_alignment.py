from semora_contracts import LogicalQueryPlan, SQLProgram, SQLProgramStep

from app.query_alignment import inspect_alignment, validate_logical_refs


def materials():
    return {
        "objects": [
            {"object": {"name": "organization", "table": "org", "properties": [
                {"name": "name", "column": "org_name"},
            ]}},
            {"object": {"name": "loan", "table": "loan", "properties": [
                {"name": "customer_id", "column": "customer_id"},
                {"name": "balance", "column": "balance"},
                {"name": "status", "column": "status"},
            ]}},
        ],
        "relations": [{"name": "organization_has_loan", "from": "organization", "to": "loan"}],
    }


def plan():
    return LogicalQueryPlan(
        subject_refs=["loan"],
        output_refs=["organization.name"],
        dimension_refs=["organization.name"],
        metrics=[{"id": "balance_total", "label": "余额合计", "operation": "sum", "field_refs": ["loan.balance"]}],
        filters=[{"field_refs": ["loan.status"], "operator": "eq", "value": "active", "description": "只包含有效记录"}],
        relation_refs=["organization_has_loan"],
        sort=[{"target_ref": "balance_total", "direction": "desc"}],
        limit=10,
    )


def program(sql):
    return SQLProgram(summary="按机构统计余额", steps=[SQLProgramStep(
        name="q", purpose="按机构统计余额", data_source_id="source", sql=sql,
        output_columns=["org_name", "balance_total"],
    )])


def test_logical_plan_references_are_checked_against_published_materials():
    assert validate_logical_refs(plan(), materials()) == []
    broken = plan().model_copy(deep=True)
    broken.output_refs = ["loan.missing"]
    assert validate_logical_refs(broken, materials()) == ["未知属性引用：loan.missing"]


def test_ast_alignment_accepts_matching_query():
    sql = """SELECT o.org_name, SUM(l.balance) AS balance_total
             FROM org o JOIN loan l ON o.id=l.org_id
             WHERE l.status=:status GROUP BY o.org_name
             ORDER BY balance_total DESC LIMIT 10"""
    assert inspect_alignment(plan(), program(sql), materials()) == []


def test_ast_alignment_reports_only_concrete_missing_operations():
    sql = "SELECT o.org_name, l.balance FROM org o JOIN loan l ON o.id=l.org_id"
    kinds = {item["kind"] for item in inspect_alignment(plan(), program(sql), materials())}
    assert {"missing_group", "missing_metric", "missing_filter", "missing_sort", "missing_limit"} <= kinds
