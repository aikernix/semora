from app.gate import review_sql


SAFE_SQL = """SELECT b.name AS branch, SUM(l.outstanding_balance) AS loan_balance
FROM loans l JOIN branches b ON b.id = l.branch_id
WHERE b.id = ANY(%(branch_ids)s)
GROUP BY b.name LIMIT 1000"""


def test_safe_aggregate_is_allowed_without_approval():
    decision = review_sql(SAFE_SQL)
    assert decision.allowed is True
    assert decision.requires_approval is False
    assert decision.risk_level == "low"


def test_sensitive_detail_requires_approval():
    sql = """SELECT b.name AS branch, c.mobile FROM customers c
    JOIN branches b ON b.id = c.branch_id WHERE b.id = ANY(%(branch_ids)s) LIMIT 1000"""
    decision = review_sql(sql)
    assert decision.allowed is True
    assert decision.requires_approval is True
    assert decision.risk_level == "medium"


def test_large_estimated_result_requires_approval_when_enabled():
    decision = review_sql(
        SAFE_SQL,
        estimated_rows=250_000,
        large_result_threshold=100_000,
        approve_large_result=True,
    )
    assert decision.allowed is True
    assert decision.requires_approval is True
    assert any(check.name == "estimated_result_size" and not check.passed for check in decision.checks)


def test_disabled_mode_does_not_claim_sensitive_approval_is_required():
    sql = """SELECT b.name AS branch, c.mobile FROM customers c
    JOIN branches b ON b.id = c.branch_id WHERE b.id = ANY(%(branch_ids)s) LIMIT 1000"""
    decision = review_sql(sql, approval_mode="disabled")
    sensitive_check = next(check for check in decision.checks if check.name == "sensitive_data")
    assert decision.requires_approval is False
    assert "未要求" in sensitive_check.message


def test_cross_organization_flag_uses_policy_trigger():
    decision = review_sql(SAFE_SQL, cross_organization=True, approve_cross_organization=True)
    assert decision.requires_approval is True


def test_write_and_multiple_statements_are_blocked():
    decision = review_sql("UPDATE loans SET status = 'closed'; SELECT * FROM loans LIMIT 1")
    assert decision.allowed is False
    failed = {check.name for check in decision.checks if not check.passed}
    assert "single_statement" in failed
    assert "readonly" in failed


def test_missing_organization_scope_is_blocked():
    decision = review_sql("SELECT loan_no FROM loans LIMIT 1000")
    assert decision.allowed is False
    assert any(check.name == "organization_scope" and not check.passed for check in decision.checks)


def test_column_abac_blocks_unauthorized_column():
    decision = review_sql(
        "SELECT b.name AS branch, c.mobile FROM customers c JOIN branches b ON b.id=c.branch_id WHERE b.id=1 LIMIT 1000",
        allowed_columns={"name", "id", "branch_id"},
    )
    assert decision.allowed is False
    assert any(check.name == "column_abac" and not check.passed for check in decision.checks)

