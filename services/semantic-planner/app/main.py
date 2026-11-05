import os
from importlib.metadata import version
from typing import Any

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

from semora_contracts import QueryPlan, SQLArtifact
from semora_telemetry import configure_telemetry

from .wren_adapter import compile_with_wren


app = FastAPI(title="Semora Semantic Planner", version="0.1.0")
configure_telemetry(app, "semora-semantic-planner")


class CompileInput(BaseModel):
    plan: QueryPlan
    ontology: dict[str, Any]


@app.get("/healthz")
def healthz() -> dict:
    return {
        "status": "ok",
        "service": "semora-semantic-planner",
        "engine": "wren-core",
        "wren_core": version("wren-core-py"),
        "database_credentials": False,
    }


@app.post("/internal/v1/compile", response_model=SQLArtifact)
def compile_query(body: CompileInput, x_semora_internal_token: str | None = Header(default=None)) -> SQLArtifact:
    if os.getenv("REQUIRE_INTERNAL_TOKEN", "true").lower() == "true" and x_semora_internal_token != os.getenv("INTERNAL_TOKEN", "semora-dev-internal"):
        raise HTTPException(401, "invalid internal token")
    try:
        return compile_with_wren(body.plan, body.ontology)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc

