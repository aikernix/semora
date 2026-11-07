"""Business intent compilation must preserve meaning before optimizing SQL."""
import asyncio
import copy
import json

import pytest
from pydantic import ValidationError
from semora_contracts import QueryIntent, QueryRun
from semora_agent import AnalystAgent
from app.intent_planner import compile_intent, IntentPlanningError, semantic_context, enabled
from app.query_stages import validate_query_shape
from app.planner import compile_plan
from services.api.tests.test_source_execution import ontology, harness


def outcome(intent, ont=None):
    ont=ont or ontology()
    value=compile_intent(intent,ont,{})
    result=AnalystAgent()._build_outcome(value,'需求',[],ont,{},'test')
    validate_query_shape(result.plan,ont)
    return result


def simple():
    return {'summary':'查询账户金额','subject':'balances','fields':['balances.amount']}


def test_default_enabled_and_strict_business_only_schema(monkeypatch):
    monkeypatch.delenv('AGENT_QUERY_INTENT',raising=False)
    assert enabled()
    for name,value in [('source_steps',[]),('stages',[]),('data_source_id','invented'),('relationship_path',[])]:
        with pytest.raises(ValidationError):
            QueryIntent.model_validate({**simple(),name:value})


def test_single_source_selected_from_object_not_domain_default():
    result=outcome(simple())
    assert result.plan.data_source_id=='accounts-db'
    assert result.plan.query_intent.subject=='balances'
    assert not result.plan.source_steps


def test_cross_source_keys_and_filters_are_generated_without_model_ids():
    intent={**simple(),'subject':'client','filters':[{'field':'client.status','operator':'eq','value':'active'}]}
    result=outcome(intent)
    assert len(result.plan.source_steps)==2
    first,last=result.plan.source_steps
    assert first.query.filters[0].field=='client.status'
    assert first.query.fields==['client.customer_id']
    assert last.inputs[0].pairs[0].target_field=='balances.customer_id'
    assert result.requirement.query_intent==result.plan.query_intent


def test_compound_relationship_keys_are_not_independent_in_predicates():
    ont=ontology()
    for obj in ont['objects']:
        obj['properties'].append({'name':'tenant','column':'tenant','type':'string'})
    ont['relations'][0]['condition']+=' AND client.tenant = balances.tenant'
    result=outcome({**simple(),'subject':'client'},ont)
    pairs=result.plan.source_steps[-1].inputs[0].pairs
    assert [(p.source_field,p.target_field) for p in pairs]==[
        ('client.customer_id','balances.customer_id'),('client.tenant','balances.tenant')]


def test_multiple_business_roles_require_clarification_instead_of_shortest_guess():
    ont=ontology()
    ont['relations'].append({**ont['relations'][0],'name':'alternate_role'})
    with pytest.raises(IntentPlanningError) as error:
        outcome({**simple(),'subject':'client'},ont)
    assert error.value.kind=='ambiguity' and len(error.value.options)==2
    result=outcome({**simple(),'subject':'client','relationship_roles':['customer_accounts']},ont)
    assert result.plan.source_steps[-1].inputs[0].relation=='customer_accounts'


def test_same_source_top_records_then_summary_is_strict_identity_semi_join():
    ont=ontology()
    intent={'summary':'取最后一条记录并统计金额','subject':'client',
            'measures':[{'name':'cnt','function':'count','field':'client.row_id'}],
            'selection':{'object':'client','unit':'records','time_field':'client.row_id','limit':1}}
    result=outcome(intent,ont)
    assert result.plan.execution_mode=='sequential_queries'
    assert result.plan.source_steps[0].query.limit==1
    assert result.plan.source_steps[-1].inputs[0].kind=='identity'
    bad=result.plan.model_copy(deep=True)
    bad.source_steps[-1].inputs[0].key_pairs[0].target_field='client.customer_id'
    from semora_agent.multisource import validate_source_steps
    with pytest.raises(ValueError,match='主键'):
        validate_source_steps(bad,ont)


def test_selection_and_display_orders_are_independent_and_scope_not_copied():
    ont=ontology()
    intent={'summary':'取最后一个客户，再列出全部关联金额','subject':'client','fields':['balances.amount'],
        'selection':{'object':'client','unit':'entities','time_field':'client.row_id','limit':1,
                     'filters':[{'field':'client.status','operator':'eq','value':'active'}]},
        'record_scope':'all_related_records','order_by':[{'field':'balances.amount','direction':'desc'}]}
    result=outcome(intent,ont)
    assert result.plan.source_steps[0].query.limit==1
    assert result.plan.source_steps[-1].query.filters==[]
    assert result.plan.source_steps[-1].query.order_by[0].field=='balances.amount'


def test_fanout_generates_preaggregation_instead_of_sum_distinct():
    ont={'domain':'generic','dataSourceId':'one','metrics':[],'objects':[
        {'name':'parent','table':'parent','primaryKey':'id','properties':[{'name':'id','column':'id','type':'integer'},{'name':'amount','column':'amount','type':'number'}]},
        {'name':'line','table':'line','primaryKey':'id','properties':[{'name':'id','column':'id','type':'integer'},{'name':'parent_id','column':'parent_id','type':'integer'},{'name':'quantity','column':'quantity','type':'number'}]}],
        'relations':[{'name':'items','from':'parent','to':'line','condition':'parent.id = line.parent_id','cardinality':'1:N'}]}
    result=outcome({'summary':'汇总金额与数量','subject':'parent','group_by':['parent.id'],
        'measures':[{'name':'total','function':'sum','field':'parent.amount'},{'name':'quantity','function':'sum','field':'line.quantity'}]},ont)
    assert len(result.plan.stages)==1
    assert result.plan.stages[0].group_by==['line.parent_id']
    sql=compile_plan(result.plan,ont).physical_sql
    assert 'SUM(DISTINCT' not in sql


def test_semantic_context_strips_physical_plans_but_retains_prior_intent():
    value={'prepared_context':{'objects':[{'object':{'name':'entity','table':'secret','primaryKey':'secret','properties':[{'name':'id','column':'secret'}]},'field_refs':['entity.id']}],
        'source_layout':[{'data_source_id':'secret'}],'transfer_templates':[]},
        'previous_confirmed_requirement':{'query_intent':simple(),'source_steps':[{'query':{'fields':['secret']}}],'confirmed_facts':[{'value':'yes'}]}}
    projected=semantic_context(value)
    assert 'secret' not in json.dumps(projected)
    assert projected['previous_confirmed_requirement']['query_intent']==simple()
    assert projected['previous_confirmed_requirement']['confirmed_facts']==[{'value':'yes'}]


def test_intent_tools_compile_and_canonical_delivery_without_plan_authoring(monkeypatch):
    async def run():
        monkeypatch.setenv('AGENT_QUERY_INTENT','true')
        from app.agentic import AgentTools
        w,session,_=harness(monkeypatch)
        w.analyst=AnalystAgent()
        r=QueryRun(session_id=session.id,domain='commerce',question='查账户金额')
        t=AgentTools(w,r,ontology(),{})
        t.start_phase('业务意图分析')
        assert 'validate_plan' not in {x.name for x in t.tools()}
        tool=next(x for x in t.tools() if x.name=='validate_intent')
        receipt=json.loads(await tool.ainvoke({'intent':simple()}))
        assert receipt.get('valid'),receipt
        value=t.resolve_submission({'candidate_id':receipt['candidate_id']})
        assert value['query_intent']['subject']=='balances'
    asyncio.run(run())



def test_intent_runtime_executes_and_preserves_confirmed_intent(monkeypatch):
    async def scenario():
        from app import agentic, prepared_context
        from app.prepared_context import InformationAssessment
        from services.api.tests.test_agent_runtime import NativeModel
        from semora_contracts import RunStatus
        monkeypatch.setenv('AGENT_QUERY_INTENT','true')
        monkeypatch.setenv('AGENT_BOUNDED_CONTEXT','true')
        w,session,calls=harness(monkeypatch)
        intent={**simple(),'subject':'client','filters':[{'field':'client.status','operator':'eq','value':'active'}]}
        model=NativeModel([('validate_intent',{'intent':intent})])
        w.analyst=AnalystAgent(model)
        async def prepare(workflow,run,question,ont,scope):
            return {'packet':prepared_context.context_packet(workflow,run,ont,['client','balances']),'supplements':0}
        async def assess(*args):
            return InformationAssessment(status='sufficient',reason='明确的筛选与输出')
        monkeypatch.setattr(prepared_context,'prepare_context',prepare)
        monkeypatch.setattr(prepared_context,'assess_information',assess)
        result=await w.start(session.id,'查询有效对象的关联金额',domain='commerce')
        assert result.status==RunStatus.COMPLETED,result.error
        assert [c[0] for c in calls]==['customers-db','accounts-db']
        assert result.result.rows==[[10],[20]]
        assert result.requirement.query_intent==result.plan.query_intent
        assert len(model.messages)==1
        user_context=json.loads(next(m['content'] for m in model.messages[0] if m['role']=='user'))
        assert 'source_layout' not in user_context['prepared_context']
        assert 'stage_join_contracts' not in json.dumps(user_context)
        assert w.store.audit.verify(result.id)==(True,None)
    asyncio.run(scenario())


def test_same_source_selection_executes_real_sql_and_aggregate(monkeypatch):
    async def scenario():
        from app import prepared_context
        from app.prepared_context import InformationAssessment
        from services.api.tests.test_agent_runtime import NativeModel
        from semora_contracts import RunStatus
        monkeypatch.setenv('AGENT_QUERY_INTENT','true')
        w,session,calls=harness(monkeypatch)
        intent={'summary':'取最后一条记录计数','subject':'client',
            'measures':[{'name':'cnt','function':'count','field':'client.row_id'}],
            'selection':{'object':'client','unit':'records','time_field':'client.row_id','limit':1}}
        w.analyst=AnalystAgent(NativeModel([('validate_intent',{'intent':intent})]))
        async def prepare(workflow,run,question,ont,scope):
            return {'packet':prepared_context.context_packet(workflow,run,ont,['client']),'supplements':0}
        async def assess(*args):
            return InformationAssessment(status='sufficient',reason='明确记录选取')
        monkeypatch.setattr(prepared_context,'prepare_context',prepare)
        monkeypatch.setattr(prepared_context,'assess_information',assess)
        result=await w.start(session.id,'取最后一条记录计数',domain='commerce')
        assert result.status==RunStatus.COMPLETED,result.error
        assert result.result.rows==[[1]]
        assert [c[0] for c in calls]==['customers-db','customers-db']
        assert result.requirement.execution_mode=='sequential_queries'
    asyncio.run(scenario())


def test_record_selection_with_foreign_pk_grouping_preserves_requested_grain():
    ont=ontology()
    intent={'summary':'选取对象后汇总各对象关联金额','subject':'client',
        'group_by':['client.row_id'],
        'measures':[{'name':'total','function':'sum','field':'balances.amount'}],
        'selection':{'object':'client','unit':'records','time_field':'client.row_id','limit':2}}
    # This relationship joins a non-PK; reducing on that key would merge
    # different selected records and must not masquerade as PK grouping.
    with pytest.raises(IntentPlanningError,match='唯一标识'):
        outcome(intent,ont)
    ont['relations'][0]['condition']='client.row_id = balances.owner_id'
    ont['objects'][1]['properties'].append({'name':'owner_id','column':'owner_id','type':'integer'})
    result=outcome(intent,ont)
    assert result.plan.source_steps[-1].query.dimensions==['balances.owner_id']
    assert result.plan.source_steps[-1].inputs[0].include_source_fields==['client.row_id']


def test_legacy_history_retains_step_filters_and_selection_without_execution_wiring():
    old={'source_steps':[{'name':'s1','data_source_id':'secret','query':{'grain':'entity','fields':['entity.id'],
        'filters':[{'field':'entity.state','operator':'eq','value':'ready'}],
        'order_by':[{'field':'entity.id','direction':'desc'}],'limit':10}}]}
    context=semantic_context(old)
    query=context['previous_business_operations'][0]
    assert query['filters'][0]['value']=='ready' and query['limit']==10
    assert 'source_steps' not in context and 'secret' not in json.dumps(context)


def test_selection_time_annotation_does_not_restrict_related_history_or_trigger_replanning():
    ont=ontology()
    intent={'summary':'选出对象后查全部关联记录','subject':'client','fields':['balances.amount'],
        'selection':{'object':'client','unit':'records','time_field':'client.row_id','limit':1,
                     'filters':[{'field':'client.row_id','operator':'gt','value':1}]},
        'record_scope':'selected_records','time_range':'昨日选取时段'}
    result=outcome(intent,ont)
    from semora_agent.multisource import source_ontology
    for step in result.plan.source_steps:
        compile_plan(__import__('semora_contracts').QueryPlan.model_validate(step.query.model_dump(mode='json')),source_ontology(ont,step.data_source_id))
    assert result.plan.source_steps[-1].query.time_range is None
    assert result.plan.source_steps[-1].query.filters==[]
    assert result.requirement.time_semantics=='昨日选取时段'


def test_reviewer_receives_executor_enrichment_proof_and_repairs_unlocated_rejection(monkeypatch):
    async def scenario():
        from app import agentic
        from semora_agent import ReviewerAgent
        from services.api.tests.test_agent_runtime import NativeModel
        w,session,_=harness(monkeypatch)
        r=await w.start(session.id,'查询',domain='commerce')
        r.plan.query_intent=QueryIntent.model_validate(simple())
        model=NativeModel([
            ('AgentSubmission',{'payload':{'decision':'revise','issues':['字段不在SQL里'],'rationale':'缺少前置展示字段'}}),
            ('AgentSubmission',{'payload':{'decision':'approved','rationale':'前置展示列由执行器核验后补齐，契约一致'}}),
        ])
        w.reviewer=ReviewerAgent(model)
        result=await agentic.review(w,r,ontology())
        assert result.decision=='approved' and len(model.messages)==2
        ctx=json.loads(next(m['content'] for m in model.messages[0] if m['role']=='user'))
        final=ctx['result_output_contract']['steps'][-1]
        assert final['executor_enrichment'][0]['field']=='client.segment'
        assert final['executor_enrichment'][0]['column']=='pick_clients__client_segment'
        assert 'pick_clients__client_segment' in final['final_columns']
    asyncio.run(scenario())


def test_technical_review_is_never_delegated_to_business_analyst(monkeypatch):
    async def scenario():
        from semora_contracts import ReviewDecision,ReviewFinding,RunStatus
        from app.intent_planner import intent_repair_route
        w,session,_=harness(monkeypatch)
        r=await w.start(session.id,'查询',domain='commerce')
        r.plan.query_intent=QueryIntent.model_validate(simple())
        r.review=ReviewDecision(decision='revise',issues=['编译映射错误'],rationale='具体程序错误',findings=[
            ReviewFinding(kind='repair',paths=['/source_steps/1/query/fields'],message='编译映射错误',evidence='SQL投影证据',acceptance='修复程序映射')])
        assert intent_repair_route(r.review)=='technical'
        original_review=r.review
        async def forbidden(*a,**k):
            raise AssertionError('technical repair must not reach Analyst')
        monkeypatch.setattr(w,'_analyse_with_tools',forbidden)
        result=await w._revise_node({'run_id':r.id,'ontology_model':ontology()})
        assert r.status!=RunStatus.FAILED and result['internal_repair'] is True
        assert r.sql is None and r.review is None and r.approval is None
        assert r.revision_count==1
        assert any(e.event=='plan.repaired' for e in r.events)
        original_review.findings[0].paths=['/query_intent/filters']
        assert intent_repair_route(original_review)=='business'
    asyncio.run(scenario())


def test_business_revision_preserves_structured_request_and_invalidates_execution_authority(monkeypatch):
    async def scenario():
        from semora_contracts import ReviewDecision,ReviewFinding
        w,session,_=harness(monkeypatch)
        r=await w.start(session.id,'查询',domain='commerce')
        old=outcome(simple())
        r.plan=old.plan
        r.review=ReviewDecision(decision='revise',issues=['缺少已确认阈值'],rationale='用户要求阈值',findings=[
            ReviewFinding(kind='repair',paths=['/query_intent/filters'],message='缺少阈值',evidence='用户确认',acceptance='金额大于15')])
        fixed=outcome({**simple(),'filters':[{'field':'balances.amount','operator':'gt','value':15}]})
        original=json.dumps({'current':'查询','previous':{'source_steps':[{'query':{'filters':[]}}]}})
        monkeypatch.setattr(w,'_analysis_question',lambda run:original)
        async def analyse(run,request,evidence,ont,scope,feedback):
            assert json.loads(request)['current']=='查询'
            assert feedback['findings'][0]['paths']==['/query_intent/filters']
            return fixed
        monkeypatch.setattr(w,'_analyse_with_tools',analyse)
        state=await w._revise_node({'run_id':r.id,'ontology_model':ontology()})
        assert state['internal_repair'] is False
        assert r.requirement is None and r.approval is None and r.sql is None
        assert r.source_artifacts=={} and r.source_results=={}
        assert r.proposed_plan.query_intent.filters[0].value==15
        assert r.requirement_draft.query_intent==r.proposed_plan.query_intent
    asyncio.run(scenario())


def test_remote_filter_source_is_planned_independently_of_business_subject():
    intent={**simple(),'subject':'balances',
        'filters':[{'field':'client.status','operator':'eq','value':'active'}]}
    result=outcome(intent)
    first,last=result.plan.source_steps
    assert first.data_source_id=='customers-db' and last.data_source_id=='accounts-db'
    assert first.query.filters[0].field=='client.status'
    assert first.query.fields==['client.customer_id']
    assert last.query.fields==['balances.amount']
    assert result.plan.query_intent.subject=='balances'


def test_foreign_filter_prefix_keeps_target_subject_existence(monkeypatch):
    async def scenario():
        import inspect
        from semora_contracts import OntologySnapshotRef,RunStatus
        w,session,_=harness(monkeypatch)
        ont=ontology()
        ont['objects'].append({'name':'holder','table':'holders','primaryKey':'customer_id','dataSourceId':'accounts-db',
            'properties':[{'name':'customer_id','column':'customer_id','type':'string'}]})
        ont['relations'].append({'name':'holder_accounts','from':'holder','to':'balances',
            'condition':'holder.customer_id = balances.customer_id','cardinality':'1:N','optional':False})
        databases=inspect.getclosurevars(w.execute).nonlocals['databases']
        databases['accounts-db'].execute('CREATE TABLE holders(customer_id TEXT PRIMARY KEY)')
        intent={'summary':'有效对象对应持有者的账户','subject':'holder','fields':['balances.amount'],
            'filters':[{'field':'client.status','operator':'eq','value':'active'}]}
        value=compile_intent(intent,ont,{})
        async def snapshot(domain):
            return OntologySnapshotRef(domain='commerce',version='1',content_hash='test',artifact_version='published'),ont
        class Analyst:
            async def analyse(self,question,evidence,model,scope):
                return AnalystAgent()._build_outcome(value,question,evidence,model,scope,'test')
        w.snapshot_loader=snapshot;w.analyst=Analyst()
        result=await w.start(session.id,'只查有持有者的账户',domain='commerce')
        assert result.status==RunStatus.COMPLETED,result.error
        assert result.result.rows==[],'Missing target subject must not be dropped by foreign filter planning'
    asyncio.run(scenario())
