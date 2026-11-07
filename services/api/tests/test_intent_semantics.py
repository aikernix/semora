"""P1 result regressions through the production intent/compiler contract."""
import asyncio
import copy
import json
import re
import sqlite3

import pytest
import sqlglot
from semora_agent import AnalystAgent, ReviewerAgent
from semora_contracts import ReviewDecision, ReviewFinding, RunStatus
from app.intent_planner import compile_intent
from app.planner import compile_plan
from app.query_stages import validate_query_shape
from app.review_loop import issue_ledger, validate_resolution


def catalog(optional=False, reverse=False):
    return {'domain':'generic','dataSourceId':'one','metrics':[],'objects':[
        {'name':'parent','table':'parent','primaryKey':'id','properties':[
            {'name':'id','column':'id','type':'integer'},{'name':'amount','column':'amount','type':'number'}]},
        {'name':'line','table':'line','primaryKey':'id','properties':[
            {'name':'id','column':'id','type':'integer'},{'name':'parent_id','column':'parent_id','type':'integer'},
            {'name':'quantity','column':'quantity','type':'number'}]}],
        'relations':[{'name':'items','from':'line' if reverse else 'parent','to':'parent' if reverse else 'line',
            'condition':'parent.id = line.parent_id','cardinality':'N:1' if reverse else '1:N','optional':optional}]}


def build(ont, *, grouped=False, function='sum', filters=None):
    value=compile_intent({'summary':'汇总已定义指标','subject':'parent','group_by':['parent.id'] if grouped else [],
        'filters':filters or [],'measures':[{'name':'total','function':'sum','field':'parent.amount'},
            {'name':'detail','function':function,'field':'line.id' if function=='count' else 'line.quantity'}]},ont,{})
    result=AnalystAgent()._build_outcome(value,'汇总',[],ont,{},'test')
    validate_query_shape(result.plan,ont)
    return result.plan


def execute(plan, ont, parents=((1,100),(2,200)), lines=((1,1,3),(2,1,4))):
    artifact=compile_plan(plan,ont)
    sql=re.sub(r'%\((\w+)\)s',r':\1',artifact.physical_sql)
    sql=sqlglot.transpile(sql,read='postgres',write='sqlite')[0]
    with sqlite3.connect(':memory:') as db:
        db.executescript('CREATE TABLE parent(id INTEGER PRIMARY KEY,amount NUMERIC); CREATE TABLE line(id INTEGER PRIMARY KEY,parent_id INTEGER,quantity NUMERIC);')
        db.executemany('INSERT INTO parent VALUES(?,?)',parents)
        db.executemany('INSERT INTO line VALUES(?,?,?)',lines)
        rows=db.execute(sql,artifact.parameters).fetchall()
    return sorted(rows), artifact.physical_sql


@pytest.mark.parametrize('reverse',[False,True])
@pytest.mark.parametrize('optional,expected',[(False,[(100,7)]),(True,[(300,7)])])
def test_automatic_stage_preserves_required_and_optional_matching(optional,expected,reverse):
    ont=catalog(optional,reverse)
    rows,sql=execute(build(ont),ont)
    assert rows==expected,sql


@pytest.mark.parametrize('optional',[False,True])
@pytest.mark.parametrize('reverse',[False,True])
def test_stage_local_filter_keeps_where_population(optional,reverse):
    ont=catalog(optional,reverse)
    filters=[{'field':'line.quantity','operator':'gt','value':3}]
    plan=build(ont,filters=filters)
    rows,sql=execute(plan,ont)
    assert rows==[(100,4)],sql
    assert plan.stages[0].join_type=='inner'


@pytest.mark.parametrize('reverse',[False,True])
def test_outer_count_missing_group_is_zero_but_sum_stays_null(reverse):
    ont=catalog(True,reverse)
    rows,sql=execute(build(ont,grouped=True,function='count'),ont)
    assert rows==[(1,100,2),(2,200,0)],sql
    rows,_=execute(build(ont,grouped=True),ont)
    assert rows==[(1,100,7),(2,200,None)]


@pytest.mark.parametrize('parents,lines,expected',[
    ([],[],[(None,0)]),
    ([(1,100),(2,200)],[],[(300,0)]),
    ([(1,100)],[(1,1,None),(2,1,None)],[(100,2)]),
])
def test_global_count_empty_and_matches(parents,lines,expected):
    ont=catalog(True)
    rows,sql=execute(build(ont,function='count'),ont,parents,lines)
    assert rows==expected,sql


def test_count_nullable_original_field_is_zero():
    ont=catalog(True)
    plan=build(ont,grouped=True,function='count')
    # Use the same public intent pipeline for COUNT(nullable attribute).
    intent=plan.query_intent.model_dump(mode='json')
    intent['measures'][1]['field']='line.quantity'
    value=compile_intent(intent,ont,{})
    plan=AnalystAgent()._build_outcome(value,'计数',[],ont,{},'test').plan
    validate_query_shape(plan,ont)
    rows,sql=execute(plan,ont,lines=[(1,1,None),(2,1,None)])
    assert rows==[(1,100,0),(2,200,0)],sql


def test_business_review_path_is_valid_in_actual_plan():
    ont=catalog()
    plan=build(ont)
    finding=ReviewFinding(kind='repair',paths=['/query_intent/filters'],message='遗漏筛选',evidence='用户确认',acceptance='补齐阈值')
    validate_resolution(ReviewDecision(decision='revise',findings=[finding],rationale='遗漏'),[],plan)


def test_default_protocol_reviewer_business_fix_resolution_and_execution(monkeypatch):
    async def scenario():
        from app import prepared_context
        from services.api.tests.test_source_execution import harness,ontology
        from services.api.tests.test_agent_runtime import NativeModel
        monkeypatch.delenv('AGENT_QUERY_INTENT',raising=False)
        monkeypatch.setenv('AGENT_BOUNDED_CONTEXT','true')
        w,session,calls=harness(monkeypatch)
        intent={'summary':'有效对象的关联金额','subject':'client','fields':['balances.amount']}
        corrected={**intent,'filters':[{'field':'client.status','operator':'eq','value':'active'}]}
        finding={'kind':'repair','paths':['/query_intent/filters'],'message':'遗漏已确认状态','evidence':'需求要求有效对象','acceptance':'补齐active状态筛选'}
        rejected=ReviewDecision(decision='revise',findings=[ReviewFinding(**finding)],rationale='补齐筛选')
        pending=issue_ledger(rejected,[])
        analyst=NativeModel([('validate_intent',{'intent':intent}),('validate_intent',{'intent':corrected})])
        reviewer=NativeModel([
            ('AgentSubmission',{'payload':rejected.model_dump(exclude={'resolutions','phase','reviewer_mode'})}),
            ('AgentSubmission',{'payload':{'decision':'approved','rationale':'状态条件已落地',
                'resolutions':[{'finding_id':pending[0]['id'],'status':'resolved','rationale':'已补齐active状态'}]}}),
            ('AgentSubmission',{'payload':{'decision':'approved','rationale':'SQL与确认需求一致'}}),
            ('AgentSubmission',{'payload':{'decision':'approved','rationale':'结果正确'}}),
        ])
        w.analyst=AnalystAgent(analyst);w.reviewer=ReviewerAgent(reviewer)
        async def prepare(workflow,run,question,ont,scope):
            return {'packet':prepared_context.context_packet(workflow,run,ont,['client','balances']),'supplements':0}
        monkeypatch.setattr(prepared_context,'prepare_context',prepare)
        r=await w.start(session.id,'查有效对象的关联金额',domain='commerce')
        assert r.status==RunStatus.COMPLETED,r.error
        assert r.result.rows==[[10],[20]]
        assert len(analyst.messages)==2 and len(reviewer.messages)==4
        assert len(calls)==2
        reviews=[e.data['review'] for e in r.events if e.event=='plan.preflight.completed']
        assert [x['decision'] for x in reviews]==['revise','approved']
        assert reviews[1]['resolutions'][0]['finding_id']==pending[0]['id']
        assert r.plan.query_intent.filters[0].value=='active'
        assert w.store.audit.verify(r.id)==(True,None)
    asyncio.run(scenario())


def test_two_many_sides_with_filters_preserve_population_and_do_not_multiply():
    ont=catalog(True)
    extra=copy.deepcopy(ont['objects'][1]);extra.update(name='detail2',table='detail2')
    ont['objects'].append(extra)
    ont['relations'].append({'name':'other_items','from':'detail2','to':'parent',
        'condition':'parent.id = detail2.parent_id','cardinality':'N:1','optional':False})
    intent={'summary':'多个明细范围汇总','subject':'parent','group_by':['parent.id'],
        'filters':[{'field':'line.quantity','operator':'gt','value':3},{'field':'detail2.quantity','operator':'gt','value':10}],
        'measures':[{'name':'total','function':'sum','field':'parent.amount'},
                    {'name':'a','function':'sum','field':'line.quantity'},
                    {'name':'b','function':'sum','field':'detail2.quantity'}]}
    value=compile_intent(intent,ont,{})
    plan=AnalystAgent()._build_outcome(value,'汇总',[],ont,{},'test').plan
    validate_query_shape(plan,ont)
    artifact=compile_plan(plan,ont)
    sql=sqlglot.transpile(re.sub(r'%\((\w+)\)s',r':\1',artifact.physical_sql),read='postgres',write='sqlite')[0]
    with sqlite3.connect(':memory:') as db:
        db.executescript('CREATE TABLE parent(id INTEGER PRIMARY KEY,amount NUMERIC); CREATE TABLE line(id INTEGER,parent_id INTEGER,quantity NUMERIC); CREATE TABLE detail2(id INTEGER,parent_id INTEGER,quantity NUMERIC); INSERT INTO parent VALUES(1,100),(2,200); INSERT INTO line VALUES(1,1,3),(2,1,4),(3,2,8); INSERT INTO detail2 VALUES(1,1,9),(2,1,11);')
        assert db.execute(sql,artifact.parameters).fetchall()==[(1,100,4,11)]
    assert len(plan.stages)==2 and all(s.join_type=='inner' for s in plan.stages)


def test_average_null_is_not_coalesced_to_zero():
    ont=catalog(True)
    rows,_=execute(build(ont,grouped=True,function='avg'),ont,lines=[(1,1,None),(2,1,None)])
    assert rows==[(1,100,None),(2,200,None)]


@pytest.mark.parametrize('path',[
    '/query_intent/summary','/query_intent/assumptions','/query_intent/missing',
    '/query_intent/filters/-1','/source_steps/-1/query/fields',
])
def test_review_paths_cannot_target_descriptions_or_negative_indexes(path):
    plan=build(catalog(),filters=[{'field':'parent.amount','operator':'gt','value':0}])
    review=ReviewDecision(decision='revise',rationale='范围校验',findings=[ReviewFinding(
        kind='repair',paths=[path],message='修订',evidence='确认需求',acceptance='执行口径一致')])
    with pytest.raises(ValueError):
        validate_resolution(review,[],plan)


def test_review_business_and_artifact_targets_are_separate():
    from semora_agent.review_policy import ReviewIssue,ReviewTarget,intent_repair_route
    plan=build(catalog())
    business=ReviewFinding(kind='repair',paths=['/query_intent/filters'],message='业务修订',evidence='用户口径',acceptance='补齐条件')
    artifact=ReviewFinding(kind='repair',paths=['/filters'],message='编译修订',evidence='SQL证据',acceptance='修复编译')
    for finding,target in [(business,ReviewTarget.BUSINESS),(artifact,ReviewTarget.ARTIFACT)]:
        assert ReviewIssue.from_finding(finding).target==target
        validate_resolution(ReviewDecision(decision='revise',rationale='校验',findings=[finding]),[],plan)
    mixed=business.model_copy(update={'paths':['/query_intent/filters','/filters']})
    with pytest.raises(ValueError,match='mix'):
        validate_resolution(ReviewDecision(decision='revise',rationale='校验',findings=[mixed]),[],plan)
    assert intent_repair_route(ReviewDecision(decision='revise',rationale='业务',findings=[business]))=='business'
    assert intent_repair_route(ReviewDecision(decision='revise',rationale='技术',findings=[artifact]))=='technical'


def test_role_coordinator_has_no_reverse_import_of_compatibility_entrypoint():
    import ast
    from pathlib import Path
    import app.intent_analysis as coordinator
    import semora_agent.review_policy as policy
    coordinator_tree=ast.parse(Path(coordinator.__file__).read_text(encoding='utf-8-sig'))
    assert not any(isinstance(n,ast.ImportFrom) and (n.module or '').endswith('agentic') for n in ast.walk(coordinator_tree))
    policy_tree=ast.parse(Path(policy.__file__).read_text(encoding='utf-8-sig'))
    roots={n.module.split('.')[0] for n in ast.walk(policy_tree) if isinstance(n,ast.ImportFrom) and n.module}
    roots.update(a.name.split('.')[0] for n in ast.walk(policy_tree) if isinstance(n,ast.Import) for a in n.names)
    assert roots <= {'copy','dataclasses','enum','hashlib','json','re','semora_contracts'}
    # Every Agent package file must remain free of API application imports.
    for source in Path(policy.__file__).parent.glob('*.py'):
        tree=ast.parse(source.read_text(encoding='utf-8-sig'))
        for node in ast.walk(tree):
            if isinstance(node,ast.ImportFrom):
                assert not (node.module or '').startswith(('app.','services.api.')),source.name


def test_default_protocol_stops_unchanged_business_fix_before_second_review(monkeypatch):
    async def scenario():
        from app import prepared_context
        from services.api.tests.test_source_execution import harness
        from services.api.tests.test_agent_runtime import NativeModel
        monkeypatch.delenv('AGENT_QUERY_INTENT',raising=False)
        monkeypatch.setenv('AGENT_BOUNDED_CONTEXT','true')
        w,session,calls=harness(monkeypatch)
        intent={'summary':'有效对象的关联金额','subject':'client','fields':['balances.amount']}
        analyst=NativeModel([('validate_intent',{'intent':intent}),('validate_intent',{'intent':intent}),('validate_intent',{'intent':intent})])
        reviewer=NativeModel([('AgentSubmission',{'payload':{'decision':'revise','rationale':'遗漏已确认状态',
            'findings':[{'kind':'repair','paths':['/query_intent/filters'],'message':'遗漏筛选','evidence':'用户确认', 'acceptance':'状态active'}]}})])
        w.analyst=AnalystAgent(analyst);w.reviewer=ReviewerAgent(reviewer)
        async def prepare(workflow,run,question,ont,scope):
            return {'packet':prepared_context.context_packet(workflow,run,ont,['client','balances']),'supplements':0}
        monkeypatch.setattr(prepared_context,'prepare_context',prepare)
        r=await w.start(session.id,'查有效对象',domain='commerce')
        assert r.status==RunStatus.FAILED and '连续修订' in r.error
        assert len(analyst.messages)==3
        assert len(reviewer.messages)==1 and calls==[]
        assert r.result is None
    asyncio.run(scenario())


def test_capability_gap_is_delivered_without_another_model_call(monkeypatch):
    async def scenario():
        from app import prepared_context
        from services.api.tests.test_source_execution import harness
        from services.api.tests.test_agent_runtime import NativeModel
        monkeypatch.delenv('AGENT_QUERY_INTENT',raising=False)
        monkeypatch.setenv('AGENT_BOUNDED_CONTEXT','true')
        w,session,calls=harness(monkeypatch)
        intent={'summary':'跨源联合汇总','subject':'balances','measures':[
            {'name':'amount','function':'sum','field':'balances.amount'},
            {'name':'other','function':'sum','field':'client.row_id'}]}
        model=NativeModel([('validate_intent',{'intent':intent})])
        w.analyst=AnalystAgent(model)
        async def prepare(workflow,run,question,ont,scope):
            return {'packet':prepared_context.context_packet(workflow,run,ont,['client','balances']),'supplements':0}
        monkeypatch.setattr(prepared_context,'prepare_context',prepare)
        result=await w.start(session.id,'跨源联合汇总',domain='commerce')
        assert result.status==RunStatus.FAILED and '跨源指标联合汇总' in result.error
        assert '工具调用格式无效' not in result.error
        assert len(model.messages)==1 and calls==[]
    asyncio.run(scenario())
