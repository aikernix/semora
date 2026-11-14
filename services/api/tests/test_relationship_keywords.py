"""New pipeline acceptance cases. Real providers and databases are never called here."""
import asyncio
import json
from unittest.mock import Mock

import pytest
from pydantic import ValidationError

from app.relationship_contracts import KeywordOutput
from app.relationship_inputs import build_inputs, Checkpoints, digest
from app.relationship_keywords import KeywordDiscovery, KEYWORD_SYSTEM
from app.relationship_discovery import DiscoveryIncomplete
from app.relationship_research import decode_decision
from app.relationship_work import RelationshipWork, keyword_set


def cards(n):
    return build_inputs({'source':{f's.t{i}':{'id':f's.t{i}','name':f'record_{i}',
        'comment':'对象登记与业务记录','columns':[{'name':'entity_ref','type':'bigint','comment':'对象编号'},
        {'name':'id','type':'bigint','comment':'主键'}], 'primary_keys':[{'columns':['id']}]} for i in range(n)}})


class Keywords:
    def __init__(self): self.calls=[]
    async def complete_json(self,system,user):
        p=json.loads(user); self.calls.append(p)
        assert p['phase']=='keywords'
        return {'keywords':['entity_ref',' ENTITY_REF ','对象']}


def test_first_stage_is_keyword_only_and_batches_commit_before_completion(tmp_path):
    source=cards(41); model=Keywords(); cp=Checkpoints(tmp_path)
    state={'job_id':'job'}; saved=[]
    def save():
        cp.save('job',state); saved.append(json.loads(cp.path('job').read_text()))
    engine=KeywordDiscovery(source,model,state,save)
    result=asyncio.run(engine.extract_keywords())
    assert result==['entity_ref','对象']
    assert len(model.calls)==3 and max(len(p['tables']) for p in model.calls)<=20
    assert all('source_table_id' not in json.dumps(p) for p in model.calls)
    assert 'retrieval_complete' not in engine.scope
    assert any(s.get('keyword_work') for s in saved)
    restored=KeywordDiscovery(source,model,cp.load('job',None),lambda:None)
    # Checkpoints use fingerprints; this standalone fixture intentionally uses no fingerprint.
    assert asyncio.run(restored.extract_keywords())==result
    assert len(model.calls)==3


@pytest.mark.parametrize('word',['id',' ID ','name','status','VARCHAR','字段'])
def test_generic_search_terms_are_rejected_without_rejecting_business_abbreviations(word):
    with pytest.raises(ValueError,match='non_business_search_keyword'): keyword_set([word])
    assert keyword_set(['AB','entity_ref','Entity_Ref'])==['AB','entity_ref']


def test_program_preserves_model_selected_identifier_terms_and_only_normalizes_case_and_spaces():
    assert keyword_set(['candidate_id','CANDIDATE','Candidate_ID','group_im_id'])==['candidate_id','CANDIDATE','group_im_id']
    assert keyword_set(['UID','User ID','user_id','U_ID'])==['UID','User ID','user_id','U_ID']
    assert keyword_set(['entity_id_id','  entity_id_id  '])==['entity_id_id']


def test_chinese_semantic_terms_remain_valid_without_programmatic_character_splitting():
    words=['业务对象','业务对象编号','业务对象','用户IM标识']
    assert keyword_set(words)==['业务对象','业务对象编号','用户IM标识']
    assert '【中文语义提取】' in KEYWORD_SYSTEM


def test_model_selected_object_term_retrieves_target_without_programmatic_stemming():
    rows={'s.object':{'id':'s.object','name':'object','columns':[{'name':'id','type':'bigint'}]},
          's.events':{'id':'s.events','name':'events','columns':[{'name':'object_id','type':'bigint'}]}}
    cards=build_inputs({'source':rows});before=json.loads(json.dumps(cards))
    engine=KeywordDiscovery(cards,None,{},lambda:None)
    assert engine.retrieve('OBJECT_ID')['total']==1
    assert engine.retrieve('object')['total']==2
    assert cards==before


def test_model_selected_chinese_object_term_is_saved_and_retrieves_name_independent_target():
    rows={'s.registry':{'id':'s.registry','name':'registry','comment':'业务对象登记',
                       'columns':[{'name':'id','type':'bigint','comment':'主键'}]},
          's.events':{'id':'s.events','name':'events',
                      'columns':[{'name':'owner_ref','type':'varchar(50)','comment':'所属业务对象编号'}]}}
    class Model:
        calls=0
        async def complete_json(self,system,user):
            self.calls+=1
            return {'keywords':['业务对象']}
    model=Model();engine=KeywordDiscovery(build_inputs({'source':rows}),model,{},lambda:None)
    assert asyncio.run(engine.extract_keywords())==['业务对象']
    assert engine.work.keywords()==['业务对象']
    assert engine.retrieve('业务对象')['total']==2
    assert model.calls==1


def test_keyword_wire_contract_has_no_source_or_explanation():
    assert KeywordOutput.model_validate({'keywords':[]}).keywords==[]
    with pytest.raises(ValidationError): KeywordOutput.model_validate({'keywords':['entity_ref'],'source':'table'})
    assert '禁止输出' in KEYWORD_SYSTEM


def test_input_includes_nonunique_indexes_without_storage_names():
    from app.relationship_keywords import wire_card
    from app.relationship_normalization import unique_key
    table={'id':'s.events','name':'events','columns':[
        {'name':'entity_ref','type':'varchar(40)','comment':'所属对象编号'},
        {'name':'created_at','type':'timestamp','comment':'创建时间'}],
        'indexes':[{'name':'idx_storage_a','columns':['entity_ref','created_at'],'unique':False},
                   {'name':'idx_storage_b','columns':['entity_ref','created_at'],'unique':False}]}
    card=next(iter(build_inputs({'source':{'s.events':table}}).values()))
    assert wire_card(card)['indexes']==[{'columns':['entity_ref','created_at'],'unique':False}]
    assert 'idx_storage' not in json.dumps(wire_card(card))
    assert not unique_key(card,['entity_ref'])
    assert [f['name'] for f in card['columns']]==['entity_ref']


def test_only_resolvable_complete_foreign_key_mapping_is_marked_automatic():
    target={'id':'s.registry','name':'registry','columns':[{'name':'part_a','type':'bigint'},
        {'name':'part_b','type':'bigint'}]}
    source={'id':'s.events','name':'events','columns':[{'name':n,'type':'varchar(40)'}
        for n in ('entity_a','entity_b','owner_ref')],
        'foreign_keys':[{'columns':['entity_a','entity_b'],'target_table':'s.registry','target_columns':['part_a','part_b']},
                        {'columns':['owner_ref'],'target_table':'s.external','target_columns':['id']}]}
    values=build_inputs({'source':{'s.registry':target,'s.events':source}})
    card=next(c for c in values.values() if c['name']=='events')
    assert [fk['automatic_relation_available'] for fk in card['foreign_keys']]==[True,False]
    assert set(f['name'] for f in card['columns'])=={'entity_a','entity_b','owner_ref'}
    assert 'automatic_relation_available=true' in KEYWORD_SYSTEM


def test_partial_or_expression_index_does_not_prove_global_uniqueness():
    from app.relationship_normalization import unique_key
    assert not unique_key({'indexes':[{'columns':['ref'],'unique':True,'predicate':'active=1'}]},['ref'])
    assert not unique_key({'indexes':[{'columns':['ref'],'unique':True,'expressions':['lower(ref)']}]},['ref'])


def test_search_is_case_insensitive_for_table_field_and_comment():
    values=cards(1);card=next(iter(values.values()))
    card['name']='Object_REGISTRY';card['columns'][0]['comment']='用户IMID'
    engine=KeywordDiscovery(values,None,{},lambda:None)
    for a,b in [('object_registry','OBJECT_REGISTRY'),('entity_ref','ENTITY_REF'),('用户IMID','用户imid')]:
        assert engine.retrieve(a)['items']==engine.retrieve(b)['items']
        assert engine.retrieve(a)['total']==1
    assert len(keyword_set(['sessionId','SESSIONID','session_id']))==2


def test_keyword_prompt_uses_query_style_sections_and_keeps_only_keyword_output():
    from app.relationship_output_schema import MARKER
    sections=['【角色】','【任务】','【输入】','【提取要求】','【依据边界】','【输出】',MARKER]
    positions=[KEYWORD_SYSTEM.index(section) for section in sections]
    assert positions==sorted(positions)
    schema=json.loads(KEYWORD_SYSTEM.split(MARKER,1)[1].split('\n',1)[1])
    assert set(schema['properties'])=={'keywords'}
    assert schema['required']==['keywords'] and schema['additionalProperties'] is False


def test_retrieval_uses_whole_expression_and_caches_all_hits():
    source=cards(107); engine=KeywordDiscovery(source,None,{},lambda:None)
    first=engine.retrieve('entity_ref')
    assert first['total']==107 and len(first['items'])==107
    source[next(iter(source))]['columns']=[]
    assert engine.retrieve('ENTITY_REF')==first  # same immutable input/version cache
    with pytest.raises(ValueError,match='non_business_search_keyword'):
        engine.retrieve('column')
    assert engine.retrieve('columns')['total']==0  # JSON attribute names are not searched
    assert engine.retrieve('对象 编号')['total']==106  # whitespace is ignored
    assert engine.retrieve('对象 不存在')['total']==0  # still not split into a broad OR


def test_later_stages_do_not_implicitly_call_the_keyword_model():
    model=Keywords(); engine=KeywordDiscovery(cards(2),model,{},lambda:None)
    with pytest.raises(DiscoveryIncomplete,match='keyword_stage_not_completed'):
        asyncio.run(engine.retrieve_all())
    with pytest.raises(DiscoveryIncomplete,match='retrieval_stage_not_completed'):
        asyncio.run(engine.discover())
    assert not model.calls


def test_small_groups_keep_comparison_and_large_groups_use_agent(monkeypatch):
    class Model:
        def __init__(self): self.phases=[]
        async def complete_json(self,system,user):
            p=json.loads(user); self.phases.append(p['phase'])
            if p['phase']=='keywords': return {'keywords':['entity_ref']}
            if p['phase']=='organization': return {'groups':[{'input_group_ids':[g['group_id']], 'table_ids':g['table_ids'], 'reason':'编号体系一致，保留待分析'} for g in p['groups']], 'excluded':[]}
            if p['phase']=='compare': return {'relations':[]}
            if p['phase']=='large_group_agent':
                return {'tool':'finish','arguments':{'checked_table_ids':[c['table_id'] for c in p['catalog']['items']], 'gaps':[]}}
            raise AssertionError(p['phase'])
    monkeypatch.setenv('RELATIONSHIP_AGENT_GROUP_THRESHOLD','3')
    for count,phase in [(3,'compare'),(4,'large_group_agent')]:
        m=Model(); asyncio.run(KeywordDiscovery(cards(count),m,{},lambda:None).run())
        assert phase in m.phases
        assert ('compare' in m.phases) != ('large_group_agent' in m.phases)


def test_agent_cannot_finish_unseen_pages_and_stops_repeating():
    class Repeat:
        async def complete_json(self,system,user):
            return {'tool':'catalog','arguments':{'page':0}}
    engine=KeywordDiscovery(cards(41),Repeat(),{},lambda:None)
    ids=tuple(sorted(engine.cards)); engine.retrieve('entity_ref')
    with pytest.raises(DiscoveryIncomplete,match='agent_no_progress'):
        asyncio.run(engine.agent(ids,['entity_ref']))
    context=next(iter(engine.scope['agents'].values()))
    assert not context.get('complete')
    assert len(context['seen'])==40


def test_early_finish_reports_missing_tables_without_marking_complete():
    engine=KeywordDiscovery(cards(2),None,{},lambda:None); ids=list(engine.cards)
    context={'seen':[ids[0]],'checked':[],'gaps':[],'relations':{}}
    result=asyncio.run(engine.agent_tool('finish',{'checked_table_ids':[ids[0]],'gaps':[]},context,ids,[]))
    assert result['error']=='unaccounted_group_tables' and not context.get('complete')


def test_database_claim_and_batch_commit_use_atomic_conflicts_and_owner_fencing():
    connection=Mock(); connection.__enter__=Mock(return_value=connection); connection.__exit__=Mock(return_value=False)
    transaction=Mock(); transaction.__enter__=Mock(); transaction.__exit__=Mock(return_value=False)
    connection.transaction.return_value=transaction
    connection.execute.return_value.fetchone.return_value=('claimed',)
    repo=Mock(dsn='postgres'); repo._connect.return_value=connection
    work=RelationshipWork(repo,'job','hash',{},lambda:None)
    assert work.claim('keywords','batch')
    work.finish('keywords','batch',{'keywords':['entity_ref']},['entity_ref'])
    statements=[c.args[0] for c in connection.execute.call_args_list]
    assert 'ON CONFLICT' in statements[0] and 'lease_until<now()' in statements[0]
    assert 'owner=%s' in statements[1] and "status='running'" in statements[1]
    assert 'ON CONFLICT' in statements[2]
    assert connection.transaction.call_count==1


def test_compact_decision_expands_internal_flags_and_keeps_conditions():
    predicate={'side':'source','field':'kind','operator':'eq','value':2}
    raw={'candidate_id':'c','outcome':'usable','kind':'entity_reference','reason':'来源对象引用目标登记'}
    result=decode_decision(raw,[predicate])
    assert result['supported'] and result['key_domain']=='same' and result['structure_sufficient']
    assert result['additional_predicates']==[predicate]
    assert decode_decision({**raw,'predicates':[]},[predicate])['additional_predicates']==[]
    assert decode_decision({**raw,'kind':'shared_entity'}) is None
    assert decode_decision({**raw,'gaps':['编号体系未确定']}) is None
    assert decode_decision({**raw,'coverage':'identifier_conflict'}) is None


def test_review_schema_does_not_request_redundant_internal_flags():
    from app.relationship_output_schema import RESEARCH_SCHEMA
    properties=RESEARCH_SCHEMA['$defs']['CompactDecision']['properties']
    assert set(properties).isdisjoint({'supported','structure_sufficient','target_role',
        'alternative_unresolved','key_domain','query_role','coverage_expectation'})


def test_inflight_evidence_is_shared_across_agent_groups():
    calls=[]
    async def tool(kind,args):
        calls.append(kind)
        await asyncio.sleep(0)
        return {'statistics':{'matched_keys':2,'guards_applied':[]}}
    engine=KeywordDiscovery(cards(2),None,{},lambda:None,tool=tool)
    async def run():
        first,second=await asyncio.gather(engine.evidence('profile','same',{}),engine.evidence('profile','same',{}))
        assert first==second
        assert await engine.evidence('profile','same',{})==first
    asyncio.run(run())
    assert calls==['profile']


def test_small_agent_roundtrip_preserves_full_mapping_guards_and_statistics(monkeypatch):
    source=cards(2); ids=list(source)
    source[ids[0]]['columns'].append({'name':'kind','type':'integer','comment':'1:主体；2:其他'})
    relation={'source_table_id':ids[0],'target_table_id':ids[1],
        'key_pairs':[{'source':'entity_ref','target':'id'}],
        'guards':[{'side':'source','field':'kind','operator':'eq','value':1}],
        'reason':'来源编号引用目标登记记录'}
    class Model:
        step=0
        calls=[]
        async def complete_json(self,system,user):
            p=json.loads(user); self.calls.append(p['phase'])
            if p['phase']=='keywords': return {'keywords':['entity_ref']}
            if p['phase']=='organization': return {'groups':[{'input_group_ids':[g['group_id']], 'table_ids':g['table_ids'], 'reason':'编号体系一致，保留待分析'} for g in p['groups']], 'excluded':[]}
            actions=[{'tool':'read_tables','arguments':{'table_ids':ids}},
                     {'tool':'profile','arguments':{'relation':relation}},
                     {'tool':'submit','arguments':{'relations':[relation]}},
                     {'tool':'finish','arguments':{'checked_table_ids':ids,'gaps':[]}}]
            result=actions[self.step]; self.step+=1
            return result
    async def tool(kind,args):
        assert kind=='profile' and args['relation']['guards']==relation['guards']
        return {'statistics':{'matched_keys':2,'directional_coverage':1,'guards_applied':relation['guards']}}
    monkeypatch.setenv('RELATIONSHIP_AGENT_GROUP_THRESHOLD','1')
    model=Model(); state={}; engine=KeywordDiscovery(source,model,state,lambda:None,tool=tool)
    result=asyncio.run(engine.run())
    assert len(result)==1
    assert result[0]['key_pairs']==relation['key_pairs'] and result[0]['guards']==relation['guards']
    assert result[0]['profile_statistics']['matched_keys']==2
    assert model.calls==['keywords','organization']+['large_group_agent']*4
    assert engine.scope['discovery_complete']


def test_search_ignores_all_whitespace_in_both_keyword_and_original_metadata():
    source=cards(2)
    for c in source.values():
        c['columns'][0]['comment']='业务会话 ID'
    engine=KeywordDiscovery(source,None,{},lambda:None)
    compact=engine.retrieve('业务会话ID')
    assert compact['total']==2
    assert engine.retrieve('业务 会话\tID')==compact
    assert len([key for key in engine.work.local if key.startswith('retrieval:')])==1
    assert len(keyword_set(['业务会话ID','业务 会话 ID']))==1


def test_retrieval_prunes_single_hits_atomically_and_survives_restart():
    source=cards(2);state={}
    engine=KeywordDiscovery(source,None,state,lambda:None)
    words=['entity_ref','record_0']
    assert engine.work.claim('keywords','batch')
    engine.work.finish('keywords','batch',{'keywords':words},words)
    engine.finish_stage('keywords',{'keyword_count':2})
    results=asyncio.run(engine.retrieve_all())
    assert [r['total'] for r in results]==[2,1]
    assert engine.work.keywords()==['entity_ref']
    assert engine.stage_result('retrieval')['counts']=={'entity_ref':2,'record_0':1}
    assert engine.work.get('keywords','batch')['payload']['keywords']==words
    restored=KeywordDiscovery(source,None,state,lambda:None)
    assert asyncio.run(restored.retrieve_all())==results
    assert restored.work.keywords()==['entity_ref']
    restored.add_keywords(['record_0','missing_object','对象'])
    assert restored.work.keywords()==['entity_ref','对象']
    assert all(len(ids)>=2 for ids in restored.groups())


def test_zero_hit_is_anomaly_and_not_silently_pruned():
    engine=KeywordDiscovery(cards(2),None,{},lambda:None)
    engine.work.claim('keywords','old')
    engine.work.finish('keywords','old',{'keywords':['missing_object']},['missing_object'])
    engine.finish_stage('keywords',{'keyword_count':1})
    with pytest.raises(DiscoveryIncomplete,match='keyword_without_source_match'):
        asyncio.run(engine.retrieve_all())
    assert engine.stage_result('retrieval') is None
    assert engine.work.keywords()==['missing_object']
    assert engine.scope['zero_hit_keywords']==['missing_object']


def test_keyword_must_match_its_actual_batch_input_after_whitespace_normalization():
    class Model:
        calls=0
        async def complete_json(self,system,user):
            self.calls+=1
            return {'keywords':['概括改写后的不存在对象']}
    model=Model();engine=KeywordDiscovery(cards(2),model,{},lambda:None)
    with pytest.raises(DiscoveryIncomplete,match='keyword_without_source_match'):
        asyncio.run(engine.extract_keywords())
    assert model.calls==3 and not engine.work.keywords()
