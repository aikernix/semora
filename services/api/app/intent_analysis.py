"""Intent-only Analyst protocol. The Owner owns compilation and review routing."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone

from semora_agent.model import TaskSupersededError

from .intent_planner import semantic_context
from .role_ports import IntentRolePorts
from .task_handoff import handoff, revision
from .task_memory import digest, binding


from semora_agent.prompts import analyst_prompt

INTENT_PROMPT = analyst_prompt()


async def analyse_intent(workflow, run, question, ontology, scope, feedback=None, *, ports: IntentRolePorts):
    from .prepared_context import prepare_context, confirmed_fact_context
    from .query_stages import validate_query_shape
    from .review_loop import capabilities, issue_ledger, route_findings, intent_repair_route, check_revision
    initial = revision(run)
    facts = confirmed_fact_context(workflow,run)
    toolkit = ports.create_tools(workflow,run,ontology,scope)
    toolkit.start_phase('业务意图分析')
    state = await prepare_context(workflow,run,question,ontology,scope)
    toolkit.context_state = state
    if revision(run) != initial:
        raise TaskSupersededError('需求在上下文准备期间已更新')
    prior = workflow._confirmed_history(run) if run.message_mode != 'new' and not run.continuation else []
    previous = (run.requirement.model_dump(mode='json') if run.requirement else
                (run.continuation or {}).get('confirmed_requirement') or
                (prior[-1]['confirmed_requirement'] if prior else None))
    draft = run.requirement_draft.model_dump(mode='json') if run.requirement_draft else (run.continuation or {}).get('draft') or {}
    answered = {f.question_id for f in run.user_answers}
    context = semantic_context({
        'protocol':'business_intent_v1', 'request':question, 'latest_user_request':run.question,
        'current_date':datetime.now(timezone(timedelta(hours=8))).date().isoformat(),
        'business_domain':{k:ontology.get(k) for k in ('domain','name','description')},
        'ontology_version':run.ontology.model_dump(mode='json') if run.ontology else None,
        'confirmed_fact_context':facts, 'previous_confirmed_requirement':previous,
        'previous_intent_candidate':(run.continuation or {}).get('candidate_intent'),
        'previous_planning_diagnostics':(run.continuation or {}).get('planning_diagnostics',[]),
        'open_business_questions':[q for q in draft.get('clarifications',[]) if q.get('id') not in answered],
        'information_assessment':{'status':'prepared'},
        'prepared_context':state['packet'], 'execution_capabilities':capabilities(),
        'evidence':[e.model_dump(mode='json') for e in run.evidence], 'review_feedback':feedback,
    })
    from .control_state import record_preflight
    pending = []
    previous_plan = None
    def build(value):
        resolved=toolkit.resolve_submission(value)
        outcome=workflow.analyst._build_outcome(resolved,question,run.evidence,ontology,scope,'intent-compiler-v1')
        if outcome.plan:
            validate_query_shape(outcome.plan,ontology)
        return outcome
    for attempt in range(3):
        from .delivery import best_effort,advisory,unavailable_review,RECOVERABLE_REVIEW_ERRORS
        task,toolkit.task_guard=handoff(workflow,run,'analyst','interpret_and_plan',context)
        toolkit.task_id=task['task_contract']['task_id']
        value,memory=await ports.runner(workflow.analyst.model,analyst_prompt(repairing=bool(pending or feedback),best_effort=best_effort(run)),task,toolkit.tools(),[],toolkit.event,
            build,ports.submission,tool_policy=toolkit.tool_policy,canonical_delivery=toolkit.canonical_delivery,
            progress_state=toolkit.progress_state,bounded_protocol=True,max_models=5,max_tools=4,task_guard=toolkit.task_guard)
        outcome=build(value)
        run.agent_memory['analyst']=memory
        run.agent_memory['last_candidate']=[{'requirement':outcome.requirement.model_dump(mode='json') if outcome.requirement else None,
                                            'plan':outcome.plan.model_dump(mode='json') if outcome.plan else None}]
        workflow.store.save_run(run)
        if not outcome.plan or not workflow._agentic_enabled('reviewer'):
            return outcome
        if previous_plan is not None and check_revision(previous_plan, outcome.plan, pending):
            unchanged=check_revision(previous_plan,outcome.plan,pending)
            if attempt==2:
                raise ValueError('连续修订仍未解决具体问题，已停止重复处理；已有需求和确认口径已保留。')
            workflow.emit(run,'planning.repair.retry',run.status,{
                'summary':'上一轮未修订指定问题，正在进行定向补正','unresolved_finding_ids':unchanged},'conversation_owner')
            context['unresolved_finding_ids']=unchanged
            toolkit.start_phase('业务意图定向补正')
            continue
        try:check=await ports.review(workflow,run,ontology,question,outcome,pending)
        except TaskSupersededError:raise
        except RECOVERABLE_REVIEW_ERRORS as exc:
            if not best_effort(run):raise
            check=unavailable_review(exc)
        if best_effort(run) and check.decision!='approved':
            check=advisory(workflow,run,check,'方案预检')
            record_preflight(run,outcome.plan,ontology,scope,check,attempt+1,[])
            workflow.store.save_run(run)
            return outcome
        record_preflight(run,outcome.plan,ontology,scope,check,attempt+1,pending)
        workflow.emit(run,'plan.preflight.completed',run.status,{'review':check.model_dump(mode='json'),'attempt':attempt+1},'reviewer')
        if check.decision=='approved':
            workflow.store.save_run(run)
            return outcome
        if check.decision=='revise' and intent_repair_route(check)=='technical':
            # Owner regenerates executable artifacts; Analyst never edits them.
            # One targeted diagnosis per round can withdraw a false alarm or
            # return a correctly located business repair instead of failing.
            from .intent_planner import compile_intent
            workflow.emit(run,'planning.repair.started',run.status,{
                'summary':'正在内部修复并核对复核指出的问题','attempt':attempt+1,
                'issues':check.issues},'conversation_owner')
            regenerated=compile_intent(outcome.plan.query_intent,ontology,scope)
            rebuilt=workflow.analyst._build_outcome(regenerated,question,run.evidence,ontology,scope,'owner-recompile')
            validate_query_shape(rebuilt.plan,ontology)
            run.agent_memory['owner_repair_diagnosis']={
                'original_review':check.model_dump(mode='json'),'operation':'recompile_same_business_intent',
                'business_intent_unchanged':rebuilt.plan.query_intent==outcome.plan.query_intent,
                'instruction':'核对程序已支持的执行契约；业务遗漏定位QueryIntent，不把技术拆步交给用户。'}
            try:
                check=await ports.review(workflow,run,ontology,question,rebuilt,[])
            except TaskSupersededError:raise
            except RECOVERABLE_REVIEW_ERRORS as exc:
                if not best_effort(run):raise
                check=advisory(workflow,run,unavailable_review(exc),'定向复核')
                record_preflight(run,rebuilt.plan,ontology,scope,check,attempt+1,[])
                workflow.store.save_run(run)
                return rebuilt
            finally:
                run.agent_memory.pop('owner_repair_diagnosis',None)
            outcome=rebuilt
            record_preflight(run,outcome.plan,ontology,scope,check,attempt+1,[])
            workflow.emit(run,'planning.repair.completed',run.status,{
                'summary':'内部修复复核已通过' if check.decision=='approved' else '已定位需要继续修订的问题',
                'attempt':attempt+1,'decision':check.decision,'issues':check.issues},'conversation_owner')
            if check.decision=='approved':
                workflow.store.save_run(run)
                return outcome
        routed=route_findings(check,outcome) if check.decision!='rejected' else None
        if routed is not None:
            return routed
        if check.decision=='rejected' or attempt==2:
            raise ValueError('业务查询意图暂未通过复核，已保留需求与确认口径，尚未执行查询。')
        if intent_repair_route(check)=='technical':
            workflow.emit(run,'planning.technical_gap',run.status,{'summary':'程序生成的方案存在具体执行结构问题，不转交业务Analyst重写','issues':check.issues},'conversation_owner')
            raise ValueError('已尝试内部重编译并定向复核，仍有执行能力缺口：'+'；'.join(check.issues)[:900]+'。需求已保留，尚未执行查询。')
        pending=issue_ledger(check,pending)
        previous_plan=outcome.plan.model_copy(deep=True)
        context['candidate_intent']=outcome.plan.query_intent.model_dump(mode='json')
        context['business_review_findings']=semantic_context(check.model_dump(mode='json'))
        context['revision_mode']='business_correction'
        toolkit.start_phase('业务意图修订')
    raise AssertionError('unreachable')
