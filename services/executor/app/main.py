from __future__ import annotations

import json
import os
import re
import threading
import time
from typing import Any

import psycopg
import sqlglot
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field
from sqlglot import exp
from semora_telemetry import configure_telemetry
from . import mysql_adapter


app = FastAPI(title="Semora Read-only Executor", version="0.1.0")
configure_telemetry(app, "semora-executor")
FORBIDDEN = (exp.Insert, exp.Update, exp.Delete, exp.Create, exp.Drop, exp.Alter, exp.Command, exp.Merge)
SENSITIVE_RESULT_COLUMNS = {
    "name",
    "mobile",
    "phone",
    "id_no",
    "id_number",
    "customer_name",
    "customer_mobile",
    "customer_phone",
    "customer_id_no",
    "customer_id_number",
}
ACTIVE_QUERIES: dict[str, psycopg.Connection] = {}
ACTIVE_QUERIES_LOCK = threading.Lock()


class DataSourceInput(BaseModel):
    datasource_id: str = Field(min_length=1, max_length=64)
    type: str = Field(min_length=1, max_length=32)
    secret_ref: str = Field(pattern=r"^DATASOURCE_[A-Z0-9_]+_URL$", max_length=200)


class QueryInput(BaseModel):
    sql: str = Field(min_length=1, max_length=100_000)
    parameters: dict[str, Any] = Field(default_factory=dict)
    sensitive_columns: list[str] = Field(default_factory=list, max_length=1000)
    timeout_ms: int | None = Field(default=None, ge=100, le=30_000)
    max_rows: int | None = Field(default=None, ge=1, le=1_000)
    max_bytes: int | None = Field(default=None, ge=1_024, le=10 * 1024 * 1024)
    execution_id: str | None = Field(default=None, min_length=1, max_length=64)
    datasource: DataSourceInput


class EstimateInput(BaseModel):
    sql: str = Field(min_length=1, max_length=100_000)
    parameters: dict[str, Any] = Field(default_factory=dict)
    timeout_ms: int = Field(default=5_000, ge=100, le=5_000)
    datasource: DataSourceInput


def require_internal_token(token: str | None) -> None:
    expected = os.getenv("INTERNAL_TOKEN", "semora-dev-internal")
    if os.getenv("REQUIRE_INTERNAL_TOKEN", "false").lower() == "true" and token != expected:
        raise HTTPException(401, "invalid internal token")


def resolve_datasource(body: DataSourceInput) -> str:
    if body.type not in {"postgresql", "mysql"}:
        raise HTTPException(422, f"当前版本不支持数据源类型：{body.type}")
    value = os.getenv(body.secret_ref)
    if not value:
        raise HTTPException(422, f"Executor 未配置密钥引用：{body.secret_ref}")
    return value


def inspect_connection(dsn: str) -> dict[str, Any]:
    try:
        with psycopg.connect(dsn, autocommit=False, connect_timeout=5) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """SELECT current_database(), current_user,
                    current_setting('default_transaction_read_only'),
                    rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolbypassrls,
                    has_schema_privilege(current_user, 'public', 'CREATE')
                    FROM pg_roles WHERE rolname = current_user"""
                )
                row = cursor.fetchone()
                connection.rollback()
    except Exception as exc:
        raise HTTPException(502, f"数据源连接失败：{type(exc).__name__}: {exc}") from exc
    assert row is not None
    dangerous = any(bool(value) for value in row[3:8]) or bool(row[8])
    read_only = str(row[2]).lower() == "on" and not dangerous
    return {
        "database": row[0],
        "user": row[1],
        "default_transaction_read_only": row[2],
        "dangerous_role_capabilities": dangerous,
        "read_only": read_only,
    }


def sensitivity_level(table: str, column: str) -> str:
    normalized = column.lower()
    restricted = {"id_no", "id_number", "identity_no", "card_no", "account_no"}
    confidential = {"name", "customer_name", "mobile", "phone", "email", "address"}
    if normalized in restricted:
        return "restricted"
    if normalized in confidential or any(token in normalized for token in ("mobile", "phone", "email")):
        return "confidential"
    return "internal"


def scan_postgresql(dsn: str) -> dict[str, Any]:
    connection_info = inspect_connection(dsn)
    if not connection_info["read_only"]:
        raise HTTPException(409, "数据源账号不是只读账号，拒绝扫描和注册")
    try:
        with psycopg.connect(dsn, autocommit=False, connect_timeout=5) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SET TRANSACTION READ ONLY")
                cursor.execute(
                    """SELECT table_schema,table_name,column_name,data_type,is_nullable,ordinal_position
                    FROM information_schema.columns
                    WHERE table_schema NOT IN ('pg_catalog','information_schema')
                    ORDER BY table_schema,table_name,ordinal_position"""
                )
                column_rows = cursor.fetchall()
                cursor.execute(
                    """SELECT n.nspname,c.relname,a.attname
                    FROM pg_constraint con
                    JOIN pg_class c ON c.oid=con.conrelid
                    JOIN pg_namespace n ON n.oid=c.relnamespace
                    JOIN LATERAL unnest(con.conkey) WITH ORDINALITY k(attnum,ord) ON true
                    JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=k.attnum
                    WHERE con.contype='p'
                      AND n.nspname NOT IN ('pg_catalog','information_schema')
                      AND has_table_privilege(current_user,con.conrelid,'SELECT')
                    ORDER BY n.nspname,c.relname,k.ord"""
                )
                primary_keys = {(row[0], row[1], row[2]) for row in cursor.fetchall()}
                cursor.execute(
                    """SELECT ns.nspname,src.relname,sa.attname,
                    nt.nspname,tgt.relname,ta.attname
                    FROM pg_constraint con
                    JOIN pg_class src ON src.oid=con.conrelid
                    JOIN pg_namespace ns ON ns.oid=src.relnamespace
                    JOIN pg_class tgt ON tgt.oid=con.confrelid
                    JOIN pg_namespace nt ON nt.oid=tgt.relnamespace
                    JOIN LATERAL unnest(con.conkey) WITH ORDINALITY sk(attnum,ord) ON true
                    JOIN LATERAL unnest(con.confkey) WITH ORDINALITY tk(attnum,ord) ON tk.ord=sk.ord
                    JOIN pg_attribute sa ON sa.attrelid=src.oid AND sa.attnum=sk.attnum
                    JOIN pg_attribute ta ON ta.attrelid=tgt.oid AND ta.attnum=tk.attnum
                    WHERE con.contype='f'
                      AND ns.nspname NOT IN ('pg_catalog','information_schema')
                      AND has_table_privilege(current_user,con.conrelid,'SELECT')
                      AND has_table_privilege(current_user,con.confrelid,'SELECT')
                    ORDER BY ns.nspname,src.relname,sk.ord"""
                )
                foreign_keys = [
                    {"from": f"{row[0]}.{row[1]}.{row[2]}", "to": f"{row[3]}.{row[4]}.{row[5]}"}
                    for row in cursor.fetchall()
                ]
                connection.rollback()
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(502, f"Schema 扫描失败：{type(exc).__name__}: {exc}") from exc
    tables: dict[tuple[str, str], dict[str, Any]] = {}
    sensitive_columns: list[dict[str, str]] = []
    for schema, table, column, data_type, nullable, _ in column_rows:
        value = tables.setdefault((schema, table), {"schema": schema, "name": table, "columns": []})
        level = sensitivity_level(table, column)
        value["columns"].append({
            "name": column,
            "type": data_type,
            "nullable": nullable == "YES",
            "primary_key": (schema, table, column) in primary_keys,
            "sensitivity": level,
        })
        if level in {"confidential", "restricted"}:
            sensitive_columns.append({"field": f"{schema}.{table}.{column}", "level": level})
    return {
        "status": "completed",
        "connection": connection_info,
        "tables": list(tables.values()),
        "foreign_keys": foreign_keys,
        "sensitive_columns": sensitive_columns,
    }


def assert_readonly(sql: str, dialect: str = "postgres") -> None:
    parser_sql = re.sub(r"%\([A-Za-z_][A-Za-z0-9_]*\)s", "ARRAY[1]", sql)
    try:
        statements = sqlglot.parse(parser_sql, read=dialect)
    except sqlglot.errors.ParseError as exc:
        raise HTTPException(400, f"SQL 语法错误：{exc}") from exc
    if len(statements) != 1:
        raise HTTPException(400, "Executor 仅允许单条 SQL")
    ast = statements[0]
    if not isinstance(ast, (exp.Select, exp.Union, exp.Intersect, exp.Except)) or any(ast.find(node) for node in FORBIDDEN):
        raise HTTPException(400, "Executor 拒绝非只读 SQL")
    limit = ast.args.get("limit")
    if limit is None:
        raise HTTPException(400, "Executor 要求显式 LIMIT")
    if ast.find(exp.Into) or ast.find(exp.Lock):
        raise HTTPException(400, "Executor 拒绝导出文件或锁定数据")
    functions = {func.sql_name().upper() if not isinstance(func, exp.Anonymous) else func.name.upper() for func in ast.find_all(exp.Func)}
    if functions & {"SLEEP", "BENCHMARK", "LOAD_FILE", "GET_LOCK", "RELEASE_LOCK"}:
        raise HTTPException(400, "Executor 拒绝有副作用的函数")


def mask_sensitive(column: str, value: Any, sensitive_columns: set[str] | None = None) -> Any:
    """Mask sensitive values before they can leave the isolated executor."""
    normalized = column.lower()
    configured_sensitive = {item.lower() for item in sensitive_columns or set()}
    if value is None or (normalized not in SENSITIVE_RESULT_COLUMNS and normalized not in configured_sensitive):
        return value.isoformat() if hasattr(value, "isoformat") else value
    text = str(value)
    if normalized in {"mobile", "phone", "customer_mobile", "customer_phone"}:
        return f"{text[:3]}****{text[-4:]}" if len(text) >= 7 else "****"
    if normalized in {"id_no", "id_number", "customer_id_no", "customer_id_number"}:
        return f"{text[:3]}***********{text[-4:]}" if len(text) >= 7 else "****"
    return f"{text[:1]}{'*' * max(1, len(text) - 1)}"


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok", "service": "semora-executor"}


@app.post("/internal/v1/datasources/test")
def test_datasource(body: DataSourceInput, x_semora_internal_token: str | None = Header(default=None)) -> dict:
    require_internal_token(x_semora_internal_token)
    inspect = mysql_adapter.inspect if body.type == "mysql" else inspect_connection
    return {"datasource_id": body.datasource_id, **inspect(resolve_datasource(body))}


@app.post("/internal/v1/datasources/scan")
def scan_datasource(body: DataSourceInput, x_semora_internal_token: str | None = Header(default=None)) -> dict:
    require_internal_token(x_semora_internal_token)
    result = mysql_adapter.scan(resolve_datasource(body), sensitivity_level) if body.type == "mysql" else scan_postgresql(resolve_datasource(body))
    return {"datasource_id": body.datasource_id, **result}


@app.post("/internal/v1/query")
def execute_query(body: QueryInput, x_semora_internal_token: str | None = Header(default=None)) -> dict:
    require_internal_token(x_semora_internal_token)
    assert_readonly(body.sql, "mysql" if body.datasource.type == "mysql" else "postgres")
    dsn = resolve_datasource(body.datasource)
    started = time.perf_counter()
    # Environment values are hard security ceilings. A versioned system policy
    # may only make a query stricter, never relax the Executor boundary.
    hard_max_rows = int(os.getenv("MAX_RESULT_ROWS", "1000"))
    hard_max_bytes = int(os.getenv("MAX_RESULT_BYTES", str(10 * 1024 * 1024)))
    hard_timeout_ms = int(os.getenv("QUERY_TIMEOUT_MS", "30000"))
    max_rows = min(body.max_rows or hard_max_rows, hard_max_rows)
    max_bytes = min(body.max_bytes or hard_max_bytes, hard_max_bytes)
    timeout_ms = min(body.timeout_ms or hard_timeout_ms, hard_timeout_ms)
    connection: psycopg.Connection | None = None
    try:
        factory = (lambda: mysql_adapter.connect(dsn, timeout_ms)) if body.datasource.type == "mysql" else (lambda: psycopg.connect(dsn, autocommit=False))
        with factory() as connection:
            if body.execution_id:
                with ACTIVE_QUERIES_LOCK:
                    ACTIVE_QUERIES[body.execution_id] = connection
            with connection.cursor() as cursor:
                if body.datasource.type == "mysql":
                    mysql_adapter.begin_readonly(connection, timeout_ms)
                else:
                    cursor.execute("SET TRANSACTION READ ONLY")
                # PostgreSQL does not accept bind parameters in SET statements.
                # set_config keeps the timeout parameterized and transaction-local.
                if body.datasource.type != "mysql":
                    cursor.execute("SELECT set_config('statement_timeout', %s, true)", (f"{timeout_ms}ms",))
                cursor.execute(body.sql, body.parameters)
                columns = [column[0] for column in cursor.description or []]
                rows = cursor.fetchmany(max_rows + 1)
                truncated = len(rows) > max_rows
                rows = rows[:max_rows]
                sensitive_columns = {item.lower() for item in body.sensitive_columns}
                serialized_rows = [
                    [mask_sensitive(columns[index], value, sensitive_columns) for index, value in enumerate(row)]
                    for row in rows
                ]
                payload_bytes = len(json.dumps(serialized_rows, ensure_ascii=False, default=str).encode())
                if payload_bytes > max_bytes:
                    raise HTTPException(413, f"查询结果超过 {max_bytes} 字节限制")
                connection.rollback()
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(502, f"业务库执行失败：{type(exc).__name__}") from exc
    finally:
        if body.execution_id:
            with ACTIVE_QUERIES_LOCK:
                ACTIVE_QUERIES.pop(body.execution_id, None)
    duration_ms = int((time.perf_counter() - started) * 1000)
    return {
        "columns": columns,
        "rows": serialized_rows,
        "row_count": len(serialized_rows),
        "duration_ms": duration_ms,
        "truncated": truncated,
        "bytes_returned": payload_bytes,
        "summary": {
            "timeout_ms": timeout_ms,
            "row_limit": max_rows,
            "transaction": "read_only",
            "masked_columns": [
                column
                for column in columns
                if column.lower() in SENSITIVE_RESULT_COLUMNS or column.lower() in sensitive_columns
            ],
        },
    }


@app.post("/internal/v1/executions/{execution_id}/cancel")
def cancel_query(execution_id: str, x_semora_internal_token: str | None = Header(default=None)) -> dict:
    require_internal_token(x_semora_internal_token)
    with ACTIVE_QUERIES_LOCK:
        connection = ACTIVE_QUERIES.get(execution_id)
    if connection is None:
        return {"execution_id": execution_id, "accepted": False, "reason": "not_active"}
    try:
        if hasattr(connection, "cancel"):
            connection.cancel()
        else:
            # PyMySQL has no cancellation API; closing the socket interrupts I/O.
            # Server-side MAX_EXECUTION_TIME bounds remaining server work.
            connection.close()
    except Exception as exc:
        raise HTTPException(502, f"取消数据库查询失败：{exc}") from exc
    return {"execution_id": execution_id, "accepted": True}


@app.post("/internal/v1/estimate")
def estimate_query(body: EstimateInput, x_semora_internal_token: str | None = Header(default=None)) -> dict:
    """Estimate a read-only query without executing it or returning business data."""
    require_internal_token(x_semora_internal_token)
    assert_readonly(body.sql, "mysql" if body.datasource.type == "mysql" else "postgres")
    dsn = resolve_datasource(body.datasource)
    if body.datasource.type == "mysql":
        return mysql_adapter.estimate(dsn, body.sql, body.parameters, body.timeout_ms)
    try:
        with psycopg.connect(dsn, autocommit=False) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SET TRANSACTION READ ONLY")
                cursor.execute("SELECT set_config('statement_timeout', %s, true)", (f"{body.timeout_ms}ms",))
                cursor.execute(f"EXPLAIN (FORMAT JSON) {body.sql}", body.parameters)
                payload = cursor.fetchone()
                connection.rollback()
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(502, f"查询成本估算失败：{exc}") from exc
    if not payload:
        raise HTTPException(502, "查询成本估算未返回执行计划")
    document = payload[0]
    if isinstance(document, str):
        document = json.loads(document)
    plan = document[0]["Plan"]
    return {
        "estimated_rows": int(plan.get("Plan Rows", 0)),
        "total_cost": float(plan.get("Total Cost", 0)),
        "source": "postgresql-explain",
    }

