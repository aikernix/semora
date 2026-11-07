"""Owner-built role context. No model summaries become authoritative facts."""
from copy import deepcopy
import json
import os
from uuid import uuid4

from semora_agent.model import ModelError, TaskSupersededError
from semora_agent.task_contracts import TaskContract, TaskLease
from .task_memory import digest


class ContextNotReady(ModelError):
    def __init__(self, missing):
        self.missing=missing
        self.public_message='任务上下文尚未就绪：'+'；'.join(missing)+'。已保留需求，系统需补齐资料后再分析。'
        super().__init__(self.public_message)


def revision(run):
    # Operational events, new evidence and private drafts are deliberately not
    # authoritative user changes. They must not invalidate their own tool call.
    return digest({'question':run.question,'domain':run.domain,
        'answers':[f.model_dump(mode='json') for f in run.user_answers],
        'authorization':run.authorization.model_dump(mode='json') if run.authorization else None,
        'ontology':run.ontology.model_dump(mode='json') if run.ontology else None,
        'requirement':run.requirement.model_dump(mode='json') if run.requirement else None,
        'plan':run.plan.model_dump(mode='json') if run.plan else None,
        'result_hash':digest(run.result.model_dump(mode='json')) if run.result else None})


def size(value):
    return len(json.dumps(value,ensure_ascii=False,separators=(',',':'),default=str).encode())


def project_context(context,role):
    """Remove duplicates/physical scans, never shorten facts or SQL clauses."""
    result=deepcopy(context)
    for key in ('agent_memory','analyst_messages','reviewer_messages','private_memory','messages','conversation_history'):
        result.pop(key,None)
    packet=result.get('prepared_context')
    if packet:
        packet.pop('rule',None)
        packet.pop('coverage_check',None)
        schemas=packet.pop('schemas',None)
        if schemas is not None:
            packet['scan_status']=[{'object':s.get('object'),'scanned':s.get('scanned',False)} for s in schemas]
        # Complete semantic definitions, relationships, source layout and
        # exact stage/transfer contracts remain in the packet once.
        result.pop('selected_catalog',None)
        if role=='analyst':
            result.pop('objects',None)
            result.pop('metrics',None)
    memory=result.get('task_memory')
    if memory:
        # Original messages and all facts are already in request/fact_context.
        # Keep the previous execution contract and unresolved conflicts intact.
        root_facts=result.get('confirmed_fact_context') or []
        if memory.get('facts') and all(any(all(f.get(k)==v for k,v in item.get('fact',{}).items()) for f in root_facts) for item in memory['facts']):
            memory.pop('facts',None)
        if memory.get('request_history'):
            request=result.get('request') or result.get('user_request') or ''
            memory['request_history']=list(dict.fromkeys(m for m in memory['request_history'] if m not in request))
        memory.pop('current_request',None)
        if memory.get('evidence') and all(not e.get('requires_refresh') and e.get('reference') in (result.get('evidence') or []) for e in memory['evidence']):
            memory.pop('evidence',None)
    if role=='reviewer':
        if packet:
            # The Reviewer checks actual artifacts and published semantics,
            # not instructions for constructing hypothetical execution steps.
            packet.pop('source_layout',None)
            packet.pop('transfer_templates',None)
            for entry in packet.get('objects',[]):
                entry.pop('stage_group_keys',None)
                entry.pop('stage_join_contracts',None)
        for key in ('continuation','task_memory','rejected_plan','rejected_sql'):
            result.pop(key,None)
        # A cross-source final SQL is already present in source_artifacts.
        sql=result.get('sql')
        artifacts=result.get('source_artifacts') or {}
        if sql and sql.get('artifact_hash') and artifacts:
            match=next((name for name,a in artifacts.items() if a.get('artifact_hash')==sql.get('artifact_hash')
                and a.get('physical_sql')==sql.get('physical_sql')),None)
            if match:result['sql']={'artifact_ref':'source_artifacts.'+match,'artifact_hash':sql['artifact_hash']}
    return result


def readiness(run,role,operation,context):
    missing=[]
    if not (context.get('request') or context.get('user_request')):missing.append('缺少当前分析请求')
    facts=context.get('confirmed_fact_context') or []
    for fact in run.user_answers:
        if not any(all(item.get(k)==getattr(fact,k) for k in ('question_id','field','value','label')) for item in facts):
            missing.append('未完整交接用户确认项：'+fact.question_id)
    known=run.agent_memory.get('confirmed_fact_context') or []
    for fact in known:
        if fact.get('selected_option_description') and not any(
            all(item.get(k)==fact.get(k) for k in ('question_id','field','value','selected_option_description')) for item in facts):
            missing.append('遗漏确认选项的完整含义：'+fact.get('question_id','unknown'))
    packet=context.get('prepared_context')
    if packet is not None:
        objects=packet.get('objects') or []
        if not objects:missing.append('缺少已授权的相关本体对象')
        names={o.get('object',{}).get('name') for o in objects}
        if not set(packet.get('selected_objects') or [])<=names:missing.append('本体对象目录与已交接定义不一致')
        for entry in objects:
            obj=entry.get('object') or {}
            refs={obj.get('name','')+'.'+p['name'] for p in obj.get('properties',[])}
            if not refs<=set(entry.get('field_refs') or []):missing.append('缺少对象字段契约：'+obj.get('name','unknown'))
        for relation in packet.get('relations') or []:
            if not {relation.get('from'),relation.get('to')}<=names:missing.append('关联路径缺少必要的对象定义：'+relation.get('name','unknown'))
    if role=='reviewer':
        plan=context.get('plan')
        if not plan:missing.append('缺少当前候选方案')
        if operation!='plan_preflight':
            if not context.get('sql'):missing.append('缺少当前SQL产物')
            steps=(plan or {}).get('source_steps') or []
            if steps and set(context.get('source_artifacts') or {})!={s['name'] for s in steps}:
                missing.append('跨源执行产物未完整交接')
        if operation=='result_review' and context.get('result') is None:missing.append('缺少执行结果')
        if operation=='result_review' and (plan or {}).get('source_steps'):
            receipts=context.get('source_execution_receipts') or []
            if {s['name'] for s in plan['source_steps']}!={r.get('step') for r in receipts}:
                missing.append('缺少各源实际执行行数，不能用最终行数推断前置步骤结果')
    return missing


def handoff(workflow,run,role,operation,context):
    projected=project_context(context,role)
    if run.question not in (projected.get('request'),projected.get('user_request'),projected.get('latest_user_request')):
        projected['original_user_request']=run.question
    if context.get('protocol')=='business_intent_v1' or role=='reviewer':
        from .model_context import project_knowledge
        projected=project_knowledge(projected,run)
    missing=readiness(run,role,operation,projected)
    if missing:
        workflow.emit(run,'task.context.missing',run.status,{'role':role,'operation':operation,'missing':missing},'conversation_owner')
        raise ContextNotReady(missing)
    from .context_encoding import encode_context
    projected=encode_context(projected)
    frozen=revision(run)
    initial_terminal=getattr(run.status,'value',run.status) in {'failed','completed'}
    task_id=str(uuid4())
    goals={'interpret_and_plan':'只处理当前查数需求与必要的方案修订，不扩展目标',
        'plan_preflight':'独立核对当前候选与用户需求及确认口径是否一致',
        'execution_review':'独立核对当前执行产物是否忠实于已确认口径',
        'result_review':'核对当前结果和实际执行产物，不推断未提供的数据'}
    contract={'task_id':task_id,'role':role,'operation':operation,'objective':goals[operation],
        'input_revision':frozen,'run_user_message':run.question,'current_request_ref':'request' if 'request' in projected else 'user_request',
        'bindings':{'domain':run.domain,'ontology':run.ontology.model_dump(mode='json') if run.ontology else None,
                    'authorization_hash':digest(run.authorization.model_dump(mode='json') if run.authorization else None)},
        'must_preserve_fact_refs':[digest(f) for f in projected.get('confirmed_fact_context') or []],
        'context_hash':digest(projected),'acceptance':['保留完整用户口径','只使用本任务相关语义和授权证据','返回当前工具协议允许的结构化产物'],
        'missing_context_action':'缺核心对象时request_context并说明用途；技术缺口不能伪装成业务歧义；辅助知识无命中不阻断',
        'memory_policy':'事实和当前版本产物是依据；不继承其他角色私有对话。当前用户明确修改优先于未修改前的历史条件。'}
    contract['max_request_bytes']=int(os.getenv('AGENT_'+role.upper()+'_CONTEXT_MAX_BYTES','131072'))
    if contract['max_request_bytes']<=0:raise ValueError('角色上下文预算必须大于0')
    contract['effective_authorization']={k:v for k,v in (run.authorization.model_dump(mode='json') if run.authorization else {}).items()
        if k in {'allowed_data_source_ids','sensitivity_clearance','branch_ids','cross_organization','policy_version'}}
    contract=TaskContract.model_validate(contract).model_dump(mode='json')
    saved=run.agent_memory.setdefault('task_contracts',[])
    saved.append(contract)
    del saved[:-12]
    projected['task_contract']={k:contract[k] for k in (
        'task_id','role','operation','objective','max_request_bytes','effective_authorization')}
    summary={'task_id':task_id,'role':role,'operation':operation,'input_revision':frozen,
        'context_hash':contract['context_hash'],'fact_count':len(projected.get('confirmed_fact_context') or []),
        'original_context_bytes':size(context),'context_bytes':size(projected),'max_request_bytes':contract['max_request_bytes'],'ready':True}
    summary['section_bytes']={k:size(v) for k,v in projected.items()}
    manifests=run.agent_memory.setdefault('task_handoffs',[])
    manifests.append(summary)
    del manifests[:-12]
    workflow.emit(run,'task.context.ready',run.status,{**summary,'summary':'已交接当前目标、完整确认口径及角色必需资料'},'conversation_owner')
    lease=TaskLease(frozen,initial_terminal)
    def guard():
        now_terminal=getattr(run.status,'value',run.status) in {'failed','completed'}
        if not lease.matches(revision(run),now_terminal):
            raise TaskSupersededError('任务输入、权限或执行版本已改变，已丢弃旧任务的迟到结果')
    return projected,guard
