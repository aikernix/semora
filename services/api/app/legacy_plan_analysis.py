"""Explicit compatibility adapter for model-authored QueryPlan. Not the default protocol."""
from __future__ import annotations

import os
from copy import deepcopy
from uuid import uuid4
from types import SimpleNamespace
from datetime import datetime, timedelta, timezone

from semora_agent.model import TaskSupersededError

from .review_loop import capabilities, issue_ledger, check_revision, route_findings
from .task_memory import build_memory, memory_context, repair_context, binding as task_binding, digest as task_digest
from .datasources import datasource_service
from .task_handoff import handoff


from .role_contracts import AnalysisPayload, CandidateDiagnostics, PlanPatch, ANALYST_PROMPT
from semora_agent.legacy_prompts import BOUNDED_ANALYST

from .role_ports import AnalysisRolePorts
from .review_analysis import selected_catalog


async def analyse_legacy(workflow, run, question, ontology, scope, feedback=None, *, ports: AnalysisRolePorts):
    from .task_handoff import revision
    initial_revision=revision(run)
    from .prepared_context import confirmed_fact_context
    fact_context=confirmed_fact_context(workflow,run)
    toolkit = ports.create_tools(workflow, run, ontology, scope)
    toolkit.start_phase('复核后修订' if feedback else '失败后继续分析' if run.continuation else '补充需求分析' if run.agent_memory.get('analyst') else '首次需求分析')
    context = {
        'protocol': '先按需探索；上下文足够时调用enter_planning，再validate_plan；校验通过后仅用candidate_id交付。需要补证据或修改时调用resume_exploration。' if toolkit.staged else '兼容协议',
        'request': question, 'review_feedback': feedback, 'execution_capabilities': capabilities(),'confirmed_fact_context':fact_context,
        'continuation': run.continuation,
        'business_domain': {key: ontology.get(key) for key in ('domain', 'name', 'description')},
        'current_date': datetime.now(timezone(timedelta(hours=8))).date().isoformat(),
        'ontology_version': run.ontology.model_dump(mode='json') if run.ontology else None,
        'objects': [{'name': o['name'], 'label': o.get('label'), 'data_source_id': o.get('dataSourceId') or ontology.get('dataSourceId')} for o in ontology.get('objects', [])],
        'metrics': ontology.get('metrics', []), 'knowledge_base_ids': ontology.get('knowledgeBaseIds') or [],
        'confirmed_requirement': run.requirement.model_dump(mode='json') if run.requirement else (run.continuation or {}).get('confirmed_requirement'),
        'rejected_plan': run.plan.model_dump(mode='json') if feedback and run.plan else None,
        'rejected_sql': run.sql.model_dump(mode='json') if feedback and run.sql else None,
        'evidence': [e.model_dump(mode='json') for e in run.evidence],
    }
    draft=run.requirement_draft.model_dump(mode='json') if run.requirement_draft else (run.agent_memory.get('last_candidate') or [{}])[-1].get('requirement')
    if draft:
        answered={f.question_id for f in run.user_answers}
        context['open_business_questions']=[q for q in draft.get('clarifications',[]) if q.get('id') not in answered]
    bounded_prompt=ANALYST_PROMPT
    if toolkit.bounded:
        from .prepared_context import prepare_context,authorized_objects,assess_information
        toolkit.context_state=await prepare_context(workflow,run,question,ontology,scope)
        assessment=await assess_information(workflow,run,question,ontology,scope,toolkit.context_state)
        if revision(run)!=initial_revision:raise TaskSupersededError('需求在上下文准备期间已更新')
        if assessment.status=='ambiguous':
            return workflow.analyst._build_outcome({'summary':assessment.reason,'clarifications':[q.model_dump(mode='json') for q in assessment.clarifications]},question,run.evidence,ontology,scope,'llm-context-assessment')
        context['information_assessment']=assessment.model_dump(mode='json')
        from .prepared_context import selection_plan_shape
        context['selection_plan_shape']=selection_plan_shape(workflow,run,ontology,scope,toolkit.context_state)
        context['prepared_context']=toolkit.context_state['packet']
        context['metrics']=toolkit.context_state['packet']['metrics']
        context['objects']=[{'name':o['name'],'label':o.get('label'),'data_source_id':o.get('dataSourceId') or ontology.get('dataSourceId')} for o in authorized_objects(run,ontology).values()]
        context['protocol']='prepared_legacy_plan'
        bounded_prompt=BOUNDED_ANALYST
    context['datasources'] = []
    source_objects=authorized_objects(run,ontology).values() if toolkit.bounded else ontology.get('objects',[])
    for source_id in sorted({o.get('dataSourceId') or ontology.get('dataSourceId') for o in source_objects} - {None, ''}):
        try:
            source = datasource_service.repository.get(source_id)
            context['datasources'].append({'id': source.id, 'name': source.name, 'type': source.type, 'status': source.status})
        except KeyError:
            context['datasources'].append({'id': source_id, 'status': 'not_registered'})
    use_task_memory = os.getenv('AGENT_TASK_MEMORY', 'false').lower() == 'true'
    if use_task_memory:
        prior=workflow._confirmed_history(run) if run.message_mode != 'new' and not run.continuation else []
        snapshot = build_memory(run, ontology, scope, feedback,
                                prior_requirement=prior[-1]['confirmed_requirement'] if prior else None)
        context['task_memory'] = memory_context(snapshot, question)
        context['evidence'] = []  # The identical full references live once in task_memory.evidence.
        # Keep the original analysis request intact until fixed evaluation proves
        # its full replacement safe. Only bulky duplicated artifacts are refs.
        context['continuation'] = {'source_run_id': (run.continuation or {}).get('source_run_id')}
        context['confirmed_requirement'] = None
        workflow.store.save_run(run)
    previous_failure=(run.continuation or {}).get('previous_error')
    if not feedback and previous_failure and (run.continuation or {}).get('failed_stage') in {'understanding','clarifying','aligning'}:
        context['previous_attempt_failed']={'source_run_id':(run.continuation or {}).get('source_run_id'),
            'instruction':'上次未形成有效执行方案。历史模型的受阻说明不是当前本体缺口或执行能力证据；以当前prepared_context、selection_plan_shape和程序校验结果为准。用户原需求和确认事实仍完整保留。'}
    elif feedback or previous_failure:
        context['local_repair'] = repair_context((run.continuation or {}).get('previous_error') or (feedback or {}).get('rationale'),
                                                context.get('rejected_plan'), (feedback or {}).get('findings'))
    def validate(value):
        value = toolkit.resolve_submission(value)
        outcome = workflow.analyst._build_outcome(value, question, run.evidence, ontology, scope, 'llm-react')
        if outcome.plan:
            from .query_stages import validate_query_shape
            validate_query_shape(outcome.plan, ontology)
            if toolkit.bounded:
                from .prepared_context import business_contract_diagnostics
                steps=outcome.plan.source_steps or [SimpleNamespace(name='single',query=outcome.plan)]
                errors=business_contract_diagnostics(run,ontology,steps)
                if errors:raise CandidateDiagnostics(errors)
        if feedback and run.plan and outcome.plan:
            execution_fields = {'metrics', 'dimensions', 'fields', 'filters', 'grain', 'time_range', 'calculations', 'stages', 'source_steps', 'relationship_path', 'data_scope', 'order_by', 'limit'}
            if outcome.plan.model_dump(include=execution_fields) == run.plan.model_dump(include=execution_fields):
                raise ValueError('复核要求修订，但执行计划没有变化。请根据复核意见修改 filters、calculations 或关联等执行字段；不能只改说明文字。无法落实时请提出具体澄清。')
    pending = []
    previous_plan = None
    resumed=None
    resume_repairs=[]
    saved=retry_validated_candidate(workflow,run) if toolkit.bounded and not feedback else None
    if saved:
        try:
            resumed=workflow.analyst._build_outcome(saved,question,run.evidence,ontology,scope,'revalidated-preflight-retry')
            from .query_stages import validate_query_shape
            from .prepared_context import business_contract_diagnostics
            if resumed.plan is None:raise ValueError('候选不是完整方案')
            validate_query_shape(resumed.plan,ontology)
            errors=business_contract_diagnostics(run,ontology,resumed.plan.source_steps or [SimpleNamespace(name='single',query=resumed.plan)])
            if errors:raise CandidateDiagnostics(errors)
        except CandidateDiagnostics as exc:
            resume_repairs=exc.issues;resumed=None
        except ValueError:resumed=None
    if resume_repairs:
        repair_id=str(uuid4())
        toolkit.invalid_candidate={'id':repair_id,'analysis':{k:v for k,v in saved.items() if k in AnalysisPayload.model_fields},'binding':toolkit.candidate_binding()}
        toolkit.patch_only=True
        suggestions=[{'path':PlanPatch.normalize_path(e['path']),'value_json':e['replacement_json']} for e in resume_repairs if 'replacement_json' in e]
        if len(suggestions)==len(resume_repairs):toolkit.allowed_patch_paths={s['path'] for s in suggestions}
        context.update(candidate_plan=saved,invalid_candidate_id=repair_id,required_repairs=resume_repairs,suggested_patches=suggestions,
            revision_instruction='原候选结构有效，但遗漏了完整的确认口径；只用patch_plan修订列出的范围条件，禁止重写其余方案。')
    for attempt in range(3):
        if attempt==0 and resumed is not None:
            outcome=resumed
            workflow.emit(run,'plan.candidate.resumed',run.status,{'summary':'已重新校验原候选，继续独立复核；未重跑需求分析或继承执行权限'},'conversation_owner')
        else:
            task_context,toolkit.task_guard=handoff(workflow,run,'analyst','interpret_and_plan',context)
            toolkit.task_id=task_context['task_contract']['task_id']
            value, memory = await ports.runner(workflow.analyst.model, bounded_prompt, task_context, toolkit.tools(), [], toolkit.event, validate, ports.submission, tool_policy=toolkit.tool_policy, canonical_delivery=toolkit.canonical_delivery,progress_state=toolkit.progress_state,bounded_protocol=toolkit.bounded,max_models=5 if toolkit.bounded else 16,max_tools=4 if toolkit.bounded else 12,task_guard=toolkit.task_guard)
            value = toolkit.resolve_submission(value)
            outcome = workflow.analyst._build_outcome(value, question, run.evidence, ontology, scope, 'llm-react')
            run.agent_memory['analyst'] = memory
        run.agent_memory['last_candidate'] = [{'requirement': outcome.requirement.model_dump(mode='json') if outcome.requirement else None,
                                               'plan': outcome.plan.model_dump(mode='json') if outcome.plan else None}]
        workflow.store.save_run(run)
        if outcome.plan is None or not workflow._agentic_enabled('reviewer'):
            return outcome
        if previous_plan is not None:
            unchanged = check_revision(previous_plan, outcome.plan, pending)
            if unchanged:
                workflow.emit(run, 'plan.revision.stalled', run.status, {'finding_ids': unchanged, 'summary': '复核指定的执行内容未修改，已停止重复复核'}, 'owner')
                raise ValueError('自动修订未完成：复核指定的执行内容没有变化。已保留需求与候选方案，尚未执行查询。')
        check = await ports.review(workflow, run, ontology, question, outcome, pending)
        normalized = issue_ledger(check, pending)
        for finding in check.findings:
            matching = next((f for f in normalized if f['kind'] == finding.kind and f['message'] == finding.message and f['paths'] == finding.paths), None)
            if matching:
                finding.id = matching['id']
        from .control_state import record_preflight
        record_preflight(run,outcome.plan,ontology,scope,check,attempt+1,pending)
        workflow.emit(run, 'plan.preflight.completed', run.status, {'review': check.model_dump(mode='json'), 'attempt': attempt + 1}, 'reviewer')
        if check.decision == 'approved':
            workflow.store.save_run(run)
            return outcome
        if check.decision != 'rejected':
            routed = route_findings(check, outcome)
            if routed is not None:
                workflow.emit(run, 'plan.review.routed', run.status, {'summary': '复核发现口径冲突或能力缺口，停止内部修订并请求用户选择', 'findings': [f.model_dump(mode='json') for f in check.findings]}, 'owner')
                return routed
        if check.decision == 'rejected' or attempt == 2:
            # Detailed feedback is already recorded in plan.preflight.completed.
            # It must not be copied into the user's top-level failure banner.
            raise ValueError('查询方案暂未通过校验，自动修订未完成。已保留需求和补充内容，尚未生成或执行 SQL，请重新分析。')
        pending = issue_ledger(check, pending)
        previous_plan = outcome.plan.model_copy(deep=True)
        context['pending_findings'] = pending
        context['local_repair'] = repair_context(check.rationale, outcome.plan.model_dump(mode='json'), [f.model_dump(mode='json') for f in check.findings])
        context['preflight_feedback'] = check.model_dump(mode='json')
        context['candidate_plan'] = outcome.plan.model_dump(mode='json')
        context['repair_catalog'] = selected_catalog(outcome.plan, ontology)
        toolkit.start_phase('方案预检后修订')
        toolkit.revision_base = previous_plan
        toolkit.pending_findings = pending
        repair_id=str(uuid4())
        repair_value={k:v for k,v in outcome.plan.model_dump(mode='json').items() if k in AnalysisPayload.model_fields}
        toolkit.invalid_candidate={'id':repair_id,'analysis':repair_value,'binding':toolkit.candidate_binding()}
        toolkit.patch_only=True
        context['invalid_candidate_id']=repair_id
        context['revision_instruction']='优先用patch_plan只修改Reviewer指出的执行路径，不重新抄写其余方案；所有修订仍须通过完整校验。'
    raise AssertionError('unreachable')


def retry_validated_candidate(workflow,run):
    """Explicit unchanged retries revalidate a retained plan, never authority."""
    if run.question.strip().rstrip('。！!') not in {'重试','继续','再试一次','重新执行','按原口径继续'}:return None
    c=run.continuation or {}
    if c.get('bindings_changed') or not c.get('candidate_plan'):return None
    source=workflow.store.runs.get(c.get('source_run_id'))
    if not source or getattr(source.status,'value',source.status) not in {'failed','completed'}:return None
    if source.authorization!=run.authorization or source.ontology is None or source.ontology!=run.ontology:return None
    local_zone=timezone(timedelta(hours=8))
    if source.created_at.astimezone(local_zone).date()!=run.created_at.astimezone(local_zone).date():return None
    facts=lambda r:{f.question_id:(f.field,f.value) for f in r.user_answers}
    if facts(source)!=facts(run):return None
    preflight_retry=(not any(e.event=='plan.preflight.completed' for e in source.events)
        and any(e.event in {'agent.request.timeout','agent.request.failed'} and e.data.get('agent')=='reviewer' and e.data.get('phase')=='方案完整性预检' for e in source.events))
    execution_retry=(c.get('failed_stage')=='executing' and source.requirement is not None and source.plan is not None
        and source.result is None and source.review is not None and source.review.decision=='approved')
    completed_retry=(getattr(source.status,'value',source.status)=='completed' and source.requirement is not None
        and source.plan is not None and source.review is not None and source.review.decision=='approved')
    if not preflight_retry and not execution_retry and not completed_retry:return None
    recorded=(source.agent_memory.get('last_candidate') or [{}])[-1].get('plan')
    if (execution_retry or completed_retry) and recorded!=source.plan.model_dump(mode='json'):return None
    return deepcopy(recorded) if recorded and recorded==c['candidate_plan'] else None
