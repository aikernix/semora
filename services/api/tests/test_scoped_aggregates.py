import sqlite3
from copy import deepcopy
import re

import pytest
import sqlglot

from semora_agent import AnalystAgent
from semora_contracts import QueryIntent,ReviewDecision,ReviewFinding
from semora_query_core.intent import compile_intent
from semora_query_core.sql import compile_plan,adapt_mysql_artifact
from semora_agent.review_policy import repairable_paths,validate_resolution


def fixture():
    def obj(name,props):return dict(name=name,table=name,primaryKey='id',properties=[dict(name=k,column=k,type=t) for k,t in props.items()])
    ontology=dict(domain='generic',dataSourceId='source',objects=[
        obj('entity',{'id':'integer','label':'string'}),
        obj('position',{'id':'integer','entity_id':'integer','currency':'string','balance':'number','due':'number','days':'integer'}),
        obj('movement',{'id':'integer','position_id':'integer','occurred':'integer','amount':'number','fee':'number'}),
    ],relations=[dict(name='positions',**{'from':'entity','to':'position'},condition='entity.id=position.entity_id',cardinality='1:N'),
        dict(name='movements',**{'from':'movement','to':'position'},condition='movement.position_id=position.id',cardinality='N:1')],metrics=[])
    scope=[dict(field='movement.occurred',operator='gte',value=10),dict(field='movement.occurred',operator='lt',value=100)]
    intent=dict(summary='按主体和类别汇总，事件指标只统计指定窗口，无事件仍保留，保留汇总值大于0的分组',subject='entity',
        fields=['entity.id','position.currency'],group_by=['entity.id','position.currency'],
        measures=[dict(name='items',function='count_distinct',field='position.id'),dict(name='balance',function='sum',field='position.balance'),
            dict(name='due',function='sum',field='position.due'),dict(name='days',function='max',field='position.days'),
            dict(name='amount',function='sum',field='movement.amount',filters=scope,null_value=0),
            dict(name='fee',function='sum',field='movement.fee',filters=scope,null_value=0)],
        optional_relationships=['movements'],having=[dict(field='due',operator='gt',value=0)],
        relationship_roles=['positions','movements'],order_by=[dict(field='due',direction='desc')],limit=20)
    return ontology,intent


def build(intent=None):
    ontology,default=fixture();intent=default if intent is None else intent
    outcome=AnalystAgent()._build_outcome(compile_intent(intent,ontology,{}),'query',[],ontology,{},'test')
    return ontology,outcome


def database():
    db=sqlite3.connect(':memory:')
    db.executescript('''CREATE TABLE entity(id INTEGER,label TEXT); INSERT INTO entity VALUES(1,'a'),(2,'b'),(3,'c');
    CREATE TABLE position(id INTEGER,entity_id INTEGER,currency TEXT,balance NUMERIC,due NUMERIC,days INTEGER);
    INSERT INTO position VALUES(11,1,'USD',100,10,5),(12,1,'USD',200,0,0),(21,2,'USD',80,4,2),(31,3,'USD',90,0,0),(13,1,'EUR',7,6,3);
    CREATE TABLE movement(id INTEGER,position_id INTEGER,occurred INTEGER,amount NUMERIC,fee NUMERIC);
    INSERT INTO movement VALUES(1,11,20,12,1),(2,11,5,999,99),(3,12,30,3,2),(4,13,40,4,0.5),(5,31,50,123,10);''')
    return db


def execute(artifact):
    sql=re.sub(r'%\(([A-Za-z_]\w*)\)s',r':\1',artifact.physical_sql)
    sql=sqlglot.transpile(sql,read='mysql' if artifact.dialect=='mysql' else 'postgres',write='sqlite')[0]
    return [list(row) for row in database().execute(sql,artifact.parameters).fetchall()]


@pytest.mark.parametrize('mysql',[False,True])
def test_independent_windows_outer_preservation_null_fill_and_group_filter(mysql):
    ontology,outcome=build();plan=outcome.plan
    assert plan.stages[0].join_type=='left'
    assert not plan.stages[0].filters and not plan.filters
    assert len(plan.stages[0].measures[0].filters)==2
    artifact=compile_plan(plan,ontology)
    if mysql:artifact=adapt_mysql_artifact(artifact)
    assert execute(artifact)==[[1,'USD',2,300,10,5,15,3],[1,'EUR',1,7,6,3,4,0.5],[2,'USD',1,80,4,2,0,0]]
    assert 'HAVING' in artifact.physical_sql and 'LEFT' in artifact.physical_sql
    assert 10 in artifact.parameters.values() and 100 in artifact.parameters.values()


def test_filtered_measures_on_same_field_do_not_share_the_wrong_stage_measure():
    ontology,intent=fixture()
    intent['measures'][-1]=dict(name='older',function='sum',field='movement.amount',filters=[dict(field='movement.occurred',operator='lt',value=10)],null_value=0)
    _,outcome=build(intent);artifact=compile_plan(outcome.plan,ontology)
    rows=execute(artifact)
    assert rows[0][-2:]==[15,999]
    assert rows[1][-2:]==[4,0] and rows[2][-2:]==[0,0]


def test_single_object_conditional_aggregate_is_parameterized():
    ontology,_=fixture()
    intent=dict(summary='按类别统计窗口内数值',subject='movement',measures=[dict(name='value',function='sum',field='movement.amount',
        filters=[dict(field='movement.occurred',operator='gte',value=10)],null_value=0)])
    _,outcome=build(intent);artifact=compile_plan(outcome.plan,ontology)
    assert execute(artifact)==[[142]]
    assert 10 in artifact.parameters.values()


def test_review_paths_include_optional_business_controls_and_explain_invalid_paths():
    _,outcome=build();paths=repairable_paths(outcome.plan)
    for path in ['/query_intent/having','/query_intent/optional_relationships','/query_intent/measures/4/filters']:
        assert path in paths
        decision=ReviewDecision(decision='revise',rationale='缺口',findings=[ReviewFinding(kind='repair',paths=[path],message='待修订',evidence='当前口径',acceptance='字段落实')])
        validate_resolution(decision,[],outcome.plan)
    decision.findings[0].paths=['/requirement/join_type']
    with pytest.raises(ValueError,match='/requirement/join_type'):validate_resolution(decision,[],outcome.plan)


def test_declared_conditions_cannot_be_removed_after_compilation():
    from semora_agent.constraints import validate_intent_filter_coverage
    _,outcome=build()
    for mutation in ['having','optional_relationships','filters','null_value']:
        plan=outcome.plan.model_copy(deep=True)
        if mutation in ['having','optional_relationships']:setattr(plan,mutation,[])
        elif mutation=='filters':plan.calculations[-1].filters=[]
        else:plan.calculations[-1].null_value=None
        with pytest.raises(ValueError):validate_intent_filter_coverage(plan)


def test_new_default_fields_do_not_change_old_serialized_contracts():
    from semora_contracts import QueryCalculation,QueryPlan
    assert 'filters' not in QueryCalculation(name='n',function='sum',field='entity.id').model_dump()
    assert 'null_value' not in QueryCalculation(name='n',function='sum',field='entity.id').model_dump()
    old=QueryIntent(summary='list',subject='entity',fields=['entity.id']).model_dump()
    assert 'having' not in old and 'optional_relationships' not in old
    assert 'having' not in QueryPlan(domain='generic',intent='list',metrics=[]).model_dump()
