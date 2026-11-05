import sqlite3

import pytest
import sqlglot
from pydantic import ValidationError

from app.planner import compile_plan
from semora_agent.constraints import explicit_filters
from semora_agent.roles import AnalystAgent
from semora_contracts import QueryFilter, QueryPlan
from services.api.tests.test_agent_runtime import PLAN
from services.api.tests.test_query_stages import ontology, plan, execute
from services.api.tests.test_workflow import fake_snapshot


@pytest.mark.parametrize('operator,expected', [('is_null', [None]), ('is_not_null', ['A'])])
def test_null_filters_compile_to_null_predicate_and_execute(operator, expected):
    p = QueryPlan(domain='commerce', data_source_id='commerce-db', intent='地区', metrics=[], fields=['region.name'], grain='region',
                  filters=[QueryFilter(field='region.name', operator=operator)])
    artifact = compile_plan(p, ontology())
    assert 'IS ' in artifact.physical_sql and 'NULL' in artifact.physical_sql
    assert artifact.parameters == {}
    with sqlite3.connect(':memory:') as db:
        db.executescript("CREATE TABLE regions(id INTEGER,name TEXT); INSERT INTO regions VALUES(1,NULL),(2,'A');")
        sql = sqlglot.transpile(artifact.physical_sql, read='postgres', write='sqlite')[0]
        assert [r[0] for r in db.execute(sql)] == expected


def test_null_filters_work_inside_stage_and_conditional_measure():
    p = plan()
    p.stages[0].filters = [QueryFilter(field='line.product', operator='is_not_null')]
    p.stages[0].measures[0].filters = [QueryFilter(field='line.price', operator='is_null')]
    artifact = compile_plan(p, ontology())
    rows = {r['region_name']: r for r in execute(artifact)}
    assert rows['A']['order_total'] == 200 and rows['A']['units'] == 0
    assert rows['B']['order_total'] == 50  # LEFT JOIN still preserves empty details.


def test_null_operand_validation_never_emits_equals_null():
    assert QueryFilter(field='x.y', operator='eq', value=None).operator == 'is_null'
    assert QueryFilter(field='x.y', operator='ne', value=None).operator == 'is_not_null'
    for payload in ({'operator': 'gt', 'value': None}, {'operator': 'eq'}, {'operator': 'is_null', 'value': 'NULL'}):
        with pytest.raises(ValidationError):
            QueryFilter(field='x.y', **payload)


def test_explicit_null_checks_do_not_turn_disjunction_or_negation_into_and():
    o = ontology()
    checks = explicit_filters(['sale.customer_id IS NULL', 'region.name不为空'], o)
    assert {(f.field, f.operator) for f in checks} == {('sale.customer_id', 'is_null'), ('region.name', 'is_not_null')}
    assert explicit_filters(['sale.customer_id IS NULL OR region.name IS NOT NULL'], o) == []
    assert explicit_filters(['不要使用 region.name IS NULL'], o) == []


def test_predicates_in_analysis_steps_must_be_executable():
    import asyncio
    _, o = asyncio.run(fake_snapshot('retail_credit'))
    proposal = {**PLAN, 'analysis_steps': ['outstanding_balance > 0', 'as_of_date IS NULL']}
    with pytest.raises(ValueError, match='未落实到 filters'):
        AnalystAgent()._build_outcome(proposal, '查询', [], o, {}, 'test')
    proposal['filters'] = [{'field': 'loan.outstanding_balance', 'operator': 'gt', 'value': 0},
                           {'field': 'loan.as_of_date', 'operator': 'is_null'}]
    outcome = AnalystAgent()._build_outcome(proposal, '查询', [], o, {}, 'test')
    assert outcome.plan.filters == outcome.requirement.filters
