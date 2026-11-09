"""Dialect boundaries across authoring, review, conversion and bounded repair."""

import asyncio
import json

import pytest
import sqlglot

from semora_agent.sql_prompts import AUTHOR, COVERAGE, REPAIR, SQL_CONTRACT
from semora_contracts import RunStatus
from app.executor_errors import ExecutorOperationError
from app.sql_dialect import compile_step
from app.sql_errors import SQLIssue, issue_data
from app import sql_authoring
from test_sql_native import native_harness, sources as source_fixture, step

sources = source_fixture


@pytest.mark.parametrize("prompt", [AUTHOR, COVERAGE, REPAIR])
def test_all_sql_writers_share_the_same_baseline_contract(prompt):
    assert SQL_CONTRACT in prompt
    assert "PostgreSQL" in SQL_CONTRACT
    assert "目标方言" in SQL_CONTRACT


@pytest.mark.parametrize("engine,source", [("source", "a"), ("source", "b"), ("local", "")])
@pytest.mark.parametrize("expression", [
    "CURRENT_DATE - INTERVAL '30 days'",
    "TO_CHAR(CURRENT_DATE - INTERVAL '30 days', 'YYYY-MM-DD')",
    "CAST(12.5 AS DECIMAL(10, 2))",
    "COALESCE(NULL, 7)",
    "SUBSTRING('abcdef' FROM 2 FOR 3)",
    "ROW_NUMBER() OVER (ORDER BY x.n)",
])
def test_common_baseline_expressions_convert_for_each_supported_engine(sources, engine, source, expression):
    baseline = f"SELECT {expression} AS id FROM (SELECT 1 AS n) AS x"
    artifact = compile_step(step(engine=engine, source=source, sql=baseline), sources)
    sqlglot.parse_one(artifact.physical_sql, read=artifact.dialect)
    assert "INTERVAL (INTERVAL" not in artifact.physical_sql
    assert artifact.semantic_sql == baseline
    assert artifact.compilation["source_dialect"] == "postgres"
    assert artifact.compilation["baseline_hash"]


def test_conversion_error_retains_its_stage(sources):
    with pytest.raises(SQLIssue) as caught:
        compile_step(step(sql="SELECT id FROM events TABLESAMPLE BERNOULLI (10)", source="b"), sources)
    assert issue_data(caught.value, "baseline_validation")["error_stage"] == "dialect_conversion"
    assert issue_data(ExecutorOperationError("syntax", 1064), "explain")["error_stage"] == "explain"


def test_review_receives_baseline_contract_and_target_profile(monkeypatch):
    from app.datasources import datasource_service

    program = {"summary": "Total", "steps": [{"name": "q", "purpose": "Total",
        "data_source_id": "source", "sql": "SELECT SUM(amount) AS total FROM items",
        "output_columns": ["total"]}]}
    w, session, _, _, model, _ = native_harness(monkeypatch, program=program)
    datasource_service.repository.get("source").scan_result = {"connection": {"version": "16.1"}}
    model.complete_json.side_effect = [program, {"edits": []}]
    run = asyncio.run(w.start(session.id, "Total amounts", domain="test"))
    assert run.status == RunStatus.COMPLETED, run.error
    for call in model.complete_json.await_args_list:
        payload = json.loads(call.args[1])
        assert payload["sql_contract"] == {"authoring_dialect": "postgres", "parameter_style": ":name"}
        assert payload["data_sources"][0]["version"] == "16.1"


def test_explain_repair_edits_baseline_then_recompiles(monkeypatch):
    w, session, execute, estimate, model, _ = native_harness(monkeypatch)
    estimate.side_effect = [ExecutorOperationError("syntax", 1064), {"estimated_rows": 2}]
    explanation = "修正SELECT输出的id别名，保留原排序，交由数据库重新校验。"
    model.complete_json.side_effect = [model.complete_json.return_value,
        {"sql": "SELECT id AS id FROM items ORDER BY id", "change_summary": explanation}]
    run = asyncio.run(w.start(session.id, "List items", domain="test"))
    assert run.status == RunStatus.COMPLETED, run.error
    # Technical repair notes are persisted verbatim, without another model call
    # to turn them into business copy.
    assert model.complete_json.await_count == 2
    payload = json.loads(model.complete_json.await_args.args[1])
    assert "actual_sql" not in payload and "error" not in payload
    assert payload["editable_baseline"]["dialect"] == "postgres"
    assert payload["editable_baseline"]["sql"] == "SELECT id FROM items ORDER BY id"
    diagnostic = payload["execution_diagnostic"]
    assert "LIMIT 1001" in diagnostic["target_sql"]
    assert diagnostic["error"]["error_stage"] == "explain"
    assert "AS id" in estimate.await_args.args[0]
    assert execute.await_count == 1
    assert run.source_artifacts["q"].semantic_sql == "SELECT id AS id FROM items ORDER BY id"
    event = next(e for e in run.events if e.event == "sql.repaired")
    assert event.data["summary"] == explanation
    assert event.data["baseline_before_hash"] != event.data["baseline_after_hash"]


def test_compile_repair_does_not_expose_stale_target_as_current(monkeypatch):
    from app.sql_execution import SQLExecution

    w, session, _, _, model, _ = native_harness(monkeypatch)
    run = asyncio.run(w.start(session.id, "List items", domain="test"))
    run.plan.sql_program.steps[0].sql = "SELECT missing FROM items"
    model.complete_json.return_value = {"sql": "SELECT id FROM items", "change_summary": "Correct reference"}
    asyncio.run(sql_authoring.repair_step(w, run, "q", {"error_type": "sql", "error_stage": "baseline_validation"}))
    payload = json.loads(model.complete_json.await_args.args[1])
    assert payload["editable_baseline"]["sql"] == "SELECT missing FROM items"
    assert payload["execution_diagnostic"]["target_sql"] is None
    assert payload["execution_diagnostic"]["target_dialect"] == "postgres"
    # The replacement is compiled from the new baseline, never the old artifact.
    artifact = SQLExecution(w, run, {"dataSourceId": "source", "objects": [{"table": "items"}]}, {}).artifact(run.plan.sql_program.steps[0])
    assert artifact.semantic_sql == "SELECT id FROM items"
