"""Execution capabilities derived from scanned server metadata, not business rules."""

import re


def source_profile(source):
    connection = source.scan_result.get("connection") or {}
    version = str(connection.get("version") or connection.get("server_version") or "")
    vendor = source.type.lower()
    features = {}
    numbers = re.search(r"(\d+)\.(\d+)", version)
    if vendor == "mysql" and numbers:
        if "mariadb" in version.lower():
            match = re.search(r"(\d+)\.(\d+)(?:\.\d+)?-MariaDB", version, re.I)
            if match:
                vendor = "mariadb"
                features = dict.fromkeys(
                    ("window_functions", "common_table_expressions"),
                    tuple(map(int, match.groups())) >= (10, 2),
                )
        elif not any(name in version.lower() for name in ("tidb", "oceanbase")):
            features = dict.fromkeys(
                ("window_functions", "common_table_expressions"),
                tuple(map(int, numbers.groups())) >= (8, 0),
            )
    elif vendor in {"postgres", "postgresql"} and numbers:
        features = dict.fromkeys(
            ("window_functions", "common_table_expressions"),
            tuple(map(int, numbers.groups())) >= (8, 4),
        )
    return {"type": source.type, "vendor": vendor, "version": version or None, "features": features}


def validate_features(tree, profile):
    from sqlglot import exp
    from .sql_errors import SQLIssue

    features = profile.get("features", {})
    missing = []
    if features.get("window_functions") is False and tree.find(exp.Window):
        missing.append("窗口函数（含 ROW_NUMBER / OVER）")
    if features.get("common_table_expressions") is False and tree.find(exp.With):
        missing.append("WITH 公共表表达式")
    if missing:
        raise SQLIssue(
            f"目标 {profile['vendor']} {profile['version']} 不支持"
            + "、".join(missing)
            + "。请使用普通分组、派生表或相关子查询实现相同含义，保留原筛选、粒度与Top N先后顺序。"
        )
