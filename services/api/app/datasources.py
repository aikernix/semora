from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import os
from typing import Any
from uuid import uuid4

import httpx
import psycopg


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class DataSourceRecord:
    id: str
    name: str
    type: str
    secret_ref: str
    status: str = "unverified"
    access: str = "read_only"
    objects: int = 0
    last_scan_at: datetime | None = None
    scan_result: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=utc_now)

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "type": self.type,
            "secret_ref": self.secret_ref,
            "status": self.status,
            "access": self.access,
            "objects": self.objects,
            "last_scan_at": self.last_scan_at,
            "created_at": self.created_at,
        }


class DataSourceRepository:
    def __init__(self, dsn: str | None = None) -> None:
        self._dsn = dsn if dsn is not None else os.getenv("METADATA_DATABASE_URL")
        self._items: dict[str, DataSourceRecord] = {}
        if self._dsn:
            self._load()
        if not self._dsn or os.getenv("SEMORA_DEMO_SEED", "false").lower() == "true":
            self._ensure_demo_source()

    def _connect(self):
        return psycopg.connect(self._dsn, autocommit=True)

    def _load(self) -> None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """SELECT id,name,type,secret_ref,status,access,objects,last_scan_at,scan_result,created_at
                FROM semora_meta.datasources ORDER BY created_at"""
            )
            for row in cursor.fetchall():
                self._items[row[0]] = DataSourceRecord(
                    id=row[0], name=row[1], type=row[2], secret_ref=row[3], status=row[4],
                    access=row[5], objects=row[6], last_scan_at=row[7],
                    scan_result=row[8] or {}, created_at=row[9],
                )

    def _ensure_demo_source(self) -> None:
        if "retail-credit-pg" not in self._items:
            self.save(DataSourceRecord(
                id="retail-credit-pg",
                name="零售信贷演示库",
                type="postgresql",
                secret_ref="DATASOURCE_RETAIL_CREDIT_PG_URL",
            ))

    def save(self, item: DataSourceRecord) -> DataSourceRecord:
        self._items[item.id] = item
        if self._dsn:
            with self._connect() as connection, connection.cursor() as cursor:
                cursor.execute(
                    """INSERT INTO semora_meta.datasources(
                        id,name,type,secret_ref,status,access,objects,last_scan_at,scan_result,created_at
                    ) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s)
                    ON CONFLICT(id) DO UPDATE SET
                        name=excluded.name,type=excluded.type,secret_ref=excluded.secret_ref,
                        status=excluded.status,access=excluded.access,objects=excluded.objects,
                        last_scan_at=excluded.last_scan_at,scan_result=excluded.scan_result""",
                    (item.id, item.name, item.type, item.secret_ref, item.status, item.access,
                     item.objects, item.last_scan_at,
                     json.dumps(item.scan_result, ensure_ascii=False), item.created_at),
                )
        return item

    def create(self, name: str, datasource_type: str, secret_ref: str) -> DataSourceRecord:
        return self.save(DataSourceRecord(
            id=str(uuid4()), name=name, type=datasource_type, secret_ref=secret_ref,
        ))

    def list(self) -> list[DataSourceRecord]:
        return sorted(self._items.values(), key=lambda item: item.created_at)

    def get(self, datasource_id: str) -> DataSourceRecord:
        try:
            return self._items[datasource_id]
        except KeyError as exc:
            raise KeyError("data source not found") from exc

    def record_test(self, datasource_id: str, result: dict[str, Any]) -> DataSourceRecord:
        item = self.get(datasource_id)
        item.status = "healthy" if result.get("read_only") else "unsafe"
        item.access = "read_only" if result.get("read_only") else "write_capable"
        return self.save(item)

    def record_scan(self, datasource_id: str, result: dict[str, Any]) -> DataSourceRecord:
        item = self.get(datasource_id)
        item.status = "healthy"
        item.access = "read_only"
        item.objects = len(result.get("tables", []))
        item.last_scan_at = utc_now()
        item.scan_result = result
        return self.save(item)


class ExecutorDataSourceClient:
    def __init__(self, base_url: str | None = None, token: str | None = None) -> None:
        self.base_url = (base_url or os.getenv("EXECUTOR_URL", "http://localhost:8002")).rstrip("/")
        self.token = token or os.getenv("INTERNAL_TOKEN", "semora-dev-internal")

    async def request(self, endpoint: str, item: DataSourceRecord, tables: list[str] | None = None) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(timeout=35) as client:
                response = await client.post(
                    f"{self.base_url}/internal/v1/datasources/{endpoint}",
                    headers={"X-Semora-Internal-Token": self.token},
                    json={"datasource_id": item.id, "type": item.type, "secret_ref": item.secret_ref, **({"tables": tables} if tables is not None else {})},
                )
                response.raise_for_status()
                return response.json()
        except httpx.HTTPStatusError as exc:
            raise RuntimeError(
                f"Executor 数据源操作失败（HTTP {exc.response.status_code}）：{exc.response.text[:500]}"
            ) from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise RuntimeError(f"Executor 数据源请求失败：{type(exc).__name__}") from exc


class DataSourceService:
    def __init__(
        self,
        repository: DataSourceRepository | None = None,
        executor: ExecutorDataSourceClient | None = None,
    ) -> None:
        self.repository = repository or DataSourceRepository()
        self.executor = executor or ExecutorDataSourceClient()

    async def test(self, datasource_id: str) -> dict[str, Any]:
        item = self.repository.get(datasource_id)
        result = await self.executor.request("test", item)
        self.repository.record_test(datasource_id, result)
        return result

    async def scan(self, datasource_id: str, tables: list[str] | None = None) -> dict[str, Any]:
        item = self.repository.get(datasource_id)
        result = await self.executor.request("scan", item, tables=tables) if tables is not None else await self.executor.request("scan", item)
        self.repository.record_scan(datasource_id, result)
        return result


datasource_service = DataSourceService()
