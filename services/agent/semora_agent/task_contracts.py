"""Versioned role handoff contracts and immutable authority bindings. No IO."""
from __future__ import annotations
from dataclasses import asdict, dataclass
import hashlib
import json
from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field


def content_hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False,default=str).encode()).hexdigest()


@dataclass(frozen=True)
class ArtifactBinding:
    run_id: str
    session_id: str
    domain: str
    phase: int | None
    request_hash: str
    ontology_hash: str
    scope_hash: str
    authorization_hash: str
    requirement_hash: str
    facts_hash: str

    @classmethod
    def capture(cls, *, run_id, session_id, domain, phase, question, ontology, scope,
                authorization, requirement, facts):
        return cls(run_id,session_id,domain,phase,content_hash(question),content_hash(ontology),
                   content_hash(scope),content_hash(authorization),content_hash(requirement),content_hash(facts))

    @property
    def fingerprint(self):
        return content_hash(asdict(self))


class TaskBindings(BaseModel):
    model_config=ConfigDict(extra='forbid',frozen=True)
    domain: str
    ontology: dict | None
    authorization_hash: str


class TaskContract(BaseModel):
    """Wire-compatible contract; constraints are validated before a role runs."""
    model_config=ConfigDict(extra='forbid',frozen=True)
    task_id: str
    role: Literal['analyst','reviewer']
    operation: Literal['interpret_and_plan','plan_preflight','execution_review','result_review']
    objective: str
    input_revision: str
    run_user_message: str
    current_request_ref: Literal['request','user_request']
    bindings: TaskBindings
    must_preserve_fact_refs: list[str]
    context_hash: str
    acceptance: list[str]
    missing_context_action: str
    memory_policy: str
    max_request_bytes: int=Field(gt=0)
    effective_authorization: dict[str,Any]


@dataclass(frozen=True)
class TaskLease:
    """Single-process stale-result guard; not a distributed CAS lease."""
    input_revision: str
    initial_terminal: bool

    def matches(self, revision: str, terminal: bool) -> bool:
        return self.input_revision==revision and self.initial_terminal==terminal
