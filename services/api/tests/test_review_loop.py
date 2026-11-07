import asyncio
import json

import pytest

from app import agentic
from app.review_loop import (capabilities, check_revision, issue_ledger,
                             validate_resolution, route_findings)
from semora_agent import AnalystAgent, ReviewerAgent
from semora_contracts import ReviewDecision, SourceQueryPlan
from services.api.tests.test_plan_candidates import ONTOLOGY, PLAN, setup
from services.api.tests.test_agent_runtime import NativeModel


def finding(**kwargs):
    return dict(kind='repair', paths=['/filters'], message='需要过滤范围',
                acceptance='filters包含用户确认的范围', **kwargs)


def decision(**kwargs):
    return ReviewDecision.model_validate({'decision': 'revise', 'rationale': '核对执行条件',
                                          'findings': [finding()], **kwargs})


def test_targeted_diff_ignores_text_and_unrelated_execution_changes():
    before = {'filters': [], 'fields': ['record.id'], 'assumptions': []}
    pending = issue_ledger(decision(), [])
    unrelated = {**before, 'fields': ['record.amount'], 'assumptions': ['已修复']}
    assert check_revision(before, unrelated, pending) == [pending[0]['id']]
    repaired = {**before, 'filters': [{'field': 'record.amount', 'value': 10}]}
    assert check_revision(before, repaired, pending) == []


def test_resolution_cannot_silently_drop_or_approve_open_findings():
    pending = issue_ledger(decision(), [])
    with pytest.raises(ValueError, match='遗漏'):
        validate_resolution(decision(decision='approved', findings=[]), pending)
    report = {'finding_id': pending[0]['id'], 'status': 'unresolved', 'rationale': '过滤仍缺失'}
    with pytest.raises(ValueError, match='不能approved'):
        validate_resolution(decision(decision='approved', findings=[], resolutions=[report]), pending)
    ok = decision(decision='approved', findings=[], resolutions=[{**report, 'status': 'resolved'}])
    validate_resolution(ok, pending)


def test_invalid_paths_and_invented_capability_are_rejected():
    with pytest.raises(ValueError, match='执行字段'):
        validate_resolution(decision(findings=[{**finding(), 'paths': ['/assumptions']}]), [], {'filters': [], 'assumptions': []})
    with pytest.raises(ValueError, match='capability_key'):
        validate_resolution(decision(findings=[{**finding(), 'kind': 'capability', 'capability_key': 'made_up'}]), [])


def test_capabilities_match_current_contract():
    assert 'sum' in capabilities()['aggregate_functions']
    assert {'order_by', 'limit'} <= set(SourceQueryPlan.model_fields)
    assert 'ai_row_analysis' in capabilities()['unsupported']


@pytest.mark.parametrize('kind,key', [('clarification', None), ('capability', 'ai_row_analysis')])
def test_review_routes_to_clarification_without_another_analyst_call(kind, key):
    async def scenario():
        workflow, run, _, _ = setup()
        analyst = NativeModel([('AgentSubmission', {'payload': PLAN})])
        reviewer = NativeModel([('AgentSubmission', {'payload': {
            'decision': 'revise', 'issues': ['需要确认查询范围'], 'rationale': '不能擅自替换用户目标',
            'findings': [{**finding(), 'kind': kind, 'paths': [], 'capability_key': key,
                          'question': '是否只查询原始数据？'}]}})])
        workflow.analyst, workflow.reviewer = AnalystAgent(analyst), ReviewerAgent(reviewer)
        outcome = await agentic.analyse(workflow, run, run.question, ONTOLOGY, {})
        assert outcome.plan is None
        assert outcome.requirement.status == 'awaiting_clarification'
        assert outcome.requirement.clarifications[0].question == '是否只查询原始数据？'
        assert len(analyst.messages) == len(reviewer.messages) == 1
        assert run.sql is None and any(e.event == 'plan.review.routed' for e in run.events)
    asyncio.run(scenario())


def test_repair_reuses_issue_id_and_requires_reviewer_resolution():
    class Reviewer:
        configured = required = True
        def __init__(self):
            self.calls = 0
        async def complete_messages(self, messages, tools, **kwargs):
            self.calls += 1
            ctx = json.loads(next(m['content'] for m in messages if m['role'] == 'user'))
            if self.calls == 1:
                payload = {'decision': 'revise', 'issues': ['阈值应为20'], 'rationale': '执行阈值与确认口径不符',
                           'findings': [{**finding(), 'message': '阈值应为20', 'acceptance': '过滤阈值为20'}]}
            else:
                pending = ctx['pending_findings']
                assert len(pending) == 1 and pending[0]['id'].startswith('issue_')
                assert ctx['plan']['filters'][0]['value'] == 20
                payload = {'decision': 'approved', 'issues': [], 'rationale': '已核实修复',
                           'resolutions': [{'finding_id': pending[0]['id'], 'status': 'resolved', 'rationale': 'filters阈值为20'}]}
            return {'tool_calls': [{'id': str(self.calls), 'type': 'function',
                                   'function': {'name': 'AgentSubmission', 'arguments': json.dumps({'payload': payload})}}]}
    async def scenario():
        workflow, run, _, _ = setup()
        fixed = {**PLAN, 'filters': [{**PLAN['filters'][0], 'value': 20}]}
        analyst = NativeModel([('AgentSubmission', {'payload': PLAN}), ('AgentSubmission', {'payload': fixed})])
        reviewer = Reviewer()
        workflow.analyst, workflow.reviewer = AnalystAgent(analyst), ReviewerAgent(reviewer)
        outcome = await agentic.analyse(workflow, run, run.question, ONTOLOGY, {})
        assert outcome.plan.filters[0].value == 20 and reviewer.calls == 2
        events = [e for e in run.events if e.event == 'plan.preflight.completed']
        assert events[0].data['review']['findings'][0]['id'] == events[1].data['review']['resolutions'][0]['finding_id']
    asyncio.run(scenario())
