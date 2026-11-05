import asyncio
import copy
import json

import pytest

from app.agent_context import compact_definition, compact_memory
from app.agentic import AgentTools
from app.store import Store
from app.workflow import Workflow
from semora_contracts import QueryRun
from services.api.tests.test_workflow import fake_execute, fake_retrieve, fake_snapshot


@pytest.mark.parametrize('role', ['analyst', 'reviewer'])
def test_ontology_tools_drop_import_provenance_without_dropping_query_semantics(role):
    async def scenario():
        ref, ontology = await fake_snapshot('retail_credit')
        obj = ontology['objects'][0]
        source_text = 'IMPORT_PROVENANCE ' * 10000
        obj['metadata'] = {'sourceEvidence': [source_text], 'description': '正式对象业务定义'}
        prop = obj['properties'][0]
        prop['metadata'] = {'description': '字段业务解释', 'unit': '件', 'enumValues': ['A', 'B'], 'sourceEvidence': [source_text]}
        for relation in ontology['relations']:
            relation['metadata'] = {'sourceEvidence': [source_text], 'description': '业务关联角色'}
        before = copy.deepcopy(ontology)
        w = Workflow(Store(), fake_execute, fake_retrieve, fake_snapshot)
        run = QueryRun(session_id='demo-session', question='查询', ontology=ref)
        tools = {t.name: t for t in AgentTools(w, run, ontology, {}, role).tools()}
        raw = await tools['read_object'].ainvoke({'name': obj['name']})
        result = json.loads(raw)
        assert len(raw.encode()) < len(json.dumps(obj).encode()) / 10
        assert result['object']['description'] == '正式对象业务定义'
        assert result['object']['properties'][0]['description'] == '字段业务解释'
        assert result['object']['properties'][0]['unit'] == '件'
        assert result['object']['properties'][0]['enumValues'] == ['A', 'B']
        assert len(result['field_refs']) == len(obj['properties'])
        assert [r['name'] for r in result['relations']] == [r['name'] for r in ontology['relations'] if obj['name'] in (r['from'], r['to'])]
        assert 'find_relation_path' in result['relation_details']
        relation = ontology['relations'][0]
        path = json.loads(await tools['find_relation_path'].ainvoke({'source': relation['from'], 'target': relation['to']}))
        assert path['path'] and 'IMPORT_PROVENANCE' not in json.dumps(path)
        assert next(r['condition'] for r in path['relations'] if r['name'] == relation['name']) == relation['condition']
        assert ontology == before
    asyncio.run(scenario())


def test_legacy_memory_is_compacted_without_altering_call_ids_or_persisted_history():
    memory = [{'type': 'tool', 'data': {'name': 'read_object', 'tool_call_id': 'call-1', 'content': json.dumps({
        'object': {'name': 'item', 'metadata': {'sourceEvidence': ['RAW_IMPORT']}, 'properties': [{'name': 'amount', 'column': 'amt', 'sensitive': True}]},
        'field_refs': ['item.amount'], 'stage_group_keys': ['item.id']})}},
        {'type': 'human', 'data': {'content': '已确认口径'}}]
    original = copy.deepcopy(memory)
    result = compact_memory(memory)
    assert 'RAW_IMPORT' not in result[0]['data']['content']
    assert result[0]['data']['tool_call_id'] == 'call-1'
    observation = json.loads(result[0]['data']['content'])
    assert observation['field_refs'] == ['item.amount']
    assert observation['stage_group_keys'] == ['item.id']
    assert observation['object']['properties'][0]['sensitive'] is True
    assert result[1] == memory[1] and memory == original


def test_execution_definitions_are_not_truncated():
    item = {'name': 'metric', 'expression': 'x' * 6000, 'defaultFilters': ['x > 0'], 'metadata': {'description': '口径' * 1000}}
    compact = compact_definition(item)
    assert compact['expression'] == item['expression']
    assert compact['defaultFilters'] == item['defaultFilters']
    assert compact['description'] == item['metadata']['description']
