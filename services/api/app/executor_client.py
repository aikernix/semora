"""The only API-side transport to the isolated business SQL Executor."""

import os
import asyncio
import httpx
from semora_contracts import ExecutionResult
from .datasources import datasource_service


class ExecutorClient:
    def datasource_payload(self, data_source_id: str) -> dict[str, str]:
        try:
            item = datasource_service.repository.get(data_source_id)
        except KeyError as exc:
            raise RuntimeError("本体绑定的数据源未登记") from exc
        if item.status != "healthy" or item.access != "read_only":
            raise RuntimeError("本体绑定的数据源尚未通过只读连接验证")
        return {"datasource_id": item.id, "type": item.type, "secret_ref": item.secret_ref}

    async def execute(
        self,
        sql: str,
        parameters: dict,
        sensitive_columns: list[str],
        settings: dict,
    ) -> ExecutionResult:
        from .executor_errors import request_executor

        url = os.getenv("EXECUTOR_URL", "http://localhost:8002")
        async with httpx.AsyncClient(timeout=35) as client:
            try:
                response = await request_executor(
                    client,
                    f"{url}/internal/v1/query",
                    operation="query",
                    json={
                        "sql": sql,
                        "parameters": parameters,
                        "sensitive_columns": sensitive_columns,
                        "timeout_ms": int(settings["query_timeout_seconds"]) * 1000,
                        "max_rows": int(settings["max_result_rows"]),
                        "max_bytes": int(settings["max_result_mb"]) * 1024 * 1024,
                        "execution_id": settings.get("execution_id"),
                        "datasource": self.datasource_payload(
                            str(settings.get("data_source_id") or "")
                        ),
                    },
                    headers={
                        "X-Semora-Internal-Token": os.getenv("INTERNAL_TOKEN", "semora-dev-internal")
                    },
                )
            except asyncio.CancelledError:
                # wait_for cancels this call at the per-statement deadline.
                # Stop its worker before allowing a retry to start.
                if settings.get('execution_id'):
                    try:
                        cancellation = await client.post(f"{url}/internal/v1/executions/{settings['execution_id']}/cancel",
                            timeout=2, headers={'X-Semora-Internal-Token':os.getenv('INTERNAL_TOKEN','semora-dev-internal')})
                        cancellation.raise_for_status()
                    except httpx.HTTPError:
                        import logging
                        logging.getLogger(__name__).warning('Timed-out SQL worker cancellation did not complete')
                raise

            return ExecutionResult.model_validate(response.json())

    async def estimate(self, sql: str, parameters: dict, data_source_id: str) -> dict:
        from .executor_errors import request_executor, ExecutorOperationError

        url = os.getenv("EXECUTOR_URL", "http://localhost:8002")
        async with httpx.AsyncClient(timeout=10) as client:
            response = await request_executor(
                client,
                f"{url}/internal/v1/estimate",
                operation="estimate",
                json={
                    "sql": sql,
                    "parameters": parameters,
                    "timeout_ms": 5_000,
                    "datasource": self.datasource_payload(data_source_id),
                },
                headers={
                    "X-Semora-Internal-Token": os.getenv("INTERNAL_TOKEN", "semora-dev-internal")
                },
            )
            try:
                value = response.json()
            except ValueError as exc:
                raise ExecutorOperationError() from exc
            if not isinstance(value, dict) or "estimated_rows" not in value:
                raise ExecutorOperationError()
            return value

    @staticmethod
    async def static_estimate(_: str, __: dict, ___: str) -> dict:
        # Injected test executors do not own a database planner.
        return {"estimated_rows": 1_000, "total_cost": 0.0, "source": "test-static"}
