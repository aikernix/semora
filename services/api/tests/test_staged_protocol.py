import asyncio
import json

from langchain_core.utils.function_calling import convert_to_openai_tool
from app.agentic import AnalysisSubmission
from services.api.tests.test_plan_candidates import setup, PLAN
from semora_agent.context_budget import encoded_size

PLAN={**PLAN,'order_by':[],'limit':None}


def test_stage_transitions_and_schema_budget(monkeypatch):
    monkeypatch.setenv('AGENT_STAGED_PROTOCOL', 'true')
    async def scenario():
        _, run, kit, _ = setup()
        functions = {t.name: t for t in kit.tools()}
        tools = [convert_to_openai_tool(t) for t in functions.values()] + [convert_to_openai_tool(AnalysisSubmission)]
        names = lambda ts: {t['function']['name'] for t in ts}
        explore = kit.tool_policy(tools)
        assert 'read_object' in names(explore) and 'validate_plan' not in names(explore)
        assert encoded_size(explore) < encoded_size(tools)
        await functions['enter_planning'].ainvoke({})
        planning = kit.tool_policy(tools)
        assert names(planning) == {'validate_plan', 'resume_exploration', 'AgentSubmission'}
        contract=next(t for t in planning if t['function']['name']=='validate_plan')['function']['parameters']['properties']['analysis']
        assert {'order_by','limit'} <= set(contract['required'])
        def check_nested(value):
            if isinstance(value,dict):
                if {'order_by','limit'} <= value.get('properties',{}).keys():
                    assert {'order_by','limit'} <= set(value['required'])
                for child in value.values():check_nested(child)
            elif isinstance(value,list):
                for child in value:check_nested(child)
        check_nested(contract)
        invalid = json.loads(await functions['validate_plan'].ainvoke({'analysis': {'fields': []}}))
        assert not invalid['valid'] and kit.protocol_stage == 'planning'
        await functions['resume_exploration'].ainvoke({'reason': '核对字段'})
        assert kit.protocol_stage == 'exploring'
        await functions['enter_planning'].ainvoke({})
        result = json.loads(await functions['validate_plan'].ainvoke({'analysis': PLAN}))
        delivery = kit.tool_policy(tools)
        assert names(delivery) == {'AgentSubmission', 'resume_exploration'}
        assert encoded_size(delivery) < encoded_size(planning)
        assert kit.resolve_submission({'candidate_id': result['candidate_id'], 'summary': '仅交付说明'})['filters'] == PLAN['filters']
        import pytest
        with pytest.raises(ValueError, match='混用'):
            kit.resolve_submission({'candidate_id': result['candidate_id'], 'summary': '展示说明', 'filters': []})
        original = kit.candidate_binding()
        kit.ontology = {**kit.ontology, 'version': 'changed'}
        assert original != kit.candidate_binding()
        try:
            kit.resolve_submission({'candidate_id': result['candidate_id']})
        except ValueError:
            pass
        else:
            raise AssertionError('stale candidate accepted')
        print(json.dumps({'baseline_schema_bytes': encoded_size([t for t in tools if t['function']['name'] not in {'enter_planning', 'resume_exploration'}]), 'explore_schema_bytes': encoded_size(explore), 'planning_schema_bytes': encoded_size(planning), 'delivery_schema_bytes': encoded_size(delivery)}))
    asyncio.run(scenario())


def test_native_agent_uses_staged_tools_and_delivers_canonical_candidate(monkeypatch):
    from app import agentic
    from semora_agent import AnalystAgent
    from services.api.tests.test_plan_candidates import ONTOLOGY
    monkeypatch.setenv('AGENT_STAGED_PROTOCOL', 'true')
    class Model:
        configured = required = True
        def __init__(self): self.calls = 0
        async def complete_messages(self, messages, tools, **kwargs):
            self.calls += 1
            names = {t['function']['name'] for t in tools}
            if self.calls == 1:
                assert 'validate_plan' not in names
                name, args = 'enter_planning', {}
            elif self.calls == 2:
                assert 'read_object' not in names and 'validate_plan' in names
                name, args = 'validate_plan', {'analysis': PLAN}
            else:
                assert names == {'AgentSubmission', 'resume_exploration'}
                observation = json.loads(next(m['content'] for m in reversed(messages) if m['role'] == 'tool'))
                name, args = 'AgentSubmission', {'payload': {'candidate_id': observation['candidate_id']}}
            return {'tool_calls': [{'id': str(self.calls), 'type': 'function', 'function': {'name': name, 'arguments': json.dumps(args)}}]}
    async def scenario():
        workflow, run, _, _ = setup()
        model = Model()
        workflow.analyst = AnalystAgent(model)
        from semora_agent import ReviewerAgent
        from services.api.tests.test_agent_runtime import NativeModel
        reviewer = NativeModel([('AgentSubmission', {'payload': {'decision': 'approved', 'issues': [], 'rationale': '独立复核通过'}})])
        workflow.reviewer = ReviewerAgent(reviewer)
        revalidations = []
        original_build = workflow.analyst._build_outcome
        def build(*a, **kw):
            revalidations.append(True)
            return original_build(*a, **kw)
        workflow.analyst._build_outcome = build
        outcome = await agentic.analyse(workflow, run, run.question, ONTOLOGY, {})
        assert outcome.plan.filters[0].value == 10
        assert model.calls == 2 and run.sql is None
        assert len(reviewer.messages) == 1 and len(revalidations) >= 3
        assert any(e.event == 'agent.delivery.canonical' for e in run.events)
    asyncio.run(scenario())


def test_canonical_delivery_needs_current_success_receipt_and_binding(monkeypatch):
    from langchain_core.messages import ToolMessage
    from semora_contracts import AuthorizationContext
    monkeypatch.setenv('AGENT_STAGED_PROTOCOL', 'true')
    async def scenario():
        _, run, kit, tool = setup()
        result = json.loads(await tool.ainvoke({'analysis': PLAN}))
        receipt = ToolMessage(content=json.dumps(result), name='validate_plan', tool_call_id='v')
        assert kit.canonical_delivery([receipt]) == {'candidate_id': result['candidate_id']}
        failed = ToolMessage(content=json.dumps({'valid': False, 'candidate_id': result['candidate_id']}), name='validate_plan', tool_call_id='f')
        assert kit.canonical_delivery([failed]) is None
        auth = run.authorization
        run.authorization = AuthorizationContext(subject='other', username='other')
        assert kit.canonical_delivery([receipt]) is None
        run.authorization = auth
        kit.scope['tenant'] = 'other'
        assert kit.canonical_delivery([receipt]) is None
        kit.scope.clear()
        original = kit.ontology
        kit.ontology = {**original, 'version': 'new'}
        assert kit.canonical_delivery([receipt]) is None
        kit.ontology = original
        kit.start_phase('复核修订')
        assert kit.canonical_delivery([receipt]) is None
        assert kit.canonical_delivery([]) is None
    asyncio.run(scenario())
