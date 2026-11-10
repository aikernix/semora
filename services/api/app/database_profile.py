"""Execution capabilities derived from scanned server metadata, not business rules."""

def source_profile(source):
    connection = source.scan_result.get("connection") or {}
    version = str(connection.get("version") or connection.get("server_version") or "")
    vendor = source.type.lower()
    from semora_contracts.database import SQL_BASELINE_ID, SQL_OPTIONAL_FEATURES
    features = {feature: False for feature in SQL_OPTIONAL_FEATURES.values()}
    feature_status = {feature: "unverified" for feature in features}
    capabilities = source.scan_result.get("capabilities", {})
    probes = connection.get("probes", {})
    for key, feature in SQL_OPTIONAL_FEATURES.items():
        probe = probes.get(key) or capabilities.get(key)
        if isinstance(probe, dict):
            feature_status[feature] = probe.get("status", "unverified")
            features[feature] = probe.get("status") == "verified"
    # Version metadata is descriptive. It cannot turn an untested feature on.
    if "mariadb" in version.lower(): vendor = "mariadb"
    return {"type": source.type, "vendor": connection.get("product") or vendor,
            "version": version or None, "features": features, "feature_status": feature_status,
            "sql_baseline": {"id": SQL_BASELINE_ID,
                "preferred_syntax": "普通SELECT、INNER/LEFT JOIN、WHERE、GROUP BY/HAVING、派生表、CASE/COALESCE；可选能力仅按实测结果启用",
                "pagination": capabilities.get("pagination_strategy") or ("rownum" if source.type=="oracle" else "sqlglot")},
            "dialect": capabilities.get("dialect"),"compatibility_mode": connection.get("compatibility_mode"),
            "driver_version": connection.get("driver_version"), "capabilities": capabilities}


def validate_features(tree, profile):
    from sqlglot import exp
    from .sql_errors import SQLIssue

    features = profile.get("features", {})
    missing = []
    if features.get("window_functions") is False and tree.find(exp.Window):
        missing.append("窗口函数（含 ROW_NUMBER / OVER）")
    if features.get("common_table_expressions") is False and tree.find(exp.With):
        missing.append("WITH 公共表表达式")
    if features.get("recursive_common_table_expressions") is False and any(w.args.get("recursive") for w in tree.find_all(exp.With)):
        missing.append("递归WITH")
    if profile.get("dialect")=="tsql" and not features.get("offset_pagination") and tree.find(exp.Offset):
        missing.append("OFFSET/FETCH 分页（普通取数优先 TOP）")
    if missing:
        raise SQLIssue(
            f"目标 {profile['vendor']} {profile['version']} 的当前兼容基线未启用"
            + "、".join(missing)
            + "（未验证或探测未通过）。请使用普通分组、派生表或相关子查询实现相同含义，保留原筛选、粒度与Top N先后顺序；不能等价改写时明确报告。"
        )
