"""Existence filtering over complete source sets; no domain-specific rules."""
import asyncio
from copy import deepcopy
import re
import sqlite3
import pytest
import sqlglot
from semora_agent import AnalystAgent
from semora_contracts import ExecutionResult,OntologySnapshotRef,RunStatus,QueryPlan
from semora_query_core.results import membership_result
from semora_query_core.sources import validate_source_steps
from semora_agent.constraints import validate_intent_filter_coverage
from services.api.tests.test_result_regroup import fixture,build
from services.api.tests.test_source_execution import harness


def scenario_intent(present=False):
    ont,intent=fixture()
    intent.update(group_by=[],measures=[],order_by=[dict(field='entity.id',direction='asc')],limit=None,
        existence=dict(relation='record_facts',anchor='record',present=present))
    return ont,intent


@pytest.mark.parametrize('present',[False,True])
@pytest.mark.parametrize('same_source',[False,True])
@pytest.mark.parametrize('empty',[False,True])
def test_complete_entity_membership_with_multiple_records_and_duplicate_matches(monkeypatch,present,same_source,empty):
    async def scenario():
        ont,intent=scenario_intent(present)
        if same_source:ont['objects'][-1]['dataSourceId']='customers-db'
        w,session,_=harness(monkeypatch)
        db={'customers-db':sqlite3.connect(':memory:'),'accounts-db':sqlite3.connect(':memory:')}
        db['customers-db'].executescript('CREATE TABLE entity(id INTEGER); INSERT INTO entity VALUES(1),(2),(3); CREATE TABLE record(id INTEGER,entity_id INTEGER,ended_at INTEGER); INSERT INTO record VALUES(11,1,20),(12,1,19),(21,2,18),(31,3,1);')
        target=db['customers-db' if same_source else 'accounts-db']
        target.executescript('CREATE TABLE fact(id INTEGER,record_id INTEGER,amount NUMERIC);')
        if not empty:target.executescript('INSERT INTO fact VALUES(1,11,4),(2,11,5),(3,31,999);')
        async def snapshot(domain):return OntologySnapshotRef(domain='commerce',version='1',content_hash='test',artifact_version='test'),ont
        w.snapshot_loader=snapshot
        class Analyst:
            async def analyse(self,*args):return build(ont,intent)
        w.analyst=Analyst();queries=[]
        async def execute(sql,params,sensitive,settings):
            queries.append((settings['data_source_id'],sql))
            sql=re.sub(r'%\(([A-Za-z_][A-Za-z0-9_]*)\)s',r':\1',sql)
            cur=db[settings['data_source_id']].execute(sqlglot.transpile(sql,read='postgres',write='sqlite')[0],params)
            rows=[list(r) for r in cur.fetchall()]
            return ExecutionResult(columns=[c[0] for c in cur.description],rows=rows,row_count=len(rows),duration_ms=1,summary={'masked_columns':[]})
        w.execute=execute
        run=await w.start(session.id,'select recent distinct subjects and check relationship membership',domain='commerce')
        assert run.status==RunStatus.COMPLETED,run.error
        expected=([[1],[2]] if not present else []) if empty else ([[1]] if present else [[2]])
        assert run.result.columns==['entity_id'] and run.result.rows==expected
        assert len(queries)==3 and queries[-1][1].endswith('LIMIT 1001')
        assert 'GROUP BY' in queries[-1][1]
        assert run.result.summary['membership']['source_rows']==3
        assert w.store.audit.verify(run.id)==(True,None)
    asyncio.run(scenario())


def test_membership_contract_survives_json_and_rejects_dropped_semantics():
    ont,intent=scenario_intent()
    plan=build(ont,intent).plan
    restored=QueryPlan.model_validate_json(plan.model_dump_json())
    assert restored==plan
    assert plan.fields==['entity.id'] and plan.source_steps[-1].membership.group_by==['entity.id']
    plan.source_steps[-1].membership=None
    with pytest.raises(ValueError,match='存在性'):validate_intent_filter_coverage(plan)


@pytest.mark.parametrize('mutation',['limit','source','fields','identity'])
def test_membership_rejects_invalid_or_partial_contract(mutation):
    ont,intent=scenario_intent();plan=build(ont,intent).plan;step=plan.source_steps[-1]
    if mutation=='limit':step.query.limit=1
    if mutation=='source':step.membership.source_step=step.name
    if mutation=='fields':step.membership.fields=['fact.amount']
    if mutation=='identity':step.membership.group_by=['record.ended_at']
    with pytest.raises(ValueError):validate_source_steps(plan,ont)


def test_empty_null_and_incomplete_target_sets_are_distinct():
    ont,intent=scenario_intent();step=build(ont,intent).plan.source_steps[-1]
    source=ExecutionResult(columns=['entity_id','record_id'],rows=[[1,11],[1,12],[2,None]],row_count=3,duration_ms=1)
    matched=ExecutionResult(columns=['fact_record_id'],rows=[[11],[None]],row_count=2,duration_ms=1)
    result=membership_result(step,source,matched,max_rows=1000,ontology=ont)
    assert result.rows==[[2]]
    matched.truncated=True
    with pytest.raises(ValueError,match='不完整'):membership_result(step,source,matched,max_rows=1000,ontology=ont)
    matched.truncated=False;matched.rows=[[999]];matched.row_count=1
    with pytest.raises(ValueError,match='精确匹配'):membership_result(step,source,matched,max_rows=1000,ontology=ont)
