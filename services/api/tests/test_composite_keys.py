import copy
import re
import sqlite3
from types import SimpleNamespace

import pytest
from semora_contracts import BoundKeyFilter, QueryPlan, StepInput, ExecutionResult, SystemSettings
from semora_agent.multisource import validate_source_steps
from app.source_execution import SourceExecution
from app.planner import compile_plan
from test_source_execution import ontology, proposal


def composite():
    o = ontology()
    for obj in o['objects']:
        obj['properties'].append({'name': 'booking', 'column': 'booking', 'type': 'integer'})
    o['relations'][0]['condition'] += ' AND client.booking = balances.booking'
    p = proposal()
    p['source_steps'][0]['query']['fields'].append('client.booking')
    p['source_steps'][1]['query']['fields'].append('balances.booking')
    b = p['source_steps'][1]['inputs'][0]
    b['key_pairs'] = [{'source_field': b.pop('source_field'), 'target_field': b.pop('target_field')},
                      {'source_field': 'client.booking', 'target_field': 'balances.booking'}]
    p.update(domain='commerce', data_source_id='accounts-db', metrics=[])
    return o, QueryPlan.model_validate(p)


def test_composite_contract_requires_all_published_keys():
    o, p = composite()
    validate_source_steps(p, o)
    p.source_steps[1].inputs[0].key_pairs.pop()
    with pytest.raises(ValueError, match='全部复合键'):
        validate_source_steps(p, o)


def test_contract_rejects_mixed_or_duplicate_keys():
    _, p = composite()
    b = p.source_steps[1].inputs[0].model_dump()
    b['source_field'] = 'client.customer_id'
    with pytest.raises(ValueError): StepInput.model_validate(b)
    b['source_field'] = None
    b['key_pairs'].append(b['key_pairs'][0])
    with pytest.raises(ValueError): StepInput.model_validate(b)


def test_composite_tuple_binding_dedup_null_and_exact_enrichment():
    o, p = composite()
    binding = p.source_steps[1].inputs[0]
    source = ExecutionResult(columns=['client_customer_id', 'client_booking', 'client_segment'],
        rows=[['A', 1, 'first'], ['A', 1, 'first'], ['B', 2, 'second'], ['A', None, 'ignore']],
        row_count=4, duration_ms=0, truncated=False)
    execution = object.__new__(SourceExecution)
    execution.ontology = o
    execution.settings = SystemSettings()
    execution.run = SimpleNamespace(source_results={'pick_clients': source})
    assert execution.keys(binding) == [('A', 1), ('B', 2)]
    result = ExecutionResult(columns=['balances_customer_id', 'balances_booking'], rows=[['B',2],['A',1]], row_count=2, duration_ms=0, truncated=False)
    assert execution.enrich(p.source_steps[1], result).rows == [['B',2,'second'], ['A',1,'first']]
    result.columns = ['balances_customer_id', 'balances_booking']
    result.rows = [['A',2]]
    with pytest.raises(ValueError, match='精确匹配'): execution.enrich(p.source_steps[1], result)


def test_tuple_predicate_never_matches_cross_product_and_empty_aggregate():
    o, p = composite()
    from semora_agent.multisource import source_ontology
    local = source_ontology(o, 'accounts-db')
    q = QueryPlan.model_validate(p.source_steps[1].query.model_dump())
    q.bound_key_filters = [BoundKeyFilter(fields=['balances.customer_id','balances.booking'],rows=[['A',1],['B',2]])]
    sql = compile_plan(q, local)
    db = sqlite3.connect(':memory:')
    db.executescript("CREATE TABLE accounts(id integer,customer_id text,booking integer,amount numeric); INSERT INTO accounts VALUES(1,'A',1,10),(2,'A',2,99),(3,'B',1,99),(4,'B',2,20);")
    def execute(artifact):
        import sqlglot
        text = re.sub(r'%\(([A-Za-z_][A-Za-z0-9_]*)\)s', r':\1', artifact.physical_sql)
        return db.execute(sqlglot.transpile(text,read='postgres',write='sqlite')[0], artifact.parameters).fetchall()
    assert execute(sql) == [('A',10,1), ('B',20,2)]
    assert ' OR ' in sql.physical_sql and ' IN ' not in sql.physical_sql
    q.bound_key_filters[0].rows=[]
    q.fields=[]
    q.metrics=['n']
    from semora_contracts import QueryCalculation
    q.calculations=[QueryCalculation(name='n',function='count')]
    assert execute(compile_plan(q,local)) == [(0,)]


def test_distinct_projection_local_student_grain():
    o={'domain':'test','dataSourceId':'one','objects':[{'name':'student','table':'appointments','primaryKey':'s_id','projection':{'distinct':True},'properties':[{'name':'s_id','column':'s_id','type':'integer'}]}],'relations':[],'metrics':[]}
    q=QueryPlan(domain='test',data_source_id='one',intent='students',metrics=[],fields=['student.s_id'])
    sql=compile_plan(q,o)
    db=sqlite3.connect(':memory:')
    db.executescript('CREATE TABLE appointments(s_id integer); INSERT INTO appointments VALUES(1),(1),(2);')
    assert db.execute(sql.physical_sql).fetchall()==[(1,),(2,)]
    o['objects'][0]['primaryKey']='undeclared'
    with pytest.raises(ValueError,match='声明'):compile_plan(q,o)
