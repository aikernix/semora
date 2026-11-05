from __future__ import annotations

from semora_contracts import AuthorizationContext, QueryRun, RunStatus

from app.store import Store


def test_audit_index_supports_search_status_and_event_count(monkeypatch) -> None:
    monkeypatch.delenv("METADATA_DATABASE_URL", raising=False)
    repository = Store()
    run = QueryRun(
        session_id="audit-session",
        domain="retail_credit",
        question="查询贷款余额",
        status=RunStatus.COMPLETED,
        authorization=AuthorizationContext(
            subject="owner-1",
            username="审计样例用户",
            roles=["analyst"],
        ),
    )
    repository.add_run(run)
    repository.audit.append(run.id, run.trace_id, "run.started", "owner", {"status": "received"})
    repository.audit.append(run.id, run.trace_id, "answer.completed", "owner", {"status": "completed"})

    result = repository.list_audit_runs("贷款余额", "completed", 10)

    assert len(result) == 1
    assert result[0]["id"] == run.id
    assert result[0]["event_count"] == 2
    assert result[0]["owner_username"] == "审计样例用户"
    assert repository.list_audit_runs("贷款余额", "failed", 10) == []
    assert repository.list_audit_runs("不存在的问题", "", 10) == []
