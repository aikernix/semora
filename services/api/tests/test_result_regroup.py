"""Generic entity → records → remote facts, preserving business grain."""
import asyncio
from copy import deepcopy
from decimal import Decimal
import re
import sqlite3

import pytest
import sqlglot
from semora_agent import AnalystAgent
from semora_contracts import ExecutionResult, OntologySnapshotRef, RunStatus
from semora_query_core.results import regroup_result
from semora_query_core.sources import validate_source_steps
from app.intent_planner import compile_intent, IntentPlanningError, source_output_contract
from services.api.tests.test_source_execution import harness


def test_existing_serialized_plan_hash_shape_is_unchanged():
    from semora_contracts import QueryPlan
    from services.api.tests.test_source_execution import proposal
    legacy=proposal()['source_steps'][-1]
    from semora_contracts import SourceQueryStep
    step=SourceQueryStep.model_validate(legacy)
    assert 'regroup' not in step.model_dump(mode='json')
    plan=QueryPlan(domain='generic',intent='list',metrics=[])
    assert 'aggregate_precision' not in plan.model_dump(mode='json')
    plan.aggregate_precision='raw'
    assert plan.model_dump(mode='json')['aggregate_precision']=='raw'


def fixture():
    def obj(name,source,props):
        return dict(name=name,table=name,primaryKey='id',dataSourceId=source,
            properties=[dict(name=k,column=k,type=t) for k,t in props.items()])
    ont=dict(domain='commerce',dataSourceId='customers-db',metrics=[],objects=[
        obj('entity','customers-db',{'id':'integer'}),
        obj('record','customers-db',{'id':'integer','entity_id':'integer','ended_at':'integer'}),
        obj('fact','accounts-db',{'id':'integer','record_id':'integer','amount':'decimal'})],relations=[
        dict(name='entity_records',**{'from':'entity','to':'record'},condition='entity.id = record.entity_id',cardinality='1:N'),
        dict(name='record_facts',**{'from':'record','to':'fact'},condition='record.id = fact.record_id',cardinality='1:N')])
    intent=dict(summary='选取最近的不同对象并累计关联金额',subject='entity',fields=['entity.id'],group_by=['entity.id'],
        measures=[dict(name='total',function='sum',field='fact.amount')],
        selection=dict(object='entity',unit='entities',time_field='record.ended_at',limit=2),
        record_scope='all_related_records',order_by=[dict(field='total',direction='desc')],limit=1)
    return ont,intent


def build(ont,intent):
    value=compile_intent(intent,ont,{})
    return AnalystAgent()._build_outcome(value,'query',[],ont,{},'test')


@pytest.mark.parametrize('scope',['all_related_records','selection_window'])
def test_selection_predicate_in_global_explanation_is_not_required_in_every_step(scope):
    ont,intent=fixture()
    ont['objects'][1]['properties'].append(dict(name='state',column='state',type='string'))
    predicate=dict(field='record.state',operator='eq',value='ready')
    intent['selection']['filters']=[predicate]
    intent['assumptions']=["选取条件 record.state = 'ready'；后续范围以record_scope为准"]
    intent['record_scope']=scope
    result=build(ont,intent)
    steps=result.plan.source_steps
    assert steps[0].query.filters[0].model_dump(mode='json')==predicate
    lookup=steps[1].query
    assert any(f.field=='record.state' for f in lookup.filters)==(scope=='selection_window')
    assert not any(f.field=='record.state' for f in steps[-1].query.filters)


def test_legacy_condition_missing_from_all_steps_is_still_rejected():
    ont,intent=fixture()
    ont['objects'][1]['properties'].append(dict(name='state',column='state',type='string'))
    intent['assumptions']=["必须满足 record.state = 'ready'"]
    value=compile_intent(intent,ont,{})
    value.pop('query_intent')
    with pytest.raises(ValueError,match='filters'):
        AnalystAgent()._build_outcome(value,'query',[],ont,{},'test')


def test_structured_intent_narrative_difference_is_advisory_not_hard_failure():
    from semora_agent.constraints import missing_explained_filters
    ont,intent=fixture()
    ont['objects'][1]['properties'].append(dict(name='state',column='state',type='string'))
    intent['assumptions']=["历史说明 record.state = 'ready'"]
    result=build(ont,intent)
    warnings=missing_explained_filters(result.plan,result.requirement.assumptions,ont)
    assert len(warnings)==1 and warnings[0].field=='record.state'


@pytest.mark.parametrize('mutation',['missing_selection','missing_query','missing_window','leaked_selection'])
def test_declared_conditions_still_have_strict_scope_coverage(mutation):
    from semora_agent.constraints import validate_intent_filter_coverage
    from semora_contracts import QueryFilter
    ont,intent=fixture()
    predicate=dict(field='record.ended_at',operator='gt',value=1)
    intent['selection']['filters']=[predicate]
    if mutation=='missing_window':intent['record_scope']='selection_window'
    if mutation=='missing_query':intent['filters']=[dict(field='fact.amount',operator='gt',value=0)]
    plan=build(ont,intent).plan
    if mutation=='missing_selection':plan.source_steps[0].query.filters=[]
    if mutation=='missing_query':plan.source_steps[-1].query.filters=[]
    if mutation=='missing_window':plan.source_steps[1].query.filters=[]
    if mutation=='leaked_selection':plan.source_steps[1].query.filters=[QueryFilter.model_validate(predicate)]
    with pytest.raises(ValueError,match='程序规划'):
        validate_intent_filter_coverage(plan)


def test_compiler_keeps_entity_grain_and_defers_order_limit():
    ont,intent=fixture()
    result=build(ont,intent)
    step=result.plan.source_steps[-1]
    assert step.regroup.group_by==['entity.id']
    assert step.query.dimensions==['fact.record_id']
    assert step.query.limit is None and step.query.order_by==[]
    assert step.regroup.limit==1
    assert result.plan.dimensions==result.requirement.dimensions==['entity.id']
    assert result.plan.order_by==step.regroup.order_by
    assert result.plan.limit==result.requirement.limit==1
    assert result.requirement.output_fields==['entity.id','total']
    assert source_output_contract(result.plan)['steps'][-1]['final_columns']==['entity_id','total']
    assert 'entity.id' in result.plan.source_steps[-2].query.fields


@pytest.mark.parametrize('function',['avg','count_distinct'])
def test_non_mergeable_partials_fail_without_changing_meaning(function):
    ont,intent=fixture()
    intent['measures'][0]['function']=function
    with pytest.raises(IntentPlanningError,match='SUM'):
        build(ont,intent)


@pytest.mark.parametrize('mutation',['limit','order','function','group','non_pk'])
def test_regroup_contract_cannot_bypass_grain_or_completeness_checks(mutation):
    ont,intent=fixture()
    plan=build(ont,intent).plan
    step=plan.source_steps[-1]
    if mutation=='limit':step.query.limit=1
    if mutation=='order':step.query.order_by=step.regroup.order_by
    if mutation=='function':step.regroup.measures[0].function='count'
    if mutation=='group':step.regroup.group_by=['fact.amount']
    if mutation=='non_pk':ont['objects'][1]['primaryKey']='entity_id'
    with pytest.raises(ValueError):validate_source_steps(plan,ont)


def test_merge_nulls_counts_numeric_cache_and_single_rounding():
    ont,intent=fixture()
    step=build(ont,intent).plan.source_steps[-1]
    step.regroup.limit=None
    # SQL partials retain precision; only the final total is rounded.
    cols=['fact_record_id','total','lookup_1__entity_id']
    binding=step.inputs[0]
    cols[-1]=binding.source_step+'__entity_id'
    raw=ExecutionResult(columns=cols,rows=[[11,'0.004',1],[12,'0.004',1],[21,None,2]],row_count=3,duration_ms=1)
    result=regroup_result(step,raw,max_rows=1000,ontology=ont)
    assert result.rows==[[1,Decimal('0.01')],[2,None]]
    assert result.row_count==2 and raw.row_count==3
    step.query.calculations[0].function='min'
    step.regroup.measures[0].function='min'
    raw.rows=[[11,'10',1],[12,'2',1]];raw.row_count=2
    assert regroup_result(step,raw,max_rows=1000,ontology=ont).rows==[[1,Decimal('2')]]
    raw.truncated=True
    with pytest.raises(ValueError,match='不完整'):
        regroup_result(step,raw,max_rows=1000,ontology=ont)


@pytest.mark.parametrize('function,values,expected',[
    ('count',[2,0,3],Decimal('5')),('max',['10','2',None],Decimal('10')),
    ('sum',[None,None,None],None),('sum',[],None)])
def test_associative_merges_preserve_empty_null_and_count_semantics(function,values,expected):
    ont,intent=fixture()
    intent['measures'][0]['function']=function
    step=build(ont,intent).plan.source_steps[-1]
    raw=ExecutionResult(columns=['fact_record_id','total',step.inputs[0].source_step+'__entity_id'],
        rows=[[i,v,1] for i,v in enumerate(values)],row_count=len(values),duration_ms=1)
    result=regroup_result(step,raw,max_rows=1000,ontology=ont)
    assert result.rows==([[1,expected]] if values else [])
    if values:
        raw.summary['masked_columns']=[raw.columns[-1]]
        with pytest.raises(ValueError,match='脱敏'):
            regroup_result(step,raw,max_rows=1000,ontology=ont)


def test_merge_preserves_database_numeric_precision_beyond_decimal_default():
    ont,intent=fixture();step=build(ont,intent).plan.source_steps[-1]
    raw=ExecutionResult(columns=['fact_record_id','total',step.inputs[0].source_step+'__entity_id'],
        rows=[[1,'123456789012345678901234567890.12',1],[2,'0.01',1]],row_count=2,duration_ms=1)
    result=regroup_result(step,raw,max_rows=1000,ontology=ont)
    assert result.rows==[[1,Decimal('123456789012345678901234567890.13')]]


@pytest.mark.parametrize('truncate',[False,True])
def test_real_sql_execution_merges_before_sort_and_limit(monkeypatch,truncate):
    async def scenario():
        ont,intent=fixture()
        intent['selection']['filters']=[dict(field='record.ended_at',operator='gt',value=10)]
        intent['assumptions']=['仅在选取阶段要求 record.ended_at > 10；选取后累计全部关联记录']
        w,session,calls=harness(monkeypatch)
        db={'customers-db':sqlite3.connect(':memory:'),'accounts-db':sqlite3.connect(':memory:')}
        db['customers-db'].executescript('CREATE TABLE entity(id INTEGER); INSERT INTO entity VALUES(1),(2),(3); CREATE TABLE record(id INTEGER,entity_id INTEGER,ended_at INTEGER); INSERT INTO record VALUES(11,1,20),(12,1,2),(21,2,18),(31,3,1);')
        # Entity 1 wins after merging, though each single record is below entity 2.
        db['accounts-db'].executescript('CREATE TABLE fact(id INTEGER,record_id INTEGER,amount NUMERIC); INSERT INTO fact VALUES(1,11,4),(2,11,4),(3,12,8),(4,21,10),(5,31,999);')
        async def snapshot(domain):return OntologySnapshotRef(domain='commerce',version='1',content_hash='test',artifact_version='test'),ont
        w.snapshot_loader=snapshot
        class Analyst:
            async def analyse(self,*args):return build(ont,intent)
        w.analyst=Analyst()
        sqls=[]
        async def execute(sql,parameters,sensitive,settings):
            sqls.append((settings['data_source_id'],sql,parameters))
            converted=re.sub(r'%\(([A-Za-z_][A-Za-z0-9_]*)\)s',r':\1',sql)
            cur=db[settings['data_source_id']].execute(sqlglot.transpile(converted,read='postgres',write='sqlite')[0],parameters)
            rows=[list(r) for r in cur.fetchall()]
            return ExecutionResult(columns=[c[0] for c in cur.description],rows=rows,row_count=len(rows),duration_ms=1,
                truncated=truncate and settings['data_source_id']=='accounts-db',summary={'masked_columns':[]})
        w.execute=execute
        run=await w.start(session.id,'select recent entities and total facts',domain='commerce')
        if truncate:
            assert run.status==RunStatus.FAILED and run.result is None
            return
        assert run.status==RunStatus.COMPLETED,run.error
        assert run.result.columns==['entity_id','total'] and run.result.rows==[[1,Decimal('16')]]
        assert [s[0] for s in sqls]==['customers-db','customers-db','accounts-db']
        assert 'ROUND' not in sqls[-1][1] and sqls[-1][1].endswith('LIMIT 1001')
        assert run.source_results['result'].row_count==3
        assert any(e.event=='source.result.regrouped' and e.data['row_count']==1 for e in run.events)
        assert w.store.audit.verify(run.id)==(True,None)
    asyncio.run(scenario())
