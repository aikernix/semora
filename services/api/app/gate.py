from __future__ import annotations

import re

import sqlglot
from sqlglot import exp

from semora_contracts import GateCheck, GateDecision


FORBIDDEN = (exp.Insert, exp.Update, exp.Delete, exp.Create, exp.Drop, exp.Alter, exp.Command, exp.Merge)


def review_sql(
    sql: str,
    *,
    dialect: str = "postgres",
    require_organization_scope: bool = True,
    approval_mode: str = "risk_based",
    allowed_columns: set[str] | None = None,
    sensitive_columns: set[str] | None = None,
    approve_sensitive_detail: bool = True,
    estimated_rows: int | None = None,
    estimate_unavailable: bool = False,
    large_result_threshold: int = 100_000,
    approve_large_result: bool = True,
    cross_organization: bool = False,
    approve_cross_organization: bool = True,
    max_sql_rows: int = 1000,
) -> GateDecision:
    checks: list[GateCheck] = []
    # SQLGlot intentionally parses SQL, not DB-driver parameter syntax. Replace
    # psycopg named placeholders only in the parser copy; the executable SQL and
    # bound parameters remain separate and unchanged.
    parser_sql = re.sub(r"%\([A-Za-z_][A-Za-z0-9_]*\)s", "ARRAY[1]", sql)
    try:
        statements = sqlglot.parse(parser_sql, read=dialect)
    except sqlglot.errors.ParseError as exc:
        return GateDecision(
            allowed=False,
            risk_level="high",
            requires_approval=False,
            checks=[GateCheck(name="syntax", passed=False, severity="blocking", message=str(exc))],
        )

    single = len(statements) == 1
    checks.append(GateCheck(name="single_statement", passed=single, severity="blocking", message="仅允许单条 SQL"))
    ast = statements[0] if statements else None
    readonly = bool(ast) and not any(ast.find(node) for node in FORBIDDEN)
    readonly = readonly and isinstance(ast, (exp.Select, exp.Union, exp.Intersect, exp.Except))
    checks.append(GateCheck(name="readonly", passed=readonly, severity="blocking", message="仅允许 SELECT 或只读 CTE"))

    columns = {column.name.lower() for column in ast.find_all(exp.Column)} if ast else set()
    forbidden_columns = columns - allowed_columns if allowed_columns is not None else set()
    checks.append(GateCheck(
        name="column_abac",
        passed=not forbidden_columns,
        severity="blocking",
        message="字段权限通过" if not forbidden_columns else f"未授权字段：{', '.join(sorted(forbidden_columns))}",
    ))

    normalized = re.sub(r"\s+", " ", sql.lower())
    has_scope = not require_organization_scope or "branch_id" in normalized or "group by b.name" in normalized or "group by branch" in normalized
    checks.append(GateCheck(name="organization_scope", passed=has_scope, severity="blocking", message="已包含机构范围或机构聚合" if require_organization_scope else "当前未配置机构范围过滤，按数据源可读范围查询"))

    try:
        has_limit = bool(ast and ast.args.get('limit') and 0 < int(ast.args['limit'].expression.name) <= max_sql_rows)
    except (ValueError, AttributeError, TypeError):
        has_limit = False
    checks.append(GateCheck(name="row_limit", passed=has_limit, severity="blocking", message=f"SQL读取上限不超过 {max_sql_rows} 行"))

    sensitive = bool(sensitive_columns)
    sensitive_requires_approval = (
        sensitive and approve_sensitive_detail and approval_mode in {"all", "risk_based"}
    )
    checks.append(GateCheck(
        name="sensitive_data",
        passed=not sensitive,
        severity="warning" if sensitive else "info",
        message=(
            "包含敏感明细，执行前必须审批"
            if sensitive_requires_approval
            else "包含敏感明细，策略未要求人工审批"
            if sensitive
            else "未查询敏感明细"
        ),
    ))
    large_result = estimated_rows is not None and estimated_rows > large_result_threshold
    checks.append(GateCheck(
        name="estimated_result_size",
        passed=not large_result and not estimate_unavailable,
        severity="warning" if large_result or estimate_unavailable else "info",
        message=(
            '成本估算不可用，未做行数预判'
            if estimate_unavailable
            else '尚未进行行数估算'
            if estimated_rows is None
            else
            f"数据库估算 {estimated_rows:,} 行，超过审批阈值 {large_result_threshold:,} 行"
            if large_result
            else f"数据库估算 {estimated_rows or 0:,} 行，未超过审批阈值"
        ),
    ))
    checks.append(GateCheck(
        name="cross_organization",
        passed=not cross_organization,
        severity="warning" if cross_organization else "info",
        message="ABAC 标记为跨机构查询" if cross_organization else "查询未超出授权机构边界",
    ))
    blocking = any(not check.passed and check.severity == "blocking" for check in checks)
    risk_trigger = (
        sensitive and approve_sensitive_detail
        or (large_result or estimate_unavailable) and approve_large_result
        or cross_organization and approve_cross_organization
    )
    requires_approval = approval_mode == "all" or (approval_mode == "risk_based" and risk_trigger)
    risk = "high" if blocking else "medium" if requires_approval or estimate_unavailable else "low"
    return GateDecision(allowed=not blocking, risk_level=risk, requires_approval=requires_approval, checks=checks)

