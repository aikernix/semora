from __future__ import annotations

import asyncio

import httpx
import pytest
from fastapi import HTTPException

from app import main


class FakeClient:
    def __init__(self, response: httpx.Response, captured: dict) -> None:
        self.response = response
        self.captured = captured

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return None

    async def get(self, url: str, headers: dict[str, str]):
        self.captured.update(url=url, headers=headers)
        return self.response


def test_archive_proxy_encodes_run_id_and_forwards_internal_token(monkeypatch):
    captured: dict = {}
    request = httpx.Request("GET", "http://audit/internal")
    response = httpx.Response(200, request=request, json={"archived": True, "valid": True})
    monkeypatch.setenv("AUDIT_EXPORTER_URL", "http://audit:8004/")
    monkeypatch.setenv("INTERNAL_TOKEN", "internal-secret")
    monkeypatch.setattr(main.httpx, "AsyncClient", lambda **_: FakeClient(response, captured))

    result = asyncio.run(main.audit_archive_request("run/id with spaces", "/verify"))

    assert result == {"archived": True, "valid": True}
    assert captured["url"] == "http://audit:8004/internal/v1/archives/run%2Fid%20with%20spaces/verify"
    assert captured["headers"] == {"X-Semora-Internal-Token": "internal-secret"}


def test_archive_proxy_maps_missing_archive_to_pending(monkeypatch):
    response = httpx.Response(404, request=httpx.Request("GET", "http://audit/internal"))
    monkeypatch.setattr(main.httpx, "AsyncClient", lambda **_: FakeClient(response, {}))

    assert asyncio.run(main.audit_archive_request("run-1", "/verify")) == {
        "run_id": "run-1",
        "archived": False,
        "valid": False,
    }


def test_archive_proxy_fails_closed_when_exporter_is_unavailable(monkeypatch):
    class BrokenClient(FakeClient):
        async def get(self, url: str, headers: dict[str, str]):
            raise httpx.ConnectError("offline")

    monkeypatch.setattr(
        main.httpx,
        "AsyncClient",
        lambda **_: BrokenClient(httpx.Response(500), {}),
    )

    with pytest.raises(HTTPException) as exc:
        asyncio.run(main.audit_archive_request("run-1", "/verify"))
    assert exc.value.status_code == 503
