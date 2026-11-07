from __future__ import annotations

from collections.abc import Callable
from typing import Any, Awaitable, TypedDict

from langgraph.graph import END, START, StateGraph


class AgentState(TypedDict, total=False):
    run_id: str
    approval_mode: str
    settings: dict[str, Any]
    ontology_model: dict[str, Any]
    auto_align: bool
    alignment_initial_draft: dict[str, Any]
    initial_clarification: dict[str, Any]
    needs_revision: bool
    internal_repair: bool
    task_superseded: bool


AsyncNode = Callable[[AgentState], Awaitable[dict[str, Any]]]
Router = Callable[[AgentState], str]


def build_query_graph(
    *,
    retrieve: AsyncNode,
    analyse: AsyncNode,
    align: AsyncNode,
    compile_sql: AsyncNode,
    gate_and_review: AsyncNode,
    approval: AsyncNode,
    execute: AsyncNode,
    explain: AsyncNode,
    revise: AsyncNode | None = None,
    route_after_step: Router,
    checkpointer: Any = None,
):
    """Build the durable query lifecycle; tools remain deterministic node dependencies."""
    graph = StateGraph(AgentState)
    graph.add_node("retrieve", retrieve)
    graph.add_node("analyse", analyse)
    graph.add_node("align", align)
    graph.add_node("compile", compile_sql)
    graph.add_node("gate_and_review", gate_and_review)
    graph.add_node("approval", approval)
    graph.add_node("execute", execute)
    graph.add_node("explain", explain)
    if revise is not None:
        graph.add_node('revise', revise)
    graph.add_edge(START, "retrieve")
    graph.add_conditional_edges("retrieve", route_after_step, {"continue": "analyse", "end": END})
    graph.add_conditional_edges("analyse", route_after_step, {"continue": "align", "end": END})
    graph.add_conditional_edges("align", route_after_step, {"continue": "compile", "end": END})
    if revise is not None:
        def after_compile(state):
            if route_after_step(state)=='end':return 'end'
            return 'revise' if state.get('needs_revision') else 'continue'
        graph.add_conditional_edges('compile',after_compile,{'continue':'gate_and_review','revise':'revise','end':END})
        def after_review(state):
            if route_after_step(state) == 'end':
                return 'end'
            return 'revise' if state.get('needs_revision') else 'continue'
        graph.add_conditional_edges('gate_and_review', after_review, {'continue': 'approval', 'revise': 'revise', 'end': END})
        def after_revision(state):
            if route_after_step(state) == 'end':
                return 'end'
            return 'compile' if state.get('internal_repair') else 'align'
        graph.add_conditional_edges('revise', after_revision, {'compile': 'compile', 'align': 'align', 'end': END})
    else:
        graph.add_conditional_edges('compile',route_after_step,{'continue':'gate_and_review','end':END})
        graph.add_conditional_edges("gate_and_review", route_after_step, {"continue": "approval", "end": END})
    graph.add_conditional_edges("approval", route_after_step, {"continue": "execute", "end": END})
    graph.add_conditional_edges("execute", route_after_step, {"continue": "explain", "end": END})
    graph.add_edge("explain", END)
    return graph.compile(checkpointer=checkpointer)
