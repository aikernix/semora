"""Application composition ports: role orchestration never imports its entrypoint."""
from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Protocol
from pydantic import BaseModel
from semora_contracts import QueryRun, ReviewDecision
from semora_agent.roles import AnalysisOutcome


class AnalystSession(Protocol):
    context_state: dict | None
    task_guard: Callable[[], None]
    task_id: str
    progress_state: dict

    def start_phase(self, label: str) -> None: ...
    def tools(self) -> list: ...
    def event(self, name: str, data: dict) -> None: ...
    def resolve_submission(self, value: dict) -> dict: ...
    def tool_policy(self, tools: list) -> list: ...
    def canonical_delivery(self, messages: list) -> dict | None: ...


@dataclass(frozen=True)
class AnalysisRolePorts:
    create_tools: Callable[[Any, QueryRun, dict, dict], AnalystSession]
    submission: type[BaseModel]
    review: Callable[[Any, QueryRun, dict, str, AnalysisOutcome, list[dict]], Awaitable[ReviewDecision]]
    runner: Callable[..., Awaitable[tuple[dict,list]]]


IntentRolePorts = AnalysisRolePorts  # Existing composition code remains compatible.


@dataclass(frozen=True)
class ReviewRolePorts:
    create_tools: Callable[..., AnalystSession]
    runner: Callable[..., Awaitable[tuple[dict,list]]]
