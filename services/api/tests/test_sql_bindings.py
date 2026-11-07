import re

import pytest
import sqlglot
from sqlglot import exp

from semora_query_core.bindings import render_bound_expression
from semora_query_core.sql import compile_plan,adapt_mysql_artifact
from services.api.tests.test_scoped_aggregates import build,fixture,execute


def test_ast_binding_preserves_literals_casts_and_repeated_parameters():
    source="SUM(CASE WHEN v > :p THEN v ELSE 0 END) > :p AND '12:30 :p'::text = '12:30 :p'"
    tree=sqlglot.parse_one(source,read='postgres')
    parameters={'p':"0 OR 1=1 -- '"}
    result=render_bound_expression(tree,parameters)
    assert result.count('%(p)s')==2 and result.count("'12:30 :p'")==2
    assert parameters['p'] not in result and tree.find(exp.Placeholder)
    assert render_bound_expression(tree,parameters,paramstyle='named').count(':p')==4
    with pytest.raises(ValueError,match='未绑定'):
        render_bound_expression(tree,{})
    with pytest.raises(ValueError,match='未适配'):
        render_bound_expression(tree,parameters,paramstyle='unknown')


@pytest.mark.parametrize('mysql',[False,True])
@pytest.mark.parametrize('operator,value',[
    ('gt',0),('between',[0,20]),('in',[4,6,10]),('is_not_null',None),
])
def test_having_bindings_use_current_driver_contract(mysql,operator,value):
    ontology,intent=fixture()
    intent['having']=[dict(field='due',operator=operator,value=value)]
    _,outcome=build(intent)
    artifact=compile_plan(outcome.plan,ontology)
    if mysql:artifact=adapt_mysql_artifact(artifact)
    assert not re.search(r'(?<!:):having_\w+',artifact.physical_sql)
    names=re.findall(r'%\(([A-Za-z_]\w*)\)s',artifact.physical_sql)
    assert set(names)==set(artifact.parameters)
    assert execute(artifact)


@pytest.mark.parametrize('mysql',[False,True])
def test_conditional_metric_and_multiple_having_conditions_bind_together(mysql):
    ontology,_=fixture()
    intent=dict(summary='汇总指定范围数值',subject='movement',measures=[dict(name='value',function='sum',field='movement.amount',
        filters=[dict(field='movement.occurred',operator='gte',value=10)],null_value=0)],
        having=[dict(field='value',operator='gt',value=0),dict(field='value',operator='lt',value=200)])
    _,outcome=build(intent)
    artifact=compile_plan(outcome.plan,ontology)
    if mysql:artifact=adapt_mysql_artifact(artifact)
    names=re.findall(r'%\(([A-Za-z_]\w*)\)s',artifact.physical_sql)
    assert set(names)==set(artifact.parameters)
    assert not re.search(r'(?<!:):(?:having_|m_)',artifact.physical_sql)
    assert execute(artifact)==[[142]]
