"""Recall organization is isolated from relation approval and external services."""
import asyncio
import json

import pytest

from app.relationship_inputs import build_inputs
from app.relationship_keywords import KeywordDiscovery
from app.relationship_contracts import OrganizationOutput
from app.relationship_organization import validate_organization


def source(n=4):
    return build_inputs({'source':{str(i):{'id':str(i),'name':f'entry_{i}',
        'comment':'业务对象记录','columns':[
            {'name':'id','type':'bigint','comment':'记录主键'},
            {'name':'object_ref','type':'bigint','comment':'对象编号'},
            {'name':'unmatched_code','type':'varchar(50)','comment':'其他用途'}]}
        for i in range(n)}})


def prepared(cards,model,state=None):
    engine=KeywordDiscovery(cards,model,state if state is not None else {},lambda:None)
    engine.work.claim('keywords','batch')
    engine.work.finish('keywords','batch',{'keywords':['object_ref']},['object_ref'])
    engine.finish_stage('keywords',{'keyword_count':1})
    asyncio.run(engine.retrieve_all())
    return engine


def retained(payload):
    return {'groups':[{'input_group_ids':[g['group_id']],'table_ids':g['table_ids'],
        'reason':'摘要未表明不同编号体系，保留完整组'} for g in payload['groups']], 'excluded':[]}


def test_split_groups_drive_actual_comparisons_and_resume_without_model_calls():
    class Model:
        calls=[]
        async def complete_json(self,system,user):
            p=json.loads(user);self.calls.append(p)
            if p['phase']=='organization':
                g=p['groups'][0]
                assert all([f['name'] for f in t['matched_fields']]==['object_ref'] for t in p['tables'])
                assert all('columns' not in t for t in p['tables'])
                return {'groups':[{'input_group_ids':[g['group_id']], 'table_ids':ids,
                    'reason':'字段注释明确为不同编号体系'} for ids in [g['table_ids'][:2],g['table_ids'][2:]]], 'excluded':[]}
            assert p['phase']=='compare'
            return {'relations':[]}
    model=Model();state={};engine=prepared(source(),model,state)
    asyncio.run(engine.organize())
    assert asyncio.run(engine.discover())==[]
    compared=[pair for p in model.calls if p['phase']=='compare' for pair in p['comparison_pairs']]
    assert len([pair for pair in compared if pair[0]!=pair[1]])==2
    assert engine.stage_result('organization')['metrics']['output_groups']==2
    before=len(model.calls)
    restored=KeywordDiscovery(engine.cards,model,state,lambda:None)
    assert asyncio.run(restored.discover())==[]
    assert len(model.calls)==before


def test_exclusion_is_per_membership_and_audited_without_changing_business_storage():
    class Model:
        calls=0
        async def complete_json(self,system,user):
            self.calls+=1;p=json.loads(user);g=p['groups'][0]
            return {'groups':[{'input_group_ids':[g['group_id']], 'table_ids':g['table_ids'][:2],
                               'reason':'两表对象编号含义相同'}],
                    'excluded':[{'input_group_id':g['group_id'],'table_id':g['table_ids'][2],
                                 'kind':'substring_noise','reason':'仅为其他单词内部的文字命中'}]}
    engine=prepared(source(3),Model())
    output=asyncio.run(engine.organize())
    assert [len(ids) for ids in output]==[2]
    saved=engine.stage_result('organization')
    assert len(saved['excluded'])==1 and saved['excluded'][0]['reason']
    assert engine.work.keywords()==['object_ref']
    assert 'discovery_complete' not in engine.scope
    assert asyncio.run(engine.organize())==output
    assert engine.model.calls==1


def test_small_groups_can_merge_and_reuse_cached_organization_after_keyword_expansion():
    class Model:
        calls=0
        async def complete_json(self,system,user):
            self.calls+=1;p=json.loads(user)
            return {'groups':[{'input_group_ids':[g['group_id'] for g in p['groups']],
                'table_ids':sorted({t for g in p['groups'] for t in g['table_ids']}),
                'reason':'两个召回组描述同一编号体系'}], 'excluded':[]}
    cards=source(3);ids=tuple(sorted(cards));model=Model()
    engine=prepared(cards,model)
    groups={ids[:2]:['object_ref'],ids[1:]:['对象']}
    result=asyncio.run(engine.organize_groups(groups))
    assert result=={ids:['object_ref','对象']}
    assert asyncio.run(engine.organize_groups(groups))==result and model.calls==1
    # Existing merged records remain readable if another input group gains a new keyword.
    updated={ids[:2]:['object_ref','业务对象'],ids[1:]:['对象']}
    assert ids in asyncio.run(engine.organize_groups(updated))


@pytest.mark.parametrize('failure',['missing','unknown','conflict','unknown_source'])
def test_invalid_model_organization_retries_three_times_without_completing_stage(failure):
    class Model:
        calls=0
        async def complete_json(self,system,user):
            self.calls+=1;p=json.loads(user);r=retained(p);g=p['groups'][0]
            if failure=='missing':r['groups'][0]['table_ids'].pop()
            if failure=='unknown':r['groups'][0]['table_ids'].append('missing-table')
            if failure=='unknown_source':r['groups'][0]['input_group_ids']=['missing-group']
            if failure=='conflict':r['excluded']=[{'input_group_id':g['group_id'],
                'table_id':g['table_ids'][0],'kind':'unrelated_meaning','reason':'矛盾输出'}]
            return r
    model=Model();engine=prepared(source(3),model)
    with pytest.raises(ValueError):asyncio.run(engine.organize())
    assert model.calls==3
    assert engine.stage_result('organization') is None
    assert not any(k.startswith('organized_group:') for k in engine.work.local)


def test_oversized_summary_retains_whole_group_without_losing_cross_page_pairs(monkeypatch):
    class Forbidden:
        async def complete_json(self,*args):raise AssertionError('oversized request')
    engine=prepared(source(4),Forbidden())
    monkeypatch.setattr('app.relationship_organization.REQUEST_LIMIT',100)
    result=asyncio.run(engine.organize())
    assert list(result)==[tuple(sorted(engine.cards))]
    assert engine.scope['organization_metrics']['deferred_groups']==1
    assert engine.scope['organization_metrics']['excluded_memberships']==0


def test_output_contract_rejects_empty_groups_and_incomplete_exclusions():
    with pytest.raises(ValueError):
        OrganizationOutput.model_validate({'groups':[{'input_group_ids':['g'], 'table_ids':['t'], 'reason':'x'}], 'excluded':[]})
    payload={'groups':[{'group_id':'g','table_ids':['a','b']}]}
    validate_organization({'groups':[], 'excluded':[
        {'input_group_id':'g','table_id':t,'kind':'substring_noise','reason':'文字误命中'} for t in ['a','b']]},payload)


def test_fifty_four_table_summary_is_read_in_one_bounded_request():
    from app.relationship_context import REQUEST_LIMIT, request_size
    class Model:
        calls=0
        async def complete_json(self,system,user):
            self.calls+=1;p=json.loads(user)
            assert len(p['tables'])==54
            assert request_size(system,p)<=REQUEST_LIMIT
            assert all('matched_fields' in t and 'columns' not in t for t in p['tables'])
            return retained(p)
    model=Model();engine=prepared(source(54),model)
    result=asyncio.run(engine.organize())
    assert len(result)==1 and len(next(iter(result)))==54 and model.calls==1
