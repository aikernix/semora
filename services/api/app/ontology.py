from __future__ import annotations

import os
from typing import Any

import httpx
from semora_contracts import OntologySnapshotRef


class OntologyCoreError(RuntimeError):
    def __init__(self, message: str, status_code: int = 503) -> None:
        super().__init__(message)
        self.status_code = status_code


class OntologyCoreClient:
    def __init__(self, base_url: str | None = None, internal_token: str | None = None) -> None:
        self.base_url = (base_url or os.getenv("ONTOLOGY_URL", "http://localhost:8081")).rstrip("/")
        self.internal_token = internal_token or os.getenv("INTERNAL_TOKEN", "semora-dev-internal")

    async def _request(self, method: str, endpoint: str, **kwargs: Any) -> Any:
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                response = await client.request(
                    method,
                    f"{self.base_url}/{endpoint.lstrip('/')}",
                    headers={"X-Semora-Internal-Token": self.internal_token},
                    **kwargs,
                )
                response.raise_for_status()
                return response.json()
        except httpx.HTTPStatusError as exc:
            status_code = exc.response.status_code if 400 <= exc.response.status_code < 500 else 503
            detail = exc.response.text[:500]
            try:
                detail = str(exc.response.json().get("error", detail))
            except ValueError:
                pass
            raise OntologyCoreError(detail, status_code) from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise OntologyCoreError(f"Ontology Core 请求失败：{type(exc).__name__}") from exc

    async def list_drafts(self) -> list[dict[str, Any]]:
        return await self._request("GET", "/api/v1/ontology/drafts")

    async def get_draft(self, domain: str) -> dict[str, Any]:
        return self._normalize_collections(await self._request("GET", f"/api/v1/ontology/drafts/{domain}"))

    @staticmethod
    def _normalize_collections(value: dict[str, Any]) -> dict[str, Any]:
        # Go encodes nil slices as null. Consumers use collection semantics even
        # when a domain has no formal metrics, relations or knowledge bindings.
        ontology = dict(value)
        for key in ('objects', 'relations', 'metrics', 'knowledgeBaseIds', 'defaultFilters'):
            items = ontology.get(key)
            if items is not None and not isinstance(items, list):
                raise OntologyCoreError(f'本体字段 {key} 必须是数组')
            ontology[key] = items if items is not None else []
        return ontology

    async def save_draft(self, domain: str, draft: dict[str, Any]) -> dict[str, Any]:
        return await self._request("PUT", f"/api/v1/ontology/drafts/{domain}", json=draft)

    async def publish(self, domain: str) -> dict[str, Any]:
        return await self._request("POST", "/api/v1/ontology/publish", json={"domain": domain})

    async def latest_snapshot(self, domain: str = "retail_credit") -> OntologySnapshotRef:
        reference, _ = await self.latest_snapshot_bundle(domain)
        return reference

    async def latest_snapshot_bundle(self, domain: str = "retail_credit") -> tuple[OntologySnapshotRef, dict[str, Any]]:
        value = await self._request("GET", "/api/v1/ontology/snapshots/latest", params={"domain": domain})
        ontology = self._normalize_collections(value["ontology"])
        ontology["_wrenArtifact"] = value.get("wrenArtifact")
        ontology["_wrenArtifactVersion"] = value.get("artifactVersion")
        reference = OntologySnapshotRef(
            domain=value["domain"],
            version=value["version"],
            content_hash=value["contentHash"],
            artifact_version=value["artifactVersion"],
            data_source_id=str(ontology.get("dataSourceId") or ""),
            knowledge_base_ids=[str(item) for item in ontology.get("knowledgeBaseIds", [])],
        )
        return reference, ontology

    async def find_path(self, domain: str, source: str, target: str) -> dict[str, Any]:
        return await self._request(
            "GET", "/api/v1/ontology/paths", params={"domain": domain, "from": source, "to": target}
        )

    async def list_candidates(self, domain: str = "") -> list[dict[str, Any]]:
        return await self._request("GET", "/api/v1/ontology/candidates", params={"domain": domain})

    async def create_candidate(self, value: dict[str, Any]) -> dict[str, Any]:
        return await self._request("POST", "/api/v1/ontology/candidates", json=value)

    async def decide_candidate(
        self, candidate_id: str, decision: str, reviewer: str = "", note: str = ""
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            f"/api/v1/ontology/candidates/{candidate_id}/decision",
            json={"decision": decision, "reviewer": reviewer, "note": note},
        )


ontology_service = OntologyCoreClient()
