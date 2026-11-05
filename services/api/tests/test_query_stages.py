import re
import sqlite3

import pytest
import sqlglot

from app.planner import PlanningError, compile_plan
from app.query_stages import validate_query_shape
from semora_contracts import QueryPlan, QueryFilter


def ontology():
    return {
        'domain': 'commerce', 'dataSourceId': 'commerce-db',
        'objects': [
            {'name': 'region', 'table': 'regions', 'primaryKey': 'id', 'properties': [{'name': 'name', 'column': 'name'}]},
            {'name': 'sale', 'table': 'orders', 'primaryKey': 'id', 'properties': [{'name': 'amount', 'column': 'amount'}, {'name': 'customer_id', 'column': 'customer_id'}]},
            {'name': 'line', 'table': 'items', 'primaryKey': 'id', 'properties': [{'name': p, 'column': p} for p in ('quantity', 'price', 'product')]},
            {'name': 'refund', 'table': 'refunds', 'primaryKey': 'id', 'properties': [{'name': 'amount', 'column': 'amount'}]},
        ],
        'relations': [
            {'name': 'sale_region', 'from': 'sale', 'to': 'region', 'condition': 'sale.region_id = region.id', 'cardinality': 'N:1'},
            {'name': 'sale_line', 'from': 'sale', 'to': 'line', 'condition': 'sale.id = line.order_id', 'cardinality': '1:N'},
            {'name': 'sale_refund', 'from': 'sale', 'to': 'refund', 'condition': 'sale.id = refund.order_id', 'cardinality': '1:N'},
        ],
        'metrics': [{'name': 'mean_price', 'expression': 'AVG(line.price)'}],
    }


def plan():
    return QueryPlan.model_validate({
        'version': '2.0', 'domain': 'commerce', 'data_source_id': 'commerce-db', 'intent': '按地区统计订单与明细',
        'grain': 'region', 'dimensions': ['region.name'],
        'metrics': ['order_total', 'units', 'refund_total', 'customers'],
        'calculations': [
            {'name': 'order_total', 'function': 'sum', 'field': 'sale.amount'},
            {'name': 'units', 'function': 'sum', 'field': 'lines_by_order.units'},
            {'name': 'refund_total', 'function': 'sum', 'field': 'refunds_by_order.amount'},
            {'name': 'customers', 'function': 'count_distinct', 'field': 'sale.customer_id'},
        ],
        'relationship_path': ['sale_region', 'sale_line', 'sale_refund'],
        'stages': [
            {'name': 'lines_by_order', 'source': 'line', 'group_by': ['line.order_id'], 'measures': [{'name': 'units', 'function': 'sum', 'field': 'line.quantity'}]},
            {'name': 'refunds_by_order', 'source': 'refund', 'group_by': ['refund.order_id'], 'measures': [{'name': 'amount', 'function': 'sum', 'field': 'refund.amount'}]},
        ],
    })


def execute(artifact, empty=False):
    with sqlite3.connect(':memory:') as db:
        db.executescript('''
          CREATE TABLE regions(id INTEGER PRIMARY KEY,name TEXT);
          CREATE TABLE orders(id INTEGER PRIMARY KEY,region_id INTEGER,amount NUMERIC,customer_id INTEGER);
          CREATE TABLE items(id INTEGER PRIMARY KEY,order_id INTEGER,quantity NUMERIC,price NUMERIC,product TEXT);
          CREATE TABLE refunds(id INTEGER PRIMARY KEY,order_id INTEGER,amount NUMERIC);
        ''')
        if not empty:
            db.executescript('''
              INSERT INTO regions VALUES(1,'A'),(2,'B');
              INSERT INTO orders VALUES(1,1,100,1),(2,1,100,1),(3,2,50,2);
              INSERT INTO items VALUES(1,1,1,0,'p'),(2,1,1,10,'p'),(3,1,1,20,'p'),(4,2,4,100,'q');
              INSERT INTO refunds VALUES(1,1,10),(2,1,15),(3,2,5);
            ''')
        sql = re.sub(r'%\(([A-Za-z_][A-Za-z0-9_]*)\)s', r':\1', artifact.physical_sql)
        sql = sqlglot.transpile(sql, read='postgres', write='sqlite')[0]
        cursor = db.execute(sql, artifact.parameters)
        return [dict(zip([d[0] for d in cursor.description], row)) for row in cursor.fetchall()]


def test_two_independent_details_are_preaggregated_in_one_statement():
    artifact = compile_plan(plan(), ontology())
    assert len(sqlglot.parse(re.sub(r'%\((\w+)\)s', r':\1', artifact.physical_sql), read='postgres')) == 1
    assert artifact.compilation['execution'] == 'single_statement'
    rows = {row['region_name']: row for row in execute(artifact)}
    assert rows['A']['order_total'] == 200  # Equal amounts are separate orders.
    assert rows['A']['units'] == 7
    assert rows['A']['refund_total'] == 30
    assert rows['A']['customers'] == 1
    assert rows['B']['order_total'] == 50  # No details: preserve the parent.
    assert rows['B']['units'] is None and rows['B']['refund_total'] is None


def test_raw_many_side_join_cannot_repeat_parent_amount():
    p = plan()
    p.stages = []
    p.metrics, p.calculations = p.metrics[:1], p.calculations[:1]
    with pytest.raises(PlanningError, match='关联放大风险'):
        compile_plan(p, ontology())


def test_extra_grouping_key_that_does_not_collapse_the_many_side_is_rejected():
    p = plan()
    p.stages[0].group_by.append('line.product')
    with pytest.raises(PlanningError, match='分组键不能证明关联唯一'):
        compile_plan(p, ontology())


def test_detail_fields_cannot_silently_change_final_aggregation_grain():
    p = plan()
    p.fields = ['sale.customer_id']
    with pytest.raises(PlanningError, match='汇总粒度冲突'):
        compile_plan(p, ontology())


def test_global_average_uses_sum_and_count_instead_of_average_of_averages():
    p = plan()
    p.metrics, p.calculations = ['mean_price'], []
    p.relationship_path = ['sale_region', 'sale_line']
    p.stages = p.stages[:1]
    p.stages[0].measures[0].name = 'average_price'
    p.stages[0].measures[0].function = 'avg'
    p.stages[0].measures[0].field = 'line.price'
    artifact = compile_plan(p, ontology())
    rows = {row['region_name']: row for row in execute(artifact)}
    assert rows['A']['mean_price'] == 32.5  # (0+10+20+100)/4, not (10+100)/2.


def test_stage_filters_are_bound_parameters_not_sql_fragments():
    p = plan()
    secret = "p' OR 1=1 --"
    p.stages[0].filters = [QueryFilter(field='line.product', operator='eq', value=secret)]
    artifact = compile_plan(p, ontology())
    assert secret not in artifact.physical_sql and secret in artifact.parameters.values()
    rows = {row['region_name']: row for row in execute(artifact)}
    assert rows['A']['units'] is None and rows['A']['order_total'] == 200


def test_empty_sources_are_valid_and_return_no_rows():
    artifact = compile_plan(plan(), ontology())
    assert execute(artifact, empty=True) == []


def test_unsupported_global_distinct_rollup_is_not_approximated():
    p = plan()
    p.metrics, p.calculations = ['distinct_products'], []
    p.relationship_path = ['sale_region', 'sale_line']
    p.stages = p.stages[:1]
    model = ontology()
    model['metrics'].append({'name': 'distinct_products', 'expression': 'COUNT(DISTINCT line.product)'})
    with pytest.raises(PlanningError, match='已被阶段预聚合'):
        compile_plan(p, model)


def test_unknown_cardinality_cannot_be_assumed_safe():
    p = plan()
    model = ontology()
    model['relations'][0].pop('cardinality')
    model['objects'][0].pop('primaryKey')
    with pytest.raises(ValueError, match='关联放大风险'):
        validate_query_shape(p, model)


def test_local_distinct_counts_cannot_be_summed_as_global_distinct():
    p = plan()
    p.stages[0].measures[0].function = 'count_distinct'
    with pytest.raises(PlanningError, match='不能相加阶段COUNT DISTINCT'):
        compile_plan(p, ontology())


def test_stage_measure_keeps_source_sensitivity():
    p = plan()
    model = ontology()
    model['objects'][2]['properties'][0]['sensitive'] = True
    artifact = compile_plan(p, model)
    assert 'units' in artifact.sensitive_columns
