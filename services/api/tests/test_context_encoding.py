from copy import deepcopy

import pytest

from app.context_encoding import encode_context,decode_context
from semora_agent.context_budget import prepare_context,ContextBudgetError,encoded_size
import json


def large_context():
    props=[{'name':f'field_{i}','label':f'字段{i}','type':'decimal','description':f'完整定义{i}，单位和例外条件全部保留',
            **({'enumValues':{'A':'只包含有效记录','B':None}} if i%3==0 else {})} for i in range(700)]
    return {'request':'保留确认的条件，金额不舍入', 'confirmed_fact_context':[{'value':'1234567890.1234567890123456789'}],
            'prepared_context':{'objects':[{'object':{'name':'record','properties':props},
                'field_refs':['record.'+p['name'] for p in props]}]}}


def test_large_semantic_packet_is_lossless_and_does_not_mutate_authority():
    original=large_context();before=deepcopy(original)
    packed=encode_context(original)
    assert decode_context(packed)==original and original==before
    assert encoded_size(packed)<encoded_size(original)*.7
    assert packed['confirmed_fact_context']==original['confirmed_fact_context']
    for size in [10,32]:
        small=deepcopy(original);entry=small['prepared_context']['objects'][0]
        entry['object']['properties']=entry['object']['properties'][:size]
        entry['field_refs']=entry['field_refs'][:size]
        assert encode_context(small)==small


def test_budget_counts_tool_contract_and_packed_packet_still_preserves_every_field(monkeypatch):
    context=large_context();packed=encode_context(context)
    tools=[{'description':'typed tool contract '*300}]
    messages=lambda value:[{'role':'user','content':json.dumps(value,ensure_ascii=False)}]
    raw=encoded_size({'messages':messages(context),'tools':tools})
    reduced=encoded_size({'messages':messages(packed),'tools':tools})
    limit=(raw+reduced)//2
    monkeypatch.setenv('AGENT_CONTEXT_MAX_BYTES',str(limit))
    with pytest.raises(ContextBudgetError) as failure:prepare_context(messages(context),tools)
    assert failure.value.stats['tool_schema_bytes']>0
    request,stats=prepare_context(messages(packed),tools)
    assert stats['request_bytes']<limit
    assert decode_context(json.loads(request[0]['content']))==context


def test_scoped_tool_observation_can_use_same_lossless_encoding():
    original=large_context()['prepared_context']
    assert decode_context(encode_context(original))==original


def test_reviewer_drops_planner_scaffolding_but_preserves_actual_semantics_and_artifacts():
    from app.task_handoff import project_context
    context=large_context()
    packet=context['prepared_context']
    packet['source_layout']=[{'field_refs':['record.field_0']*1000}]
    packet['transfer_templates']=[{'how_to_build':'instruction'}]*100
    packet['objects'][0]['stage_group_keys']=['record.field_0']*1000
    packet['objects'][0]['stage_join_contracts']={'how_to_build':'instruction'}
    context['sql']={'physical_sql':'SELECT * FROM records LIMIT 1'}
    context['plan']={'filters':[{'field':'record.field_0','operator':'eq','value':1}]}
    original=deepcopy(context)
    view=project_context(context,'reviewer')
    assert 'source_layout' not in view['prepared_context']
    assert 'stage_group_keys' not in view['prepared_context']['objects'][0]
    assert view['prepared_context']['objects'][0]['object']==packet['objects'][0]['object']
    assert view['plan']==context['plan'] and view['sql']==context['sql']
    assert context==original
