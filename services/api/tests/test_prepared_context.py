import asyncio
from copy import deepcopy
import json

import pytest
from langchain_core.utils.function_calling import convert_to_openai_tool
from semora_contracts import ConfirmedFact,AuthorizationContext
from semora_agent import AnalystAgent,ReviewerAgent
from semora_agent.model import AnalysisBlockedError
from app import agentic
from app.prepared_context import prepare_context,context_packet,supplement_context,input_binding
from services.api.tests.test_plan_candidates import setup,ONTOLOGY,PLAN
from services.api.tests.test_agent_runtime import NativeModel,CARD


class PreparedModel(NativeModel):
    def __init__(self,calls,assessment=None):
        super().__init__(calls)
        self.assessment=assessment or {'status':'sufficient','reason':'目标、字段及执行范围已经明确'}
        self.assessments=0

    async def complete_json(self,system,user,**kwargs):
        self.assessments+=1
        return self.assessment


@pytest.fixture(autouse=True)
def bounded(monkeypatch):
    monkeypatch.setenv('AGENT_BOUNDED_CONTEXT','true')


def test_program_prepares_full_small_schema_and_planner_cannot_freely_search():
    async def scenario():
        w,run,kit,_=setup()
        state=await prepare_context(w,run,run.question,ONTOLOGY,{})
        packet=state['packet']
        assert packet['objects'][0]['field_refs']==['record.id','record.amount']
        assert 'record.id' in packet['objects'][0]['stage_group_keys']
        kit.context_state=state
        offered=kit.tool_policy([convert_to_openai_tool(t) for t in kit.tools()]+[convert_to_openai_tool(agentic.AnalysisSubmission)])
        assert {t['function']['name'] for t in offered}=={'validate_plan','request_context','AgentSubmission'}
        assert kit.protocol_stage=='planning'
    asyncio.run(scenario())


def test_prepared_layout_explains_boundary_and_local_join_fields():
    w,run,_,_=setup();ont=deepcopy(ONTOLOGY)
    ont['objects'] += [
        {'name':'bridge','table':'bridge','dataSourceId':'source-b','properties':[{'name':'id','column':'id'},{'name':'record_id','column':'record_id'}]},
        {'name':'detail','table':'detail','dataSourceId':'source-b','properties':[{'name':'bridge_id','column':'bridge_id'},{'name':'duration','column':'duration'}]}]
    ont['relations']=[{'name':'boundary','from':'record','to':'bridge','condition':'record.id = bridge.record_id'},
                      {'name':'local_join','from':'bridge','to':'detail','condition':'bridge.id = detail.bridge_id'}]
    packet=context_packet(w,run,ont,['record','detail'])
    layout=next(s for s in packet['source_layout'] if s['data_source_id']=='source-b')
    assert layout['grain_options']==['bridge','detail']
    assert set(layout['field_refs'])=={'bridge.id','bridge.record_id','detail.bridge_id','detail.duration'}
    assert layout['local_relationship_path_options']==['local_join']
    assert layout['boundary_relations'][0]['to']=='bridge'
    template=next(t for t in packet['transfer_templates'] if t['relation']=='boundary' and t['source_object']=='record')
    assert template['key_pairs']==[{'source_field':'record.id','target_field':'bridge.record_id'}]
    assert template['target_data_source_id']=='source-b'
    assert template['target_local_relationships']==['local_join']
    assert template['source_query_must_output']==['record.id']


def test_transfer_templates_preserve_composite_pairs_and_skip_unsupported_keys():
    from app.prepared_context import transfer_templates
    objects={n:{'name':n,'dataSourceId':n,'properties':[{'name':'key_a','column':'A'},{'name':'key_b','column':'B'}]} for n in ['a','b']}
    relations=[{'name':'composite','from':'a','to':'b','condition':'a.A = b.A AND a.B = b.B'}]
    templates=transfer_templates(objects,relations,{})
    assert templates[0]['key_pairs']==[{'source_field':'a.key_a','target_field':'b.key_a'},{'source_field':'a.key_b','target_field':'b.key_b'}]
    assert templates[1]['source_object']=='b'
    relations[0]['condition']='a.A = b.A OR a.B = b.B'
    assert not transfer_templates(objects,relations,{})
    relations[0]['condition']='a.A = b.A'
    objects['b']['properties'][0]['sensitive']=True
    assert not transfer_templates(objects,relations,{})


def test_source_id_contract_and_error_identify_truncated_id():
    async def scenario():
        w,run,kit,_=setup()
        offered=kit.tool_policy([convert_to_openai_tool(t) for t in kit.tools()])
        schema=next(t for t in offered if t['function']['name']=='validate_plan')['function']['parameters']
        def find_ids(node):
            if isinstance(node,dict):
                if 'data_source_id' in node.get('properties',{}):
                    assert node['properties']['data_source_id']['enum']==['source-a']
                for v in node.values():find_ids(v)
            elif isinstance(node,list):
                for v in node:find_ids(v)
        find_ids(schema)
        query={'domain':ONTOLOGY['domain'],'data_source_id':'source-a-truncated','intent':'查记录','grain':'record','fields':['record.id'],'metrics':[],'order_by':[],'limit':None}
        raw={'order_by':[],'limit':None,'source_steps':[{'name':'first','data_source_id':'source-a-truncated','query':query,'inputs':[]}]}
        tool=next(t for t in kit.tools() if t.name=='validate_plan')
        result=json.loads(await tool.ainvoke({'analysis':raw}))
        assert not result['valid'] and 'source_steps[0](first).data_source_id' in result['message']
        assert "['source-a']" in result['message']
    asyncio.run(scenario())


def test_source_diagnostics_report_all_independent_defects_in_one_pass():
    from app.prepared_context import source_diagnostics
    from semora_contracts import SourceQueryStep
    w,run,_,_=setup();ont=deepcopy(ONTOLOGY)
    ont['objects'].append({'name':'detail','table':'details','dataSourceId':'source-b','properties':[{'name':'record_id','column':'record_id'},{'name':'amount','column':'amount'}]})
    ont['relations']=[{'name':'boundary','from':'record','to':'detail','condition':'record.id = detail.record_id'}]
    step=SourceQueryStep.model_validate({'name':'target','data_source_id':'source-b','query':{'domain':ont['domain'],'data_source_id':'source-b','intent':'查询指标',
        'grain':'record','metrics':['total'],'fields':['record.id','total'],'calculations':[{'name':'total','function':'sum','field':'detail.amount'}],
        'relationship_path':['boundary']},'inputs':[]})
    issues=source_diagnostics(run,ont,[step])
    assert {i['path'].split('.query.')[-1] for i in issues}=={'grain','fields[0]','fields[1]','relationship_path'}
    assert '不是缺字段' in next(i['message'] for i in issues if i['path'].endswith('fields[0]'))


def test_model_step_null_arrays_use_empty_defaults_without_rewriting_filters():
    query={'domain':'generic','data_source_id':'source-a','intent':'查询','metrics':None,'filters':None,'relationship_path':None,'order_by':None,'limit':None}
    raw={'source_steps':[{'name':'first','data_source_id':'source-a','query':query,'inputs':[]}]}
    payload=agentic.AnalysisPayload.model_validate(raw)
    assert payload.source_steps[0].query.filters==[] and payload.source_steps[0].query.relationship_path==[]
    assert raw['source_steps'][0]['query']['filters'] is None
    raw['source_steps'][0]['query']['filters']='unexpected'
    with pytest.raises(ValueError):agentic.AnalysisPayload.model_validate(raw)
    stage={'name':'by_record','source':'record','group_by':['record.id'],'filters':None,
        'measures':[{'name':'amount','function':'sum','field':'record.amount','filters':None}]}
    raw={'stages':[stage],'source_steps':[{'name':'first','data_source_id':'source-a','query':{**query,'filters':None,'stages':[stage]},'inputs':[]}]}
    payload=agentic.AnalysisPayload.model_validate(raw)
    for normalized in [payload.stages[0],payload.source_steps[0].query.stages[0]]:
        assert normalized.filters==[] and normalized.measures[0].filters==[]


def test_entity_selection_rejects_raw_detail_top_n_and_accepts_grouped_event_time():
    from app.prepared_context import selection_diagnostics
    from semora_contracts import SourceQueryStep
    w,run,_,_=setup();ont=deepcopy(ONTOLOGY)
    ont['objects'].append({'name':'event','table':'events','dataSourceId':'source-a','properties':[{'name':'id','column':'id'},{'name':'entity_id','column':'entity_id'},{'name':'finished_at','column':'finished_at','type':'timestamp'}]})
    ont['relations']=[{'name':'entity_events','from':'record','to':'event','condition':'record.id = event.entity_id'}]
    run.agent_memory['prepared_context']=[{'assessment':{'selection':{'entity_field':'record.id','time_field':'event.finished_at','limit':2,'direction':'desc'}}}]
    query={'domain':ont['domain'],'data_source_id':'source-a','intent':'最近两个实体','grain':'event','metrics':[],
        'fields':['event.entity_id','event.id','event.finished_at'],'dimensions':['event.entity_id'],
        'order_by':[{'field':'event.finished_at','direction':'desc'}],'limit':2}
    step=SourceQueryStep.model_validate({'name':'pick','data_source_id':'source-a','query':query,'inputs':[]})
    assert selection_diagnostics(run,ont,[step])
    query.update(fields=['event.entity_id'],dimensions=['event.entity_id'],metrics=['latest_time'],
        calculations=[{'name':'latest_time','function':'max','field':'event.finished_at'}],order_by=[{'field':'latest_time','direction':'desc'}])
    step=SourceQueryStep.model_validate({'name':'pick','data_source_id':'source-a','query':query,'inputs':[]})
    assert not selection_diagnostics(run,ont,[step])
    from app.prepared_context import selection_plan_shape
    shape=selection_plan_shape(w,run,ont,{},run.agent_memory['prepared_context'][-1])
    assert shape['query_shape']['grain']=='record' and shape['query_shape']['relationship_path']==['entity_events']
    assert shape['business_filters_must_be_filled'] and 'filters' not in shape['query_shape']
    query['stages']=[{'name':'event_times','source':'event','group_by':['event.entity_id'],
        'measures':[{'name':'latest','function':'max','field':'event.finished_at'}]}]
    query['calculations'][0]['field']='event_times.latest'
    staged=SourceQueryStep.model_validate({'name':'pick','data_source_id':'source-a','query':query,'inputs':[]})
    assert not selection_diagnostics(run,ont,[staged])
    query['metrics']=[]  # The existing plan builder auto-selects declared calculations.
    implicit=SourceQueryStep.model_validate({'name':'pick','data_source_id':'source-a','query':query,'inputs':[]})
    assert not selection_diagnostics(run,ont,[implicit])


def test_record_selection_allows_repeated_entities_without_grouping():
    from app.prepared_context import selection_diagnostics,selection_plan_shape
    from semora_contracts import SourceQueryStep
    w,run,_,_=setup();ont=deepcopy(ONTOLOGY)
    ont['objects'][0]['primaryKey']='id'
    ont['objects'][0]['properties'].append({'name':'event_time','column':'event_time','type':'timestamp'})
    state={'assessment':{'selection':{'mode':'records','entity_field':'record.id','time_field':'record.event_time','limit':10,'direction':'desc'}}}
    run.agent_memory['prepared_context']=[state]
    shape=selection_plan_shape(w,run,ont,{},state)['query_shape']
    step=SourceQueryStep(name='pick',data_source_id='source-a',query=shape)
    assert not shape['calculations'] and shape['fields']==['record.id','record.event_time']
    assert not selection_diagnostics(run,ont,[step])
    step.query.fields=['record.event_time']
    assert selection_diagnostics(run,ont,[step])


def test_stage_diagnostics_report_exact_own_join_keys_with_other_errors():
    from app.prepared_context import source_diagnostics,stage_join_contracts
    from services.api.tests.test_source_execution import ontology,proposal
    from semora_contracts import SourceQueryStep
    w,run,_,_=setup();ont=ontology();run.domain=ont['domain']
    value=proposal(False)['source_steps'][1]
    value['query']['stages']=[{'name':'aggregated','source':'balances','group_by':['balances.amount'],
        'measures':[{'name':'total','function':'sum','field':'balances.amount'}]}]
    # A same-source edge, while an invalid cross-source edge remains in the path.
    ont['objects'].append({'name':'header','table':'headers','dataSourceId':'accounts-db','properties':[{'name':'key','column':'key'}]})
    ont['relations'].append({'name':'local_detail','from':'header','to':'balances','condition':'header.key = balances.customer_id','cardinality':'1:N'})
    value['query']['relationship_path']=['local_detail','customer_accounts']
    step=SourceQueryStep.model_validate(value)
    errors=source_diagnostics(run,ont,[step])
    assert any(e['path'].endswith('stages[0].group_by') and 'balances.customer_id' in e['message'] for e in errors)
    assert any(e['path'].endswith('relationship_path') for e in errors)
    assert {'relation':'local_detail','group_by':['balances.customer_id']} in stage_join_contracts('balances',ont)


def test_selection_guard_runs_after_existing_implicit_metric_normalization():
    from services.api.tests.test_source_execution import ontology,proposal
    async def scenario():
        w,run,_,_=setup();ont=ontology();run.domain=ont['domain']
        ont['objects'][0]['properties'].append({'name':'finished_at','column':'finished_at','type':'timestamp'})
        kit=agentic.AgentTools(w,run,ont,{})
        run.agent_memory['prepared_context']=[{'assessment':{'selection':{'entity_field':'client.customer_id','time_field':'client.finished_at','limit':2,'direction':'desc'}}}]
        value=proposal(False);value.update(order_by=[],limit=None)
        first=value['source_steps'][0]['query']
        first.update(fields=['client.customer_id'],dimensions=['client.customer_id'],metrics=[],grain='client',
            calculations=[{'name':'latest_time','function':'max','field':'client.finished_at'}],
            order_by=[{'field':'latest_time','direction':'desc'}],limit=2)
        value['source_steps'][1]['query'].update(order_by=[],limit=None)
        tool=next(t for t in kit.tools() if t.name=='validate_plan')
        result=json.loads(await tool.ainvoke({'analysis':value}))
        assert result['valid'],result
    asyncio.run(scenario())


def test_context_reused_but_real_user_answers_reset_budget_and_preparation():
    async def scenario():
        w,run,kit,_=setup()
        first=await prepare_context(w,run,run.question,ONTOLOGY,{})
        assert await prepare_context(w,run,run.question,ONTOLOGY,{}) is first
        kit.progress_state.update(streak=3,seen=['old'])
        same=agentic.AgentTools(w,run,ONTOLOGY,{})
        same.start_phase('另一个技术阶段')
        assert same.progress_state['streak']==3
        run.user_answers=[ConfirmedFact(question_id='scope',field='time_range',value='all',label='不限时间')]
        changed=agentic.AgentTools(w,run,ONTOLOGY,{})
        assert changed.progress_state.get('streak',0)==0 and changed.progress_state.get('seen',[])==[]
        updated=await prepare_context(w,run,run.question,ONTOLOGY,{})
        assert updated['binding']!=first['binding'] and updated['packet']==first['packet']
    asyncio.run(scenario())


def test_only_one_core_supplement_no_optional_free_expansion():
    w,run,_,_=setup();ont=deepcopy(ONTOLOGY)
    for name in ['extra','another']:ont['objects'].append({'name':name,'table':name,'properties':[{'name':'id','column':'id'}]})
    state={'binding':input_binding(run,ont,{}),'packet':context_packet(w,run,ont,['record']),'supplements':0}
    result=supplement_context(w,run,ont,state,['extra'],'join','当前关联缺少中间对象',{})
    assert result['available'] and state['supplements']==1
    assert supplement_context(w,run,ont,state,['another'],'output','补齐必要字段',{})['available'] is False
    with pytest.raises(ValueError,match='尚未形成SQL'):
        supplement_context(w,run,ont,state,['another'],'sql_error','推测可能有错误',{})
    with pytest.raises(ValueError,match='授权'):
        supplement_context(w,run,ont,state,['unpublished'],'filter','缺字段',{})


def test_context_does_not_expose_unauthorized_objects_or_scan_credentials():
    w,run,_,_=setup();ont=deepcopy(ONTOLOGY)
    run.authorization=AuthorizationContext(subject='u',username='u',roles=['analyst'],allowed_data_source_ids=['source-a'])
    ont['objects'].append({'name':'secret','table':'secret','dataSourceId':'source-denied','properties':[{'name':'id','column':'id'}]})
    with pytest.raises(ValueError,match='未授权'):context_packet(w,run,ont,['secret'])
    packet=context_packet(w,run,ont,['record'])
    assert 'secret_ref' not in json.dumps(packet) and 'secret' not in packet['selected_objects']


def test_bounded_analyst_delivers_without_metadata_react_and_reviewer_stays_independent():
    async def scenario():
        w,run,_,_=setup()
        model=PreparedModel([('validate_plan',{'analysis':{**PLAN,'order_by':[],'limit':None}})])
        reviewer=NativeModel([('AgentSubmission',{'payload':{'decision':'approved','rationale':'目标和执行条件一致'}})])
        w.analyst=AnalystAgent(model);w.reviewer=ReviewerAgent(reviewer)
        outcome=await agentic.analyse(w,run,run.question,ONTOLOGY,{})
        assert outcome.plan.fields==PLAN['fields'] and len(model.messages)==1 and len(reviewer.messages)==1
        assert any(e.event=='agent.delivery.canonical' for e in run.events)
        data=json.loads(next(m['content'] for m in model.messages[0] if m['role']=='user'))
        assert data['prepared_context']['objects'][0]['field_refs']==['record.id','record.amount']
        assert run.sql is None
    asyncio.run(scenario())


def test_repeated_invalid_plan_gets_bounded_final_delivery_without_executing():
    async def scenario():
        w,run,_,_=setup()
        invalid={**PLAN,'fields':['record.missing'],'order_by':[],'limit':None}
        m=PreparedModel([('validate_plan',{'analysis':invalid})]*3+[('AgentSubmission',{'payload':{'blocked_reason':'所需字段未发布，需补齐映射','clarifications':[]}})])
        w.analyst=AnalystAgent(m)
        with pytest.raises(AnalysisBlockedError,match='所需字段未发布'):
            await agentic.analyse(w,run,run.question,ONTOLOGY,{})
        assert len(m.messages)==4 and run.sql is None and run.result is None
    asyncio.run(scenario())


def test_three_concrete_repairs_can_deliver_without_open_ended_exploration():
    async def scenario():
        w,run,_,_=setup()
        calls=[('validate_plan',{'analysis':{**PLAN,'fields':['record.invalid_'+str(i)],'order_by':[],'limit':None}}) for i in range(3)]
        calls.append(('validate_plan',{'analysis':{**PLAN,'order_by':[],'limit':None}}))
        m=PreparedModel(calls)
        w.analyst=AnalystAgent(m)
        w.reviewer=ReviewerAgent(NativeModel([('AgentSubmission',{'payload':{'decision':'approved','rationale':'执行方案符合需求'}})]))
        outcome=await agentic.analyse(w,run,run.question,ONTOLOGY,{})
        assert outcome.plan and len(m.messages)==4
        assert run.sql is None and run.result is None
    asyncio.run(scenario())


def test_patch_plan_preserves_other_conditions_revalidates_and_binds_version():
    from langchain_core.messages import ToolMessage
    async def scenario():
        w,run,kit,_=setup()
        tools={t.name:t for t in kit.tools()}
        invalid={**PLAN,'fields':['record.missing'],'order_by':[],'limit':None}
        receipt=json.loads(await tools['validate_plan'].ainvoke({'analysis':invalid}))
        assert not receipt['valid'] and receipt['invalid_candidate_id']
        patch={'invalid_candidate_id':receipt['invalid_candidate_id'],'patches':[{'path':'fields','value_json':json.dumps(PLAN['fields'])}]}
        result=json.loads(await tools['patch_plan'].ainvoke(patch))
        assert result['valid'],result
        stored=kit.candidates[result['candidate_id']]['analysis']
        assert stored['filters']==invalid['filters'] and stored['limit'] is None
        assert kit.canonical_delivery([ToolMessage(content=json.dumps(result),tool_call_id='patch',name='patch_plan')])=={'candidate_id':result['candidate_id']}
        run.question='用户已修改需求'
        stale=json.loads(await tools['patch_plan'].ainvoke(patch))
        assert not stale['valid'] and '失效' in stale['message']
    asyncio.run(scenario())


@pytest.mark.parametrize('path',['source_steps.2.inputs.0.include_source_fields','source_steps[2].inputs[0].include_source_fields','/source_steps/2/inputs/0/include_source_fields','analysis.source_steps[2](step_name).inputs[0].include_source_fields'])
def test_patch_paths_accept_standard_forms_without_guessing_values(path):
    patch=agentic.PlanPatch(path=path,value_json='[]')
    assert patch.path=='source_steps.2.inputs.0.include_source_fields'
    with pytest.raises(ValueError):agentic.PlanPatch(path='source_steps[other].query',value_json='[]')


def test_explicit_retry_revalidates_timed_out_preflight_without_reanalysing():
    from semora_contracts import OntologySnapshotRef,RunStatus
    async def scenario():
        w,run,_,_=setup()
        run.question='重试'
        run.ontology=OntologySnapshotRef(domain='generic',version='1',content_hash='h',artifact_version='published')
        m=PreparedModel([]);w.analyst=AnalystAgent(m)
        reviewer=NativeModel([('AgentSubmission',{'payload':{'decision':'approved','rationale':'口径与执行字段一致'}})])
        w.reviewer=ReviewerAgent(reviewer)
        source=run.model_copy(deep=True);source.id='previous-preflight';source.question='查询记录'
        source.status=RunStatus.FAILED
        candidate=w.analyst._build_outcome(PLAN,source.question,[],ONTOLOGY,{},'test').plan.model_dump(mode='json')
        source.agent_memory['last_candidate']=[{'plan':candidate}]
        w.emit(source,'agent.request.timeout',RunStatus.UNDERSTANDING,{'agent':'reviewer','phase':'方案完整性预检'},'reviewer')
        source.status=RunStatus.FAILED
        w.store.runs[source.id]=source
        run.continuation={'source_run_id':source.id,'candidate_plan':candidate,'bindings_changed':False}
        outcome=await agentic.analyse(w,run,run.question,ONTOLOGY,{})
        assert outcome.plan and not m.messages and len(reviewer.messages)==1
        assert any(e.event=='plan.candidate.resumed' for e in run.events)
        run.question='改为查询其他范围'
        assert agentic.retry_validated_candidate(w,run) is None
        run.question='重试';run.continuation['bindings_changed']=True
        assert agentic.retry_validated_candidate(w,run) is None
    asyncio.run(scenario())


def test_execution_retry_requires_confirmed_approved_unchanged_candidate():
    from semora_contracts import OntologySnapshotRef,RequirementSpec,ReviewDecision,RunStatus
    w,run,_,_=setup();run.question='重试'
    run.ontology=OntologySnapshotRef(domain='generic',version='1',content_hash='h',artifact_version='published')
    source=run.model_copy(deep=True);source.id='previous-execution';source.status=RunStatus.FAILED
    source.plan=w.analyst._build_outcome(PLAN,'查询记录',[],ONTOLOGY,{},'test').plan
    source.requirement=RequirementSpec(version='req',content_hash='h',source_draft_version=1,summary='已确认',confirmed_by='owner')
    source.review=ReviewDecision(decision='approved',rationale='通过')
    value=source.plan.model_dump(mode='json');source.agent_memory['last_candidate']=[{'plan':value}]
    w.store.runs[source.id]=source
    run.continuation={'source_run_id':source.id,'candidate_plan':value,'failed_stage':'executing','bindings_changed':False}
    assert agentic.retry_validated_candidate(w,run)==value
    source.status=RunStatus.COMPLETED
    run.continuation['failed_stage']='completed'
    assert agentic.retry_validated_candidate(w,run)==value
    source.review.decision='rejected'
    assert agentic.retry_validated_candidate(w,run) is None


def test_all_history_lookup_keeps_selection_filters_and_security_scope():
    from types import SimpleNamespace as NS
    from app.prepared_context import business_contract_diagnostics
    w,run,_,_=setup();run.question='重试'
    run.agent_memory['confirmed_fact_context']=[{'field':'record.amount',
        'selected_option_description':'取该对象全部记录（不限时间）的最大值'}]
    run.agent_memory['prepared_context']=[{'assessment':{'selection':{'entity_field':'record.id','time_field':'record.id','limit':2,'direction':'desc'}}}]
    def query(fields,filters):
        return NS(fields=fields,dimensions=[],calculations=[],stages=[],filters=filters,time_range=None,limit=None,data_scope={'organization':['allowed']})
    picked=NS(name='pick',inputs=[],query=query([],['selection-range']))
    picked.query.limit=2
    lookup=NS(name='lookup',inputs=[NS(source_step='pick')],query=query(['record.amount'],['unwanted-range']))
    # Isolate lookup semantics from the separately tested Top-N shape checks.
    from unittest.mock import patch
    with patch('app.prepared_context.selection_diagnostics',side_effect=lambda *args:[]):
        issues=business_contract_diagnostics(run,ONTOLOGY,[picked,lookup])
        assert len(issues)==1 and issues[0]['path']=='source_steps[1](lookup).query.filters'
        assert issues[0]['replacement_json']=='[]'
        assert lookup.query.data_scope=={'organization':['allowed']}
        run.question='这次只查询最近记录'
        assert not business_contract_diagnostics(run,ONTOLOGY,[picked,lookup])


def test_confirmed_option_description_survives_legacy_and_future_handoffs():
    from app.prepared_context import confirmed_fact_context
    from app.continuation import handoff
    from types import SimpleNamespace
    w,run,_,_=setup()
    run.user_answers=[ConfirmedFact(question_id='scope',field='record.amount',value='global',label='全部记录')]
    source=run.model_copy(deep=True);source.id='original-question'
    source.alignment_rounds=[{'draft':{'clarifications':[{'id':'scope','field':'record.amount','question':'统计范围？',
        'options':[{'id':'all','value':'global','label':'全部记录','description':'不限记录、不限时间，统计全部历史'}]}]}}]
    w.store.runs[source.id]=source;run.continuation={'source_run_id':source.id}
    facts=confirmed_fact_context(w,run)
    assert facts[0]['selected_option_description']=='不限记录、不限时间，统计全部历史'
    child=run.model_copy(deep=True);child.id='child';child.agent_memory={}
    child.continuation=handoff(run,SimpleNamespace(requirement_form={}))
    w.store.runs.clear()
    assert confirmed_fact_context(w,child)[0]['selected_option_description']==facts[0]['selected_option_description']
    child.user_answers[0].value='new_scope'
    assert 'selected_option_description' not in confirmed_fact_context(w,child)[0]


@pytest.mark.parametrize('purpose',['',None,'   '])
def test_ambiguity_goes_straight_to_card_without_lookup_loop(purpose):
    async def scenario():
        w,run,_,_=setup();m=PreparedModel([],{'status':'ambiguous','reason':'时间未明确','clarifications':CARD['clarifications'],'purpose':purpose});w.analyst=AnalystAgent(m)
        outcome=await agentic.analyse(w,run,run.question,ONTOLOGY,{})
        assert outcome.plan is None and outcome.requirement.clarifications and len(m.messages)==0 and m.assessments==1
    asyncio.run(scenario())


@pytest.mark.parametrize('purpose',['',None,'   '])
def test_assessment_optional_purpose_does_not_break_ready_state(purpose):
    from app.prepared_context import InformationAssessment
    assert InformationAssessment(status='sufficient',reason='已有必要信息',purpose=purpose).purpose is None
    with pytest.raises(ValueError,match='核心资料补充必须说明'):
        InformationAssessment(status='missing_core',reason='缺少关联对象',missing_objects=['extra'],purpose=purpose)
    with pytest.raises(ValueError):
        InformationAssessment(status='missing_core',reason='缺少关联对象',missing_objects=['extra'],purpose='invented')


def test_assessment_null_lists_are_empty_without_hiding_missing_core_errors():
    from app.prepared_context import InformationAssessment
    ready=InformationAssessment(status='sufficient',reason='已有必要信息',clarifications=None,missing_objects=None,purpose='')
    assert ready.clarifications==[] and ready.missing_objects==[]
    ambiguous=InformationAssessment(status='ambiguous',reason='时间未明确',clarifications=CARD['clarifications'],missing_objects=None,purpose=None)
    assert ambiguous.clarifications and ambiguous.missing_objects==[]
    with pytest.raises(ValueError):
        InformationAssessment(status='sufficient',reason='已有必要信息',missing_objects='unexpected')


def test_latest_request_and_two_stage_intent_are_visible_before_assessment():
    from app.prepared_context import assess_information
    from semora_agent.intent_rules import REQUEST_INTERPRETATION_RULES
    async def scenario():
        w,run,_,_=setup()
        run.question='先按结束时间取最近10个对象，然后按费用排序'
        run.user_answers=[ConfirmedFact(question_id='old_sort',field='sort_logic',value='cost_top10',label='按费用选前10个')]
        state=await prepare_context(w,run,run.question,ONTOLOGY,{})
        class Model:
            async def complete_json(self,system,user,**kwargs):
                assert REQUEST_INTERPRETATION_RULES in system
                payload=json.loads(user)
                assert payload['latest_user_request']==run.question
                assert payload['confirmed_answers'][0]['value']=='cost_top10'
                assert '最新明确需求覆盖' in payload['answer_precedence']
                return {'status':'sufficient','reason':'先选集合，再进行展示排序'}
        w.analyst.model=Model()
        assert (await assess_information(w,run,run.question,ONTOLOGY,{},state)).status=='sufficient'
        assert REQUEST_INTERPRETATION_RULES in agentic.ANALYST_PROMPT
        assert REQUEST_INTERPRETATION_RULES in agentic.REVIEWER_PROMPT
    asyncio.run(scenario())


def test_large_catalog_selects_once_and_rejects_unknown_objects():
    async def scenario():
        w,run,_,_=setup();ont=deepcopy(ONTOLOGY)
        ont['objects'] += [{'name':'item_'+str(i),'table':'table_'+str(i),'properties':[{'name':'id','column':'id'}]} for i in range(9)]
        class Selector:
            calls=0
            async def complete_json(self,system,user):
                self.calls+=1
                return {'objects':['record'],'knowledge_keywords':[]}
        m=Selector();w.analyst.model=m
        a=await prepare_context(w,run,'查询记录',ont,{})
        assert a['packet']['selected_objects']==['record'] and m.calls==1
        assert await prepare_context(w,run,'查询记录',ont,{}) is a and m.calls==1
        run.agent_memory.pop('prepared_context')
        async def invalid(*args):return {'objects':['unpublished'],'knowledge_keywords':[]}
        m.complete_json=invalid
        with pytest.raises(ValueError,match='未授权'):await prepare_context(w,run,'查询记录',ont,{})
    asyncio.run(scenario())


def test_prepared_scan_only_includes_structure_not_connection_secret(monkeypatch):
    from app import prepared_context as prep
    from app.datasources import DataSourceRecord
    source=DataSourceRecord(id='source-a',name='demo',type='mysql',secret_ref='PRIVATE_SECRET_REF',scan_result={
        'connection':'PRIVATE_DSN','tables':[{'name':'records','columns':[{'name':'id','type':'int'}],'rows':[['PRIVATE_VALUE']],'secret':'PRIVATE_SECRET'}]})
    monkeypatch.setattr(prep.datasource_service.repository,'get',lambda _:source)
    w,run,_,_=setup();packet=context_packet(w,run,ONTOLOGY,['record'])
    assert packet['schemas'][0]['scanned']
    assert 'PRIVATE' not in json.dumps(packet)


def test_reviewer_keeps_full_semantic_definitions_without_duplicate_physical_scan():
    from app.prepared_context import review_context
    w,run,_,_=setup()
    plan=w.analyst._build_outcome(PLAN,run.question,[],ONTOLOGY,{},'test').plan
    state=review_context(w,run,ONTOLOGY,plan,{})
    assert 'schemas' not in state['packet'] and state['packet']['scan_status'][0]['object']=='record'
    assert state['packet']['objects'][0]['field_refs']==['record.id','record.amount']
    assert state['packet']['objects'][0]['object']['properties']==ONTOLOGY['objects'][0]['properties']


def test_reviewer_receives_current_lookup_scopes_not_old_narrative(monkeypatch):
    from services.api.tests.test_source_execution import ontology,proposal
    from semora_contracts import SQLArtifact
    async def scenario():
        w,run,_,_=setup();ont=ontology();run.domain=ont['domain']
        run.plan=w.analyst._build_outcome(proposal(False),run.question,[],ont,{},'test').plan
        run.sql=SQLArtifact(semantic_sql='SELECT 1',physical_sql='SELECT 1',artifact_hash='h')
        run.source_artifacts={s.name:run.sql.model_copy(deep=True) for s in run.plan.source_steps}
        async def capture(model,prompt,context,*args,**kwargs):
            scopes=context['effective_step_scopes']
            assert scopes[0]['filters'] and scopes[1]['filters']==[] and scopes[1]['time_range'] is None
            assert scopes[1]['runtime_key_bindings'][0]['relation']=='customer_accounts'
            assert context['phase']=='pre_execution'
            assert context['task_contract']['operation']=='execution_review'
            return {'decision':'approved','issues':[],'rationale':'当前范围已核验'},[]
        monkeypatch.setattr(agentic,'run_agent',capture)
        assert (await agentic.review(w,run,ont)).decision=='approved'
    asyncio.run(scenario())


def test_assessment_cannot_repeat_supplement_or_hide_ambiguity():
    from app.prepared_context import assess_information
    async def scenario():
        w,run,_,_=setup();ont=deepcopy(ONTOLOGY)
        ont['objects'].append({'name':'extra','table':'extra','properties':[{'name':'id','column':'id'}]})
        state={'binding':input_binding(run,ont,{}),'packet':context_packet(w,run,ont,['record']),'supplements':0}
        responses=iter([{'status':'missing_core','reason':'关联所需对象未准备','missing_objects':['extra'],'purpose':'join'},
                        {'status':'sufficient','reason':'已有必要关联资料'}])
        class Model:
            calls=0
            async def complete_json(self,*args,**kwargs):self.calls+=1;return next(responses)
        m=Model();w.analyst.model=m
        result=await assess_information(w,run,run.question,ont,{},state)
        assert result.status=='sufficient' and state['supplements']==1 and m.calls==2
        assert await assess_information(w,run,run.question,ont,{},state)==result and m.calls==2
        state.pop('assessment')
        async def invalid(*a,**kw):return {'status':'ambiguous','reason':'存在歧义','clarifications':[]}
        m.complete_json=invalid
        with pytest.raises(ValueError,match='具体澄清'):await assess_information(w,run,run.question,ont,{},state)
    asyncio.run(scenario())


@pytest.mark.parametrize('failure,repair',[(ValueError('具体字段映射错误'),True),(RuntimeError('service unavailable'),False)])
def test_only_concrete_compiler_errors_enter_bounded_repair(failure,repair):
    from semora_contracts import OntologySnapshotRef,RequirementSpec
    async def scenario():
        w,run,_,_=setup();w.store.runs[run.id]=run
        run.ontology=OntologySnapshotRef(domain='generic',version='1.0',content_hash='sha256:test',artifact_version='1.0')
        run.plan=w.analyst._build_outcome(PLAN,run.question,[],ONTOLOGY,{},'test').plan
        run.requirement=RequirementSpec(version='req',content_hash='h',source_draft_version=1,summary='已确认',confirmed_by='user')
        async def invalid(*args):raise failure
        w._compile=invalid
        outcome=await w._compile_node({'run_id':run.id,'ontology_model':ONTOLOGY,'settings':{}})
        assert bool(outcome.get('needs_revision'))==repair
        if repair:
            assert run.review.decision=='revise' and run.status.value=='reviewing'
            assert any(e.event=='compilation.revision.required' for e in run.events)
        else:assert run.status.value=='failed'
        assert run.sql is None and run.result is None
    asyncio.run(scenario())
