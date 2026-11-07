import asyncio
import copy
import re

import pytest
import sqlglot
from pydantic import ValidationError

from app.planner import compile_plan, adapt_mysql_artifact, PlanningError
from app.query_stages import validate_query_shape
from semora_agent import AnalystAgent
from semora_agent.model import StructuredModel
from semora_contracts import QueryPlan, RunStatus
from services.api.tests.test_source_execution import harness, ontology, proposal
from services.api.tests.test_plan_candidates import ONTOLOGY, PLAN


def plan(**overrides):
    return QueryPlan(domain='generic', data_source_id='source-a',
                     **{'metrics':[],**PLAN, 'order_by': [{'field': 'record.amount', 'direction': 'desc'}, {'field': 'record.id', 'direction': 'asc'}],
                        'fields': ['record.id', 'record.amount'], 'limit': 10, **overrides})


def test_order_and_limit_compile_in_postgres_and_mysql():
    p = plan()
    artifact = compile_plan(p, ONTOLOGY)
    for sql, dialect in [(artifact.physical_sql, 'postgres'), (adapt_mysql_artifact(artifact).physical_sql, 'mysql')]:
        tree = sqlglot.parse_one(re.sub(r'%\((\w+)\)s', '0', sql), read=dialect)
        assert tree.args['limit'].expression.this == '10'
        ordered = [item for item in tree.args['order'].expressions if isinstance(item.this, sqlglot.exp.Column)]
        assert ordered[0].this.name == 'record_amount'
        assert ordered[0].args['desc'] is True
        assert ordered[1].this.name == 'record_id'
    assert artifact.artifact_hash != compile_plan(plan(limit=11), ONTOLOGY).artifact_hash


@pytest.mark.parametrize('limit', [0, 1001, -1, True, '10'])
def test_limit_is_bounded_strict_integer(limit):
    with pytest.raises(ValidationError):
        plan(limit=limit)


def test_order_requires_selected_field_and_limit_requires_order():
    for p in [plan(order_by=[]), plan(order_by=[{'field': 'record.private', 'direction': 'asc'}])]:
        with pytest.raises(ValueError):
            validate_query_shape(p, ONTOLOGY)
        with pytest.raises(PlanningError):
            compile_plan(p, ONTOLOGY)


def test_top_n_survives_analysis_confirmation_and_cross_source_execution(monkeypatch):
    async def scenario():
        w, session, calls = harness(monkeypatch)
        candidate = copy.deepcopy(proposal())
        candidate['source_steps'][0]['query'].update(order_by=[{'field': 'client.customer_id', 'direction': 'desc'}], limit=1)
        model = StructuredModel(); model.base_url = model.api_key = ''; model.mode = 'optional'
        class Analyst:
            async def analyse(self, question, evidence, ont, scope):
                return AnalystAgent(model)._build_outcome(candidate, question, evidence, ont, scope, 'test')
        w.analyst = Analyst()
        result = await w.start(session.id, '按指定排序取一条记录后关联', domain='commerce')
        assert result.status == RunStatus.COMPLETED, result.error
        assert calls[0][1].endswith('LIMIT 1')
        assert 'ORDER BY client_customer_id DESC' in calls[0][1]
        assert len(calls) == 2
        assert result.requirement.source_steps[0].query.limit == 1
        assert result.requirement.source_steps[0].query.order_by[0].direction == 'desc'
        assert result.result.row_count == 2
    asyncio.run(scenario())


def test_single_query_cannot_join_objects_from_different_sources():
    from services.api.tests.test_source_execution import ontology
    ont = ontology()
    raw = {'fields': ['client.customer_id', 'balances.amount'], 'relationship_path': ['customer_accounts']}
    with pytest.raises(ValueError, match='source_steps'):
        AnalystAgent()._build_outcome(raw, '关联记录', [], ont, {}, 'test')
    p = QueryPlan(domain='commerce', data_source_id='customers-db', intent='关联记录', metrics=[], **raw)
    with pytest.raises(PlanningError, match='source_steps'):
        compile_plan(p, ont)


def test_cross_source_boundary_can_join_local_intermediate_to_final_object():
    from semora_agent.multisource import validate_source_steps,source_ontology
    from services.api.tests.test_source_execution import ontology,proposal
    ont=ontology()
    balance=next(o for o in ont['objects'] if o['name']=='balances')
    balance['properties'].append({'name':'id','column':'id','type':'integer'})
    ont['objects'].append({'name':'details','label':'明细','table':'detail_rows','primaryKey':'id','dataSourceId':'accounts-db',
        'properties':[{'name':'id','column':'id','type':'integer'},
                      {'name':'balance_id','column':'balance_id','type':'integer'},
                      {'name':'duration','column':'duration','type':'integer'}]})
    ont['relations'].append({'name':'balance_details','from':'balances','to':'details','condition':'balances.id = details.balance_id','cardinality':'1:N','joinType':'LEFT'})
    value=proposal()
    value['source_steps'][1]['query'].update(grain='details',fields=['balances.customer_id','details.duration'],relationship_path=['balance_details'])
    model=StructuredModel();model.base_url=model.api_key=''
    outcome=AnalystAgent(model)._build_outcome(value,'选定对象后查询关联明细',[],ont,{},'test')
    assert validate_source_steps(outcome.plan,ont)['steps']==2
    local=outcome.plan.source_steps[1]
    artifact=compile_plan(QueryPlan.model_validate(local.query.model_dump()),source_ontology(ont,local.data_source_id))
    assert 'JOIN' in artifact.physical_sql and 'detail_rows' in artifact.physical_sql
    value['source_steps'][1]['query']['relationship_path']=['customer_accounts','balance_details']
    with pytest.raises(ValueError,match=r'customer_accounts.*balance_details.*inputs.relation'):
        AnalystAgent(model)._build_outcome(value,'选定对象后查询关联明细',[],ont,{},'test')


def test_latest_entity_timestamp_aggregate_preserves_type_and_deduplicates_before_limit():
    import sqlite3
    ont=copy.deepcopy(ONTOLOGY)
    obj=ont['objects'][0]
    obj['properties'] += [{'name':'entity_id','column':'entity_id','type':'string'},{'name':'finished_at','column':'finished_at','type':'timestamp'}]
    p=plan(fields=['record.entity_id'],dimensions=['record.entity_id'],metrics=['latest_time'],filters=[],
        calculations=[{'name':'latest_time','function':'max','field':'record.finished_at'}],
        order_by=[{'field':'latest_time','direction':'desc'},{'field':'record.entity_id','direction':'asc'}],limit=2)
    artifact=compile_plan(p,ont)
    for sql in [artifact.physical_sql,adapt_mysql_artifact(artifact).physical_sql]:
        assert 'ROUND' not in sql and 'numeric' not in sql.lower() and 'DECIMAL' not in sql
    db=sqlite3.connect(':memory:')
    db.execute('CREATE TABLE '+obj['table']+' (id INTEGER,amount NUMERIC,entity_id TEXT,finished_at TEXT)')
    db.executemany('INSERT INTO '+obj['table']+' VALUES(?,?,?,?)',[(1,1,'A','2026-11-05 23:00:00'),(2,2,'A','2026-11-05 22:59:00'),(3,3,'B','2026-11-05 22:00:00'),(4,4,'C','2026-11-05 21:00:00')])
    rows=db.execute(re.sub(r'%\((\w+)\)s',r':\1',artifact.physical_sql),artifact.parameters).fetchall()
    assert [r[0] for r in rows]==['A','B']


def test_idempotent_max_stage_folds_without_losing_duplicates_nulls_or_left_rows():
    import sqlite3
    from app.planner import fold_idempotent_stages
    ont=copy.deepcopy(ONTOLOGY)
    ont['objects'].append({'name':'line','table':'lines','primaryKey':'id','properties':[{'name':'id','column':'id','type':'integer'},{'name':'record_id','column':'record_id','type':'integer'},{'name':'value','column':'value','type':'number'}]})
    ont['relations']=[{'name':'record_lines','from':'record','to':'line','condition':'record.id = line.record_id','cardinality':'1:N','joinType':'LEFT'}]
    p=plan(fields=['record.id'],dimensions=['record.id'],metrics=['peak'],filters=[],grain='record',
        relationship_path=['record_lines'],calculations=[{'name':'peak','function':'max','field':'per_record.peak'}],
        stages=[{'name':'per_record','source':'line','group_by':['line.record_id'],'measures':[{'name':'peak','function':'max','field':'line.value'}]}],
        order_by=[{'field':'record.id','direction':'asc'}],limit=None)
    artifact=compile_plan(p,ont)
    assert 'WITH ' not in artifact.physical_sql and artifact.compilation['optimizations'][0]['kind']=='fold_idempotent_preaggregation'
    assert p.stages and p.calculations[0].field=='per_record.peak'
    db=sqlite3.connect(':memory:')
    db.executescript('CREATE TABLE records(id INTEGER,amount NUMERIC); INSERT INTO records VALUES(1,0),(2,0),(3,0); CREATE TABLE lines(id INTEGER,record_id INTEGER,value NUMERIC); INSERT INTO lines VALUES(1,1,2),(2,1,2),(3,1,9),(4,2,6),(5,2,NULL);')
    sql=sqlglot.transpile(artifact.physical_sql,read='postgres',write='sqlite')[0]
    assert db.execute(sql).fetchall()==[(1,9.0),(2,6.0),(3,None)]
    p.calculations[0].function='sum';p.stages[0].measures[0].function='sum'
    assert fold_idempotent_stages(p) is p
    additive=compile_plan(p,ont)
    assert additive.compilation['optimizations'][0]['kind']=='fold_safe_additive_preaggregation'
    additive_sql=sqlglot.transpile(additive.physical_sql,read='postgres',write='sqlite')[0]
    expected=db.execute('SELECT r.id,SUM(s.total) FROM records r LEFT JOIN (SELECT record_id,SUM(value) total FROM lines GROUP BY record_id) s ON r.id=s.record_id GROUP BY r.id ORDER BY r.id').fetchall()
    assert db.execute(additive_sql).fetchall()==expected==[(1,13.0),(2,6.0),(3,None)]
    assert p.stages and p.calculations[0].field=='per_record.peak'
    p.calculations[0].function='max';p.stages[0].measures[0].function='max'
    p.stages[0].filters=[__import__('semora_contracts').QueryFilter(field='line.value',operator='gt',value=0)]
    assert fold_idempotent_stages(p) is p


def test_additive_stage_not_folded_when_raw_joins_would_multiply_rows():
    ont=copy.deepcopy(ONTOLOGY)
    ont['objects'] += [{'name':n,'table':n,'primaryKey':'id','properties':[{'name':p,'column':p,'type':'integer'} for p in ['id','record_id','value']]} for n in ['first','second']]
    ont['relations']=[{'name':'r_'+n,'from':'record','to':n,'condition':f'record.id = {n}.record_id','cardinality':'1:N'} for n in ['first','second']]
    p=plan(fields=['record.id'],dimensions=['record.id'],metrics=['a','b'],grain='record',filters=[],
        relationship_path=['r_first','r_second'],
        calculations=[{'name':name,'function':'sum','field':n+'_stage.total'} for n,name in [('first','a'),('second','b')]],
        stages=[{'name':n+'_stage','source':n,'group_by':[n+'.record_id'],'measures':[{'name':'total','function':'sum','field':n+'.value'}]} for n in ['first','second']],
        order_by=[{'field':'record.id','direction':'asc'}],limit=None)
    artifact=compile_plan(p,ont)
    assert 'WITH ' in artifact.physical_sql
    assert not artifact.compilation.get('optimizations')
