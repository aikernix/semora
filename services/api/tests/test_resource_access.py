from datetime import datetime, timezone

from semora_contracts import AuthorizationContext, QueryRun

from app.auth import Principal
from app.main import can_access_audit, can_access_run, can_access_session
from app.store import Session


def principal(subject: str, *roles: str) -> Principal:
    return Principal(subject=subject, username=subject, roles=frozenset(roles), claims={})


def test_session_is_visible_only_to_owner_or_admin() -> None:
    session = Session("s-1", "private", datetime.now(timezone.utc), "retail_credit", "owner-1")
    assert can_access_session(session, principal("owner-1", "analyst"))
    assert not can_access_session(session, principal("other", "analyst"))
    assert can_access_session(session, principal("admin-1", "admin"))


def test_legacy_shared_demo_session_remains_accessible() -> None:
    session = Session("demo-session", "demo", datetime.now(timezone.utc), "retail_credit", None)
    assert can_access_session(session, principal("any-user", "business_user"))


def test_run_is_visible_only_to_authorization_subject_or_admin() -> None:
    run = QueryRun(
        session_id="s-1",
        question="贷款余额",
        authorization=AuthorizationContext(subject="owner-1", username="owner", roles=["analyst"]),
    )
    assert can_access_run(run, principal("owner-1", "analyst"))
    assert not can_access_run(run, principal("other", "analyst"))
    assert can_access_run(run, principal("admin-1", "admin"))


def test_audit_is_visible_only_to_auditor_or_admin() -> None:
    run = QueryRun(
        session_id="s-1",
        question="贷款余额",
        authorization=AuthorizationContext(subject="owner-1", username="owner", roles=["analyst"]),
    )
    assert not can_access_audit(run, principal("owner-1", "analyst"))
    assert not can_access_audit(run, principal("other", "analyst"))
    assert can_access_audit(run, principal("auditor-1", "auditor"))
    assert can_access_audit(run, principal("admin-1", "admin"))
