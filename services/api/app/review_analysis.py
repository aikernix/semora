"""Isolated Reviewer operations; dependencies supplied by the application entrypoint."""
from __future__ import annotations

import re

from semora_agent.prompts import reviewer_prompt

from .review_loop import capabilities, validate_resolution
from .task_memory import binding as task_binding, digest as task_digest
from .task_handoff import handoff


from .role_contracts import review_submission

from .role_ports import ReviewRolePorts
from .review_budget import run_review
from .delivery import best_effort


async def review_plan(workflow, run, ontology, question, outcome, pending=None, *, ports: ReviewRolePorts):
    """Review business coverage before asking the user to confirm a proposal."""
    toolkit = ports.create_tools(workflow, run, ontology, {}, 'reviewer')
    toolkit.start_phase('方案完整性预检')
    context = {'business_domain': {key: ontology.get(key) for key in ('domain', 'name', 'description')},
               'user_request': question, 'requirement': outcome.requirement.model_dump(mode='json'),
               'plan': outcome.plan.model_dump(mode='json'),
               'confirmed_requirement': run.requirement.model_dump(mode='json') if run.requirement else (run.continuation or {}).get('confirmed_requirement'),
               'selected_catalog': selected_catalog(outcome.plan, ontology)}
    from .prepared_context import confirmed_fact_context
    context['confirmed_fact_context']=confirmed_fact_context(workflow,run)
    context['program_normalizations']=run.agent_memory.get('planning_normalizations',[])
    if toolkit.bounded:
        from .prepared_context import review_context
        toolkit.context_state=review_context(workflow,run,ontology,outcome.plan,{})
        context['prepared_context']=toolkit.context_state['packet']
    from .query_stages import validate_query_shape
    context['structural_checks'] = validate_query_shape(outcome.plan, ontology)
    context['effective_filters'] = [{'step': step.name, 'data_source_id': step.data_source_id, 'filters': [f.model_dump(mode='json') for f in step.query.filters]} for step in outcome.plan.source_steps] if outcome.plan.source_steps else [{'step': 'single', 'filters': [f.model_dump(mode='json') for f in outcome.plan.filters]}]
    context['execution_capabilities'] = capabilities()
    from semora_agent.constraints import missing_explained_filters
    context['explanation_filter_warnings']=[f.model_dump(mode='json') for f in missing_explained_filters(
        outcome.plan,outcome.requirement.assumptions+outcome.requirement.analysis_steps,ontology)]
    from .model_context import execution_contract
    context['result_output_contract']=execution_contract(outcome.plan)
    context['pending_findings'] = pending or []
    if outcome.plan.query_intent:
        from .model_context import intent_field_meanings
        context['intent_field_meanings']=intent_field_meanings(outcome.plan.query_intent)
    repair_diagnosis=run.agent_memory.get('owner_repair_diagnosis')
    if repair_diagnosis:context['owner_repair_diagnosis']=repair_diagnosis
    prompt = reviewer_prompt('plan_preflight',repairing=bool(pending or repair_diagnosis))
    from semora_agent.review_policy import repairable_paths
    paths=repairable_paths(outcome.plan)
    if outcome.plan.query_intent and not repair_diagnosis:
        from .intent_planner import semantic_context
        for key in ('result_output_contract','effective_filters','structural_checks'):
            context.pop(key,None)
        context['plan']=semantic_context(context['plan'])
        context['requirement']=semantic_context(context['requirement'])
        if context.get('prepared_context'):
            context['prepared_context']=semantic_context(context['prepared_context'])
    def validate_review(value):
        decision = workflow.reviewer._model_decision(value, 'pre_execution')
        if decision.decision != 'approved' and not decision.findings:
            raise ValueError('非通过决定必须有具体findings，定位执行字段、证据和验收条件；不能以未完成的一句话要求Analyst重做。')
        try:validate_resolution(decision, pending or [], outcome.plan)
        except ValueError as exc:
            workflow.emit(run,'review.validation.rejected',run.status,{'operation':'plan_preflight',
                'error':str(exc),'findings':[f.model_dump(mode='json') for f in decision.findings]},'reviewer')
            raise
    tools = [tool for tool in toolkit.tools() if tool.name != 'inspect_artifacts']
    context,toolkit.task_guard=handoff(workflow,run,'reviewer','plan_preflight',context)
    toolkit.task_id=context['task_contract']['task_id']
    try:
        value, _ = await run_review(run, 'plan_preflight', ports.runner, workflow.reviewer.model, prompt, context, tools, [], event=toolkit.event, legacy=not toolkit.bounded, single_submission=best_effort(run), can_finalize=lambda:toolkit.protocol_stage=='finalizing', revision_key=task_digest({'plan':context.get('plan'),'requirement':context.get('requirement')}),
                              validate=validate_review, submission_schema=review_submission(pending,paths), max_tool_argument_chars=65536,progress_state=toolkit.progress_state,tool_policy=toolkit.tool_policy,bounded_protocol=toolkit.bounded,task_guard=toolkit.task_guard)
    except Exception as exc:
        if hasattr(exc,'public_message'):
            exc.role='reviewer';exc.public_message='方案预检复核未能提交有效意见；需求与候选方案已保留，尚未执行查询。'
        raise
    return workflow.reviewer._model_decision(value, 'pre_execution')


async def review(workflow, run, ontology, phase='pre_execution', *, ports: ReviewRolePorts):
    toolkit = ports.create_tools(workflow, run, ontology, {}, 'reviewer')
    toolkit.start_phase('执行前复核' if phase == 'pre_execution' else '查询结果复核')
    context = {'effective_filters': [{'step': step.name, 'data_source_id': step.data_source_id, 'filters': [f.model_dump(mode='json') for f in step.query.filters]} for step in run.plan.source_steps] if run.plan.source_steps else [{'step': 'single', 'filters': [f.model_dump(mode='json') for f in run.plan.filters]}], 'phase': phase, 'business_domain': {key: ontology.get(key) for key in ('domain', 'name', 'description')},
               'execution_capabilities': capabilities(), 'user_request': workflow._analysis_question(run), 'requirement': run.requirement.model_dump(mode='json') if run.requirement else None,
               'plan': run.plan.model_dump(mode='json'), 'sql': run.sql.model_dump(mode='json') if run.sql else None,
               'gate': run.gate.model_dump(mode='json') if run.gate else None,
               'selected_catalog': selected_catalog(run.plan, ontology),
               'structural_checks': run.sql.compilation if run.sql else {},
               'evidence': [{**e.model_dump(mode='json'), 'summary': e.summary[:800]} for e in run.evidence[:12]]}
    from .model_context import execution_contract
    context['result_output_contract']=execution_contract(run.plan)
    if run.plan.query_intent:
        from .model_context import intent_field_meanings
        context['intent_field_meanings']=intent_field_meanings(run.plan.query_intent)
    if run.agent_memory.get('owner_repair_diagnosis'):
        context['owner_repair_diagnosis']=run.agent_memory['owner_repair_diagnosis']
    from .control_state import preflight_case,review_input_hash
    saved_case=preflight_case(run)
    preflight=saved_case.model_dump(mode='json') if saved_case and saved_case.review.decision=='approved' else {}
    from .prepared_context import confirmed_fact_context
    context['confirmed_fact_context']=confirmed_fact_context(workflow,run)
    context['program_normalizations']=run.agent_memory.get('planning_normalizations',[])
    if (preflight.get('plan_hash') == task_digest(run.plan.model_dump(mode='json'))
        and preflight.get('binding') == task_binding(run, ontology, run.plan.data_scope)
        and preflight.get('input_hash') == review_input_hash(run)):
        context['verified_preflight'] = preflight['review']
        context['preflight_status']='approved_for_same_input_version'
    if phase == 'post_execution':
        context['result'] = {**run.result.model_dump(mode='json'), 'rows': run.result.rows[:20], 'sample_rows': min(20, len(run.result.rows))}
        context['source_execution_receipts']=[{'step':step.name,'row_count':run.source_results[step.name].row_count,
            'duration_ms':run.source_results[step.name].duration_ms,'truncated':run.source_results[step.name].truncated}
            for step in run.plan.source_steps if step.name in run.source_results]
    if run.plan.source_steps:
        context['effective_step_scopes']=[{'step':s.name,'filters':[f.model_dump(mode='json') for f in s.query.filters],
            'time_range':s.query.time_range,'runtime_key_bindings':[b.model_dump(mode='json') for b in s.inputs]} for s in run.plan.source_steps]
        # Only the final, already authorized result sample may reach the LLM.
        # Intermediate key lists are operational inputs, never model context.
        context['sql'] = {**context['sql'], 'parameters': {'count': len(run.sql.parameters), 'values': '程序绑定的关联参数，不提供中间键列表'}}
        context['source_artifacts'] = {name: {**a.model_dump(mode='json'), 'parameters': {'count': len(a.parameters), 'values': '运行时关联参数'}}
                                       for name, a in run.source_artifacts.items()}
    if toolkit.bounded:
        from .prepared_context import review_context
        toolkit.context_state=review_context(workflow,run,ontology,run.plan,{})
        context['prepared_context']=toolkit.context_state['packet']
    operation='result_review' if phase=='post_execution' else 'execution_review'
    review_prompt=reviewer_prompt(operation,repairing=bool(context.get('owner_repair_diagnosis')))
    def validate_execution_review(value):
        decision=workflow.reviewer._model_decision(value,phase)
        if run.plan.query_intent and decision.decision!='approved' and not decision.findings:
            raise ValueError('非通过决定必须有具体findings、paths、证据和验收条件。请先核对result_output_contract；执行器补齐的来源列不应要求写入目标源SQL。')
    tools = [tool for tool in toolkit.tools() if tool.name != 'inspect_artifacts']
    context,toolkit.task_guard=handoff(workflow,run,'reviewer','result_review' if phase=='post_execution' else 'execution_review',context)
    toolkit.task_id=context['task_contract']['task_id']
    try:
        value, _ = await run_review(run, operation, ports.runner, workflow.reviewer.model, review_prompt, context, tools, [], event=toolkit.event, legacy=not toolkit.bounded, single_submission=best_effort(run), can_finalize=lambda:toolkit.protocol_stage=='finalizing', revision_key=task_digest({'plan':context.get('plan'),'requirement':context.get('requirement')}), validate=validate_execution_review, submission_schema=review_submission(), max_tool_argument_chars=65536,progress_state=toolkit.progress_state,tool_policy=toolkit.tool_policy,bounded_protocol=toolkit.bounded,task_guard=toolkit.task_guard)
    finally:
        run.agent_memory.pop('owner_repair_diagnosis',None)
    return workflow.reviewer._model_decision(value, phase)


def selected_catalog(plan, ontology):
    """Relevant business metadata only; no full schema/scan or repeated artifacts."""
    if plan.source_steps:
        from semora_query_core.sources import source_ontology
        from semora_contracts import QueryPlan
        used = {b.relation for s in plan.source_steps for b in s.inputs}
        return {'sources': {s.name: selected_catalog(QueryPlan.model_validate(s.query.model_dump(mode='json')), source_ontology(ontology, s.data_source_id)) for s in plan.source_steps},
                'cross_source_relations': [r for r in ontology.get('relations', []) if r['name'] in used]}
    refs = set(plan.dimensions + plan.fields + [f.field for f in plan.filters])
    refs.update(c.field for c in plan.calculations if c.field)
    for stage in plan.stages:
        refs.update(stage.group_by)
        refs.update(m.field for m in stage.measures if m.field)
        refs.update(f.field for f in stage.filters)
        refs.update(f.field for m in stage.measures for f in m.filters)
    metrics = [m for m in ontology.get('metrics', []) if m['name'] in plan.metrics]
    for metric in metrics:
        refs.update(re.findall(r'\b[A-Za-z_]\w*\.[A-Za-z_]\w*\b', metric.get('expression', '')))
    relations = [r for r in ontology.get('relations', []) if r['name'] in plan.relationship_path]
    objects = {ref.split('.', 1)[0] for ref in refs} | {s.source for s in plan.stages}
    objects.update(obj for r in relations for obj in (r['from'], r['to']))
    return {'objects': [{'name': o['name'], 'label': o.get('label'), 'table': o['table'], 'primary_key': o.get('primaryKey'),
                         'fields': [{k: p.get(k) for k in ('name', 'label', 'column', 'type', 'sensitive')} for p in o.get('properties', []) if f"{o['name']}.{p['name']}" in refs]}
                        for o in ontology.get('objects', []) if o['name'] in objects],
            'relations': [{k: r.get(k) for k in ('name', 'label', 'from', 'to', 'condition', 'cardinality', 'optional')} for r in relations],
            'metrics': [{k: m.get(k) for k in ('name', 'label', 'expression', 'grain', 'format')} for m in metrics]}
