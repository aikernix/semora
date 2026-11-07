from __future__ import annotations
from query_stages import projected_table

import base64
from functools import lru_cache
import hashlib
import json
import re
from typing import Any

from semora_contracts import QueryPlan, SQLArtifact
import sqlglot
from sqlglot import exp
from wren_core import SessionContext

from planner import compile_plan


IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
REFERENCE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)\b")
PLACEHOLDER = re.compile(r"%\(([A-Za-z_][A-Za-z0-9_]*)\)s")


class WrenPlanningError(ValueError):
    pass


def _identifier(value: str, label: str) -> str:
    if not IDENTIFIER.fullmatch(value):
        raise WrenPlanningError(f"{label} 不是安全标识符：{value}")
    return value


def _table_reference(value: str) -> dict[str, str]:
    parts = value.split(".")
    if len(parts) == 1:
        return {"schema": "public", "table": _identifier(parts[0], "物理表")}
    if len(parts) == 2:
        return {
            "schema": _identifier(parts[0], "物理 schema"),
            "table": _identifier(parts[1], "物理表"),
        }
    raise WrenPlanningError(f"物理表只能使用 table 或 schema.table：{value}")


def _wren_type(value: str) -> str:
    return {
        "string": "varchar",
        "text": "varchar",
        "decimal": "decimal",
        "number": "double",
        "float": "double",
        "datetime": "timestamp",
        "timestamp": "timestamp",
        "boolean": "boolean",
        "bool": "boolean",
        "date": "date",
        "integer": "integer",
        "int": "integer",
    }.get(value.lower(), value.lower())


def build_wren_manifest(ontology: dict[str, Any]) -> dict[str, Any]:
    objects = {str(item["name"]): item for item in ontology.get("objects", [])}
    hidden_columns: dict[str, set[str]] = {name: set() for name in objects}
    for relation in ontology.get("relations", []):
        condition = sqlglot.parse_one(str(relation.get("condition", "")), read="postgres")
        for column in condition.find_all(exp.Column):
            object_name, column_name = column.table, column.name
            if object_name not in objects:
                raise WrenPlanningError(f"关系引用未知对象：{object_name}")
            hidden_columns[object_name].add(column_name)

    models: list[dict[str, Any]] = []
    for object_name, item in objects.items():
        _identifier(object_name, "本体对象")
        primary_key = _identifier(str(item["primaryKey"]), f"{object_name} 主键") if item.get("primaryKey") else ""
        column_types: dict[str, str] = {primary_key: "integer"} if primary_key else {}
        for prop in item.get("properties", []):
            physical = _identifier(str(prop["column"]), f"{object_name}.{prop['name']} 列")
            column_types[physical] = _wren_type(str(prop.get("type") or "varchar"))
        for column in hidden_columns[object_name]:
            column_types.setdefault(_identifier(column, f"{object_name} 关系列"), "integer")
        columns = [
            {
                "name": column,
                "type": data_type,
                **({"isPrimaryKey": True} if column == primary_key else {}),
            }
            for column, data_type in sorted(column_types.items())
        ]
        projection = projected_table(item)
        if projection != item['table'] and set(column_types) != {p['column'] for p in item['properties']}:
            raise WrenPlanningError('去重投影不能隐式添加未声明列')
        models.append(
            {
                "name": object_name,
                **({"refSql": projection[1:-1]} if projection != item["table"] else {"tableReference": _table_reference(str(item["table"]))}),
                **({"primaryKey": primary_key} if primary_key else {}),
                "columns": columns,
            }
        )

    cardinality = {
        "1:1": "ONE_TO_ONE",
        "1:N": "ONE_TO_MANY",
        "N:1": "MANY_TO_ONE",
        "N:N": "MANY_TO_MANY",
    }
    relationships = []
    for relation in ontology.get("relations", []):
        relationships.append(
            {
                "name": _identifier(str(relation["name"]), "关系名"),
                "models": [str(relation["from"]), str(relation["to"])],
                "joinType": cardinality.get(str(relation.get("cardinality") or ""), "MANY_TO_ONE"),
                "condition": str(relation["condition"]),
            }
        )
    return {
        "catalog": "semora",
        "schema": "public",
        "dataSource": "postgres",
        "models": models,
        "relationships": relationships,
    }


@lru_cache(maxsize=64)
def _context(manifest_json: str) -> SessionContext:
    encoded = base64.b64encode(manifest_json.encode("utf-8")).decode("ascii")
    return SessionContext(encoded)


def _model_sql(physical_sql: str, ontology: dict[str, Any]) -> str:
    modeled = physical_sql
    for item in sorted(ontology.get("objects", []), key=lambda value: len(str(value["table"])), reverse=True):
        object_name = _identifier(str(item["name"]), "本体对象")
        table = str(item["table"])
        projection = projected_table(item)
        if projection != table:
            modeled = modeled.replace(f'{projection} {object_name}', f'{object_name} {object_name}')
            continue
        escaped = re.escape(table)
        modeled, count = re.subn(
            rf"\b(FROM|JOIN)\s+{escaped}\s+{re.escape(object_name)}\b",
            lambda match: f"{match.group(1)} {object_name} {object_name}",
            modeled,
            flags=re.IGNORECASE,
        )
        # An unused model may share a physical table or its name prefix with
        # the selected model. Only the exact FROM/JOIN + model alias matches
        # above are references; substring presence is not an unresolved table.
    return modeled


def _positional_bindings(sql: str, parameters: dict[str, Any]) -> tuple[str, list[str]]:
    names: list[str] = []

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in parameters:
            raise WrenPlanningError(f"SQL 参数缺失：{name}")
        if name not in names:
            names.append(name)
        return f"${names.index(name) + 1}"

    return PLACEHOLDER.sub(replace, sql), names


def _restore_bindings(sql: str, names: list[str]) -> str:
    def replace(match: re.Match[str]) -> str:
        index = int(match.group(1)) - 1
        if index < 0 or index >= len(names):
            raise WrenPlanningError(f"Wren 返回未知参数位置：${index + 1}")
        return f"%({names[index]})s"

    return re.sub(r"\$(\d+)\b", replace, sql)


def compile_with_wren(plan: QueryPlan, ontology: dict[str, Any]) -> SQLArtifact:
    deterministic = compile_plan(plan, ontology)
    published_manifest = ontology.get("_wrenArtifact")
    if published_manifest is not None and not isinstance(published_manifest, dict):
        raise WrenPlanningError("本体发布的 Wren 产物格式无效")
    manifest = published_manifest or build_wren_manifest(ontology)
    manifest_json = json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    modeled_sql = _model_sql(deterministic.physical_sql, ontology)
    positional_sql, binding_names = _positional_bindings(modeled_sql, deterministic.parameters)
    try:
        planned_sql = _context(manifest_json).transform_sql(positional_sql)
        planned_sql = sqlglot.transpile(planned_sql, write="postgres")[0]
    except Exception as exc:
        raise WrenPlanningError(f"Wren Core 语义展开失败：{type(exc).__name__}: {exc}") from exc
    physical_sql = _restore_bindings(planned_sql, binding_names)
    semantic_sql = _restore_bindings(positional_sql, binding_names)
    artifact_hash = hashlib.sha256(
        (
            f"wren-core-py:0.8.2\n{manifest_json}\n{deterministic.data_source_id}\n{semantic_sql}\n{physical_sql}\n"
            f"{sorted(deterministic.parameters.items())}\n{sorted(deterministic.sensitive_columns)}"
        ).encode("utf-8")
    ).hexdigest()
    return deterministic.model_copy(
        update={
            "semantic_sql": semantic_sql,
            "physical_sql": physical_sql,
            "artifact_hash": artifact_hash,
        }
    )
