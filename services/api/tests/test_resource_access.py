from datetime import datetime, timezone

from semora_contracts import AuthorizationContext, QueryRun

from app.auth import Principal
from app.main import can_access_run, can_access_session
from app.store import Session


def principal(subject: str, *roles: str) -> Principal:
    return Principal(subject=subject, username=subject, roles=frozenset(roles), claims={})


def test_external_identity_jwks_can_be_derived_or_explicit(monkeypatch):
    from app.auth import OIDCVerifier

    monkeypatch.setenv("OIDC_ISSUER", "https://identity.example/realm")
    monkeypatch.setenv("OIDC_JWKS_URL", "")
    assert OIDCVerifier()._client.uri == "https://identity.example/realm/protocol/openid-connect/certs"
    monkeypatch.setenv("OIDC_JWKS_URL", "https://identity.example/custom-keys")
    assert OIDCVerifier()._client.uri == "https://identity.example/custom-keys"


def test_missing_identity_config_does_not_break_startup_or_bypass_auth(monkeypatch):
    import pytest
    from fastapi import HTTPException
    from app.auth import OIDCVerifier

    monkeypatch.setenv("OIDC_ISSUER", "")
    monkeypatch.setenv("OIDC_JWKS_URL", "")
    verifier = OIDCVerifier()
    assert verifier._client is None
    with pytest.raises(HTTPException) as caught:
        verifier.verify("Bearer invalid-token")
    assert caught.value.status_code == 503


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


def test_session_list_includes_latest_status_without_changing_creation_order(monkeypatch):
    from starlette.requests import Request
    from app.main import list_sessions, store

    older = Session("older", "older", datetime(2026, 1, 1, tzinfo=timezone.utc))
    newer = Session("newer", "newer", datetime(2026, 1, 2, tzinfo=timezone.utc))
    first = QueryRun(session_id=older.id, question="first", status="failed")
    latest = QueryRun(session_id=older.id, question="latest", status="completed")
    older.run_ids = [first.id, latest.id, "missing-run"]
    monkeypatch.setattr(store, "sessions", {older.id: older, newer.id: newer})
    monkeypatch.setattr(store, "runs", {first.id: first, latest.id: latest})
    rows = list_sessions(Request({"type": "http"}))
    assert [row["id"] for row in rows] == ["newer", "older"]
    assert rows[0]["latest_status"] is None
    assert rows[1]["latest_status"] == "completed"
