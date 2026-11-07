"""Owner control state and legacy payload migration. Never grants authority from prose."""
from semora_contracts import QueryRun,ReviewCase,ExecutionReceipt,ReviewFinding
from .task_memory import digest,binding
from .review_loop import issue_ledger


def review_input_hash(run):
    return digest({'run_id':run.id,'question':run.question,'ontology':run.ontology.model_dump(mode='json') if run.ontology else None,
                   'authorization':run.authorization.model_dump(mode='json') if run.authorization else None,
                   'answers':[f.model_dump(mode='json') for f in run.user_answers],
                   'confirmed_options':run.agent_memory.get('confirmed_fact_context',[])})


def preflight_case(run):
    if run.review_case is None:
        legacy=run.agent_memory.pop('approved_preflight',[])
        if legacy:
            value=legacy[-1]
            try:
                candidate=ReviewCase.model_validate({**value,'input_hash':review_input_hash(run)})
                if candidate.review.decision=='approved':run.review_case=candidate
            except ValueError:
                pass  # Optional cached proof is discarded; independent review still runs.
    run.agent_memory.pop('approved_preflight',None)
    return run.review_case


def record_preflight(run, plan, ontology, scope, decision, attempt, previous):
    pending=issue_ledger(decision,previous)
    for finding in decision.findings:
        item=next((i for i in pending if i['kind']==finding.kind and i['paths']==finding.paths
                   and i['message']==finding.message and i['acceptance']==finding.acceptance),None)
        if item:finding.id=item['id']
    prior=preflight_case(run)
    run.review_case=ReviewCase(
        **({'id':prior.id} if prior else {}), plan_hash=digest(plan.model_dump(mode='json')),
        binding=binding(run,ontology,scope),attempt=attempt,review=decision.model_copy(deep=True),
        pending=[ReviewFinding.model_validate(f) for f in pending],input_hash=review_input_hash(run))
    run.agent_memory.pop('approved_preflight',None)
    return pending


def execution_binding(run):
    if not run.execution_binding:
        legacy=run.agent_memory.pop('source_execution',[])
        if legacy:
            value=legacy[-1].get('binding')
            if not isinstance(value,str) or not value:
                raise ValueError('历史执行绑定无效，不能恢复中间结果')
            run.execution_binding=value
    run.agent_memory.pop('source_execution',None)
    return run.execution_binding


def record_receipt(run, payload, *, skipped=False):
    fields={k:v for k,v in payload.items() if k in ExecutionReceipt.model_fields}
    fields['status']='skipped' if skipped else 'completed'
    receipt=ExecutionReceipt.model_validate(fields)
    run.execution_receipts[receipt.step]=receipt
    return receipt


def restore_receipt(run, step):
    if step not in run.execution_receipts:
        event=next((e for e in reversed(run.events) if e.event in {'source.step.completed','source.step.skipped'} and e.data.get('step')==step),None)
        if event:
            record_receipt(run,{**event.data,'finished_at':event.data.get('finished_at') or event.created_at},skipped=event.event.endswith('skipped'))
    return run.execution_receipts.get(step)


def invalidate_execution(run):
    run.review_case=None
    run.execution_binding=''
    run.execution_receipts={}
    run.agent_memory.pop('approved_preflight',None)
    run.agent_memory.pop('source_execution',None)


def run_payload(run):
    """Private fields have one explicit persistence boundary, never API output."""
    return {**run.model_dump(mode='json'),'agent_memory':run.agent_memory,
            'source_results':{k:v.model_dump(mode='json') for k,v in run.source_results.items()},
            'review_case':run.review_case.model_dump(mode='json') if run.review_case else None,
            'execution_binding':run.execution_binding,
            'execution_receipts':{k:v.model_dump(mode='json') for k,v in run.execution_receipts.items()}}


def restore_run(payload):
    run=QueryRun.model_validate(payload)
    preflight_case(run)
    execution_binding(run)
    for step in run.source_results:
        restore_receipt(run,step)
    return run
