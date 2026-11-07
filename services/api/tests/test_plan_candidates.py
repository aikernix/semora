import asyncio
import json

import pytest
from pydantic import ValidationError

from app import agentic
from app.store import Store
from app.workflow import Workflow
from semora_agent import AnalystAgent
from semora_agent.model import PlanValidationError
from semora_contracts import QueryRun
from services.api.tests.test_workflow import fake_execute, fake_retrieve, fake_snapshot


ONTOLOGY = {
    'domain': 'generic', 'dataSourceId': 'source-a', 'knowledgeBaseIds': [],
    'objects': [{'name': 'record', 'table': 'records', 'properties': [
        {'name': 'id', 'column': 'id', 'type': 'integer'},
        {'name': 'amount', 'column': 'amount', 'type': 'number'},
    ]}], 'metrics': [], 'relations': [],
}
PLAN = {'intent': '查询记录', 'fields': ['record.id'], 'grain': 'record',
        'filters': [{'field': 'record.amount', 'operator': 'gt', 'value': 10}]}


def setup():
    workflow = Workflow(Store(), fake_execute, fake_retrieve, fake_snapshot)
    run = QueryRun(session_id='candidate-test', domain='generic', question='查询记录')
    toolkit = agentic.AgentTools(workflow, run, ONTOLOGY, {})
    toolkit.start_phase('首次需求分析')
    tool = next(t for t in toolkit.tools() if t.name == 'validate_plan')
    return workflow, run, toolkit, tool


def test_candidate_preserves_plan_and_rejects_overrides_stale_or_foreign_ids():
    async def scenario():
        _, _, toolkit, tool = setup()
        result = json.loads(await tool.ainvoke({'analysis': PLAN}))
        assert result['valid']
        assert 'plan' not in result and result['plan_summary']['fields'] == PLAN['fields']
        delivery = agentic.AnalysisDelivery(candidate_id=result['candidate_id']).model_dump()
        resolved = toolkit.resolve_submission(delivery)
        assert resolved['filters'] == PLAN['filters']
        resolved['filters'].clear()
        assert toolkit.resolve_submission(delivery)['filters'] == PLAN['filters']
        with pytest.raises(ValueError, match='混用'):
            toolkit.resolve_submission({**delivery, 'fields': ['record.amount']})
        _, _, other, _ = setup()
        with pytest.raises(ValueError, match='失效'):
            other.resolve_submission(delivery)
        toolkit.scope['tenant'] = 'changed'
        with pytest.raises(ValueError, match='失效'):
            toolkit.resolve_submission(delivery)
        toolkit.scope.clear()
        toolkit.start_phase('复核后修订')
        with pytest.raises(ValueError, match='失效'):
            toolkit.resolve_submission(delivery)
    asyncio.run(scenario())


def test_repeated_invalid_plan_stops_and_returns_specific_field_path():
    async def scenario():
        _, run, _, tool = setup()
        invalid = {'intent': '查询记录', 'grain': 'record', 'dimensions': ['record.id']}
        for count in (1, 2):
            result = json.loads(await tool.ainvoke({'analysis': invalid}))
            assert not result['valid'] and result['repeat_count'] == count
            assert 'fields/metrics/calculations' in result['message']
        with pytest.raises(PlanValidationError, match='重复提交3次'):
            await tool.ainvoke({'analysis': invalid})
        assert any(e.event == 'agent.validation.stalled' for e in run.events)
    asyncio.run(scenario())


def test_revision_receipt_rejects_unchanged_execution_before_canonical_delivery():
    async def scenario():
        workflow,run,toolkit,tool=setup()
        toolkit.revision_base=workflow.analyst._build_outcome(PLAN,run.question,[],ONTOLOGY,{},'test').plan
        toolkit.pending_findings=[{'id':'sort','kind':'repair','paths':['/order_by'],'message':'缺少排序','acceptance':'按record.id降序'}]
        rejected=json.loads(await tool.ainvoke({'analysis':PLAN}))
        assert not rejected['valid'] and 'sort' in rejected['message'] and not toolkit.candidates
        fixed={**PLAN,'order_by':[{'field':'record.id','direction':'desc'}],'limit':2}
        approved=json.loads(await tool.ainvoke({'analysis':fixed}))
        assert approved['valid'] and approved['plan_summary']['limit']==2
    asyncio.run(scenario())


def test_schema_exposes_nested_fields_and_rejects_wrong_envelope():
    _, _, _, tool = setup()
    schema = tool.args_schema.model_json_schema()
    assert 'source_steps' in schema['$defs']['AnalysisPayload']['properties']
    with pytest.raises(ValidationError):
        tool.args_schema.model_validate({'analysis': {'plan': PLAN}})


def test_multisource_validation_identifies_failing_step():
    workflow, _, _, _ = setup()
    value = {'source_steps': [{'name': 'lookup', 'label': '查询记录', 'data_source_id': 'source-a',
                              'query': {'domain': 'generic', 'intent': '查询记录', 'metrics': [],
                                        'grain': 'record', 'dimensions': ['record.id']}, 'inputs': []}]}
    with pytest.raises(ValueError, match=r'source_steps\[0\]\(lookup\).query: fields/metrics/calculations'):
        workflow.analyst._build_outcome(value, '查询记录', [], ONTOLOGY, {}, 'llm')


def test_agent_submits_validated_candidate_without_regenerating_plan():
    class Model:
        configured = required = True
        def __init__(self):
            self.calls = 0
        async def complete_messages(self, messages, tools, **kwargs):
            self.calls += 1
            if self.calls == 1:
                name, args = 'validate_plan', {'analysis': PLAN}
            else:
                observation = json.loads(next(m['content'] for m in reversed(messages) if m['role'] == 'tool'))
                name, args = 'AgentSubmission', {'payload': {'candidate_id': observation['candidate_id']}}
            return {'tool_calls': [{'id': str(self.calls), 'type': 'function',
                                   'function': {'name': name, 'arguments': json.dumps(args)}}]}
    async def scenario():
        workflow, run, _, _ = setup()
        model = Model()
        workflow.analyst = AnalystAgent(model)
        outcome = await agentic.analyse(workflow, run, run.question, ONTOLOGY, {})
        assert outcome.plan.fields == PLAN['fields']
        assert outcome.plan.filters[0].value == 10
        assert model.calls == 2
        assert run.sql is None
    asyncio.run(scenario())


def test_core_prompts_have_no_fixed_domain_vocabulary():
    for prompt in (agentic.ANALYST_PROMPT, agentic.REVIEWER_PROMPT):
        assert not any(word in prompt for word in ('银行', '教育', '学员', '老师', '贷款', '体验课'))


def test_candidate_events_report_actual_changes_not_repeated_explanations():
    async def scenario():
        _, run, _, tool = setup()
        await tool.ainvoke({'analysis': PLAN})
        await tool.ainvoke({'analysis': {**PLAN, 'assumptions': ['仅更新解释']}})
        fixed = {**PLAN, 'filters': [{**PLAN['filters'][0], 'value': 20}]}
        await tool.ainvoke({'analysis': fixed})
        outputs = [e.data for e in run.events if e.event == 'plan.candidate.validated']
        assert len(outputs) == 2 and outputs[0]['revision'] is False
        assert outputs[1]['changes'] == [{'path': '/filters/0/value', 'before': 10, 'after': 20}]
    asyncio.run(scenario())


def test_tool_rejects_source_steps_outside_analysis_instead_of_silently_dropping_them():
    async def scenario():
        _, run, toolkit, tool = setup()
        result = json.loads(await tool.ainvoke({'analysis': PLAN, 'source_steps': [{'name': 'must_not_disappear'}]}))
        assert result['valid'] is False
        assert result['errors'][0]['path'] == 'source_steps'
        assert toolkit.candidates == {}
        assert any(e.event == 'agent.validation.failed' for e in run.events)
        schema = tool.args_schema.model_json_schema()
        assert schema['additionalProperties'] is False
    asyncio.run(scenario())
