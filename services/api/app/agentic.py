"""Compatibility facade and application composition for scoped role operations."""
from __future__ import annotations
from semora_agent.runtime import run_agent
from .knowledge import knowledge_service

from .role_contracts import (AnalysisPayload, BoundedReviewFinding, ReviewPayload, InitialReviewPayload,
    review_submission, CompactDelivery, AnalysisDelivery, require_plan_controls, constrain_source_ids,
    CandidateDiagnostics, PlanPatch, AnalysisSubmission, ReviewSubmission, ANALYST_PROMPT, REVIEWER_PROMPT)

from .agent_tools import AgentTools as _AgentTools


class AgentTools(_AgentTools):
    """Compatibility construction point; all tool behavior lives in agent_tools."""
    def __init__(self, workflow, run, ontology, scope, role='analyst'):
        super().__init__(workflow,run,ontology,scope,role,knowledge_provider=knowledge_service)


async def analyse(workflow, run, question, ontology, scope, feedback=None):
    from .intent_planner import enabled as intent_enabled
    if intent_enabled():
        from .intent_analysis import analyse_intent
        from .role_ports import IntentRolePorts
        ports = IntentRolePorts(AgentTools, AnalysisSubmission, review_plan, run_agent)
        return await analyse_intent(workflow, run, question, ontology, scope, feedback, ports=ports)
    from .legacy_plan_analysis import analyse_legacy
    from .role_ports import AnalysisRolePorts
    return await analyse_legacy(workflow,run,question,ontology,scope,feedback,
        ports=AnalysisRolePorts(AgentTools,AnalysisSubmission,review_plan,run_agent))


def retry_validated_candidate(workflow,run):
    from .legacy_plan_analysis import retry_validated_candidate as retry
    return retry(workflow,run)


from . import review_analysis as _review_analysis
selected_catalog = _review_analysis.selected_catalog


async def review_plan(workflow, run, ontology, question, outcome, pending=None):
    from .role_ports import ReviewRolePorts
    return await _review_analysis.review_plan(workflow,run,ontology,question,outcome,pending,
        ports=ReviewRolePorts(AgentTools,run_agent))


async def review(workflow, run, ontology, phase='pre_execution'):
    from .role_ports import ReviewRolePorts
    return await _review_analysis.review(workflow,run,ontology,phase,
        ports=ReviewRolePorts(AgentTools,run_agent))
