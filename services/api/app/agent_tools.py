"""Scoped role tools. No conversation ownership or model orchestration."""
from __future__ import annotations

import json
import os
from copy import deepcopy
from uuid import uuid4
import re
from types import SimpleNamespace
from functools import wraps
from urllib.parse import quote
from typing import Annotated, Literal

from langchain_core.tools import StructuredTool
from semora_agent.candidates import CandidateRegistry
from semora_agent.task_contracts import ArtifactBinding
from semora_agent.model import PlanValidationError,AnalysisBlockedError,TaskSupersededError
from semora_contracts import QueryIntent, QueryFilter, KnowledgeResearchResult, KnowledgeResearchStep
from pydantic import BaseModel, Field, ValidationError, create_model

from .review_loop import check_revision, execution_view
from .task_memory import read_artifact
from .datasources import datasource_service
from .knowledge import knowledge_service
from .agent_context import compact_definition, compact_observation


from .role_contracts import AnalysisPayload, CompactDelivery, require_plan_controls, constrain_source_ids, CandidateDiagnostics, PlanPatch

class AgentTools:
    def __init__(self, workflow, run, ontology, scope, role='analyst', *, knowledge_provider=None):
        self.workflow, self.run, self.ontology, self.scope, self.role = workflow, run, ontology, scope, role
        self.knowledge_provider = knowledge_provider if knowledge_provider is not None else knowledge_service
        self.objects = {item['name']: item for item in ontology.get('objects', [])}
        self.phase = ''
        self.phase_id = None
        self.iteration = None
        self.role_iteration = None
        self.seen_tools = set()
        self.bounded=os.getenv('AGENT_BOUNDED_CONTEXT','false').lower()=='true'
        from .intent_planner import enabled as intent_enabled
        self.intent_mode = self.role == 'analyst' and intent_enabled()
        self.context_state=None
        key=self.role+'_progress'
        self.progress_state=(self.run.agent_memory.setdefault(key,[{}]))[-1]
        if self.bounded:
            from .prepared_context import input_binding
            current=input_binding(run,ontology,scope)
            if self.progress_state.get('input_binding')!=current:
                self.progress_state.clear()
                self.progress_state['input_binding']=current
        self.registry = CandidateRegistry()
        self.patch_only=False
        self.allowed_patch_paths=None
        self.previous_validated_plan = None
        self.protocol_stage = 'exploring'
        self.revision_base = None
        self.pending_findings = []
        self.intent_repair_only=False
        self.intent_repair_base=None
        self.intent_repair_paths=[]
        self.staged = self.role == 'analyst' and (self.bounded or os.getenv('AGENT_STAGED_PROTOCOL', 'false').lower() == 'true')

    def start_phase(self, label):
        self.protocol_stage = 'planning' if self.bounded else 'exploring'
        self.registry.clear()
        self.patch_only=False
        self.allowed_patch_paths=None
        self.intent_repair_only=False
        self.intent_repair_base=None
        self.intent_repair_paths=[]
        self.phase = label
        self.phase_id = 1 + sum(e.event == 'agent.phase.started' for e in self.run.events)
        self.event('agent.phase.started', {'summary': f'进入{label}阶段；任务轮次继续累计'})

    def tool_policy(self, tools):
        if not self.staged and not self.bounded and not self.intent_mode:
            return tools
        if self.intent_mode:
            allowed={'AgentSubmission','request_context','read_evidence','read_task_artifact'}
            if self.protocol_stage not in {'delivering','finalizing'}:allowed.add('validate_intent')
            if self.intent_repair_only:allowed&={'AgentSubmission','validate_intent'}
            if self.protocol_stage in {'delivering','finalizing'}:allowed={'AgentSubmission'}
        elif self.bounded:
            allowed={'AgentSubmission','request_context'}
            if self.role=='analyst' and self.protocol_stage=='planning':
                if not self.patch_only:allowed.add('validate_plan')
                if self.invalid_candidate:allowed.add('patch_plan')
            if self.protocol_stage in {'delivering','finalizing'}:allowed={'AgentSubmission'}
        else:
            allowed = {'AgentSubmission', 'enter_planning'} if self.protocol_stage == 'exploring' else {'AgentSubmission', 'validate_plan', 'resume_exploration'}
        if not self.bounded and self.protocol_stage == 'exploring':
            allowed.update(t['function']['name'] for t in tools if t['function']['name'] not in {'validate_plan', 'resume_exploration'})
        if not self.bounded and self.protocol_stage == 'delivering':
            allowed = {'AgentSubmission', 'resume_exploration'}
        selected = []
        for tool in tools:
            if tool['function']['name'] not in allowed:
                continue
            item = deepcopy(tool)
            if item['function']['name'] == 'validate_plan':
                schema=item['function']['parameters']['properties']['analysis']
                require_plan_controls(schema)
                if self.bounded:
                    from .prepared_context import authorized_objects
                    source_ids=sorted({o.get('dataSourceId') or self.ontology.get('dataSourceId') or '' for o in authorized_objects(self.run,self.ontology).values()})
                    constrain_source_ids(schema,source_ids)
            if item['function']['name'] == 'AgentSubmission' and self.role=='analyst':
                # Only change outbound schema; server still validates against
                # the full contract and the bound canonical candidate.
                schema = create_model('CompactSubmission', payload=(CompactDelivery, ...)).model_json_schema()
                valid_ids=self.registry.valid_ids(self.artifact_binding())
                schema['$defs']['CompactDelivery']['properties']['candidate_id']={'enum':[None,*valid_ids], 'description':'只能引用本阶段校验通过的候选ID；没有通过的候选时只能为null，交付具体受阻原因或真实业务澄清。'}
                item['function']['parameters'] = schema
            selected.append(item)
        return selected

    def canonical_delivery(self, messages):
        if not (self.staged or self.bounded or self.intent_mode) or self.protocol_stage not in {'delivering','finalizing'} or not messages:
            return None
        last = messages[-1]
        if getattr(last, 'type', None) != 'tool' or getattr(last, 'name', None) not in {'validate_plan','patch_plan','validate_intent'}:
            return None
        try:
            receipt = json.loads(last.content)
        except (ValueError, TypeError):
            return None
        if (self.intent_mode and self.protocol_stage=='finalizing' and last.name=='validate_intent'
            and receipt.get('valid') is False and receipt.get('kind') in {'capability','configuration','compiler_validation'}):
            return {'blocked_reason':receipt['message']}
        candidate_id = receipt.get('candidate_id')
        if receipt.get('valid') is True and self.registry.valid(candidate_id,self.artifact_binding()):
            return {'candidate_id': candidate_id}
        return None

    def artifact_binding(self):
        return ArtifactBinding.capture(run_id=self.run.id,session_id=self.run.session_id,domain=self.run.domain,
            phase=self.phase_id,question=self.run.question,ontology=self.ontology,scope=self.scope,
            requirement=self.run.requirement.model_dump(mode='json') if self.run.requirement else None,
            authorization=self.run.authorization.model_dump(mode='json') if self.run.authorization else None,
            facts={'answers':[f.model_dump(mode='json') for f in self.run.user_answers],
                   'confirmed_options':self.run.agent_memory.get('confirmed_fact_context',[])})

    def candidate_binding(self):
        return self.artifact_binding().fingerprint

    @property
    def candidates(self):
        """Compatibility observation only; mutation cannot change authority."""
        return self.registry.snapshot()

    @property
    def invalid_candidate(self):
        return self.registry.invalid(self.artifact_binding())

    @invalid_candidate.setter
    def invalid_candidate(self,value):
        if value is None:
            self.registry.clear_invalid()
        else:
            if value['binding']!=self.candidate_binding():
                raise ValueError('Invalid candidate binding is stale')
            self.registry.set_invalid(value['analysis'],self.artifact_binding(),value['id'])

    def resolve_submission(self, value):
        if value.get('blocked_reason'):
            if any(bool(v) for k,v in value.items() if k not in {'blocked_reason','summary'}):
                raise ValueError('受阻说明不能与候选方案或澄清混用')
            self.event('analysis.blocked',{'summary':value['blocked_reason'][:1000]})
            raise AnalysisBlockedError(value['blocked_reason'])
        candidate_id = value.get('candidate_id')
        if (self.staged or self.intent_mode) and not candidate_id and not value.get('clarifications'):
            raise ValueError('请先校验候选方案并通过candidate_id交付；信息不明确请提交clarifications')
        if not candidate_id:
            return {k: v for k, v in value.items() if k not in {'candidate_id','blocked_reason'}}
        if any(v is not None for k, v in value.items() if k != 'candidate_id' and not (self.staged and k == 'summary')):
            raise ValueError('candidate_id不能与方案字段或澄清混用；修改方案请重新调用validate_plan')
        return self.registry.resolve(candidate_id,self.artifact_binding())

    def event(self, name, data):
        # A public operational trace, never raw model messages or credentials.
        data = dict(data)
        if getattr(self,'task_id',None):data['task_id']=self.task_id
        if name == 'agent.model.started':
            previous = self.run.continuation or {}
            self.iteration = 1 + previous.get('model_round_offset', 0) + sum(e.event == name for e in self.run.events)
            self.role_iteration = 1 + previous.get('role_rounds', {}).get(self.role, 0) + sum(e.event == name and e.data.get('agent') == self.role for e in self.run.events)
            data['local_iteration'] = data.get('iteration')
            data['summary'] = f'任务第 {self.iteration} 轮：{self.phase or self.role}，核验需求与证据并选择下一步'
        if self.iteration is not None:
            if name == 'agent.model.completed':
                data['local_iteration'] = data.get('iteration')
            data.update(iteration=self.iteration, role_iteration=self.role_iteration)
        self.workflow.emit(self.run, name, self.run.status, {**data, 'agent': self.role, 'phase': self.phase, 'phase_id': self.phase_id}, self.role)

    @staticmethod
    def _public_observation(tool, result):
        """Small verifiable facts, not raw tool payloads or model-private text."""
        details = {}
        if tool == 'search_wiki':
            details = {k: result[k] for k in ('keyword', 'count', 'limit', 'possibly_truncated') if k in result}
            details['pages'] = [{'title': p.get('title'), 'slug': p.get('slug')} for p in result.get('pages', [])[:8]]
        elif tool == 'read_object' and result.get('object'):
            obj = result['object']
            details = {'object': obj.get('name'), 'label': obj.get('label'), 'field_count': len(obj.get('properties', [])), 'relations': [r.get('name') for r in result.get('relations', [])]}
        elif tool == 'find_relation_path':
            details = {'path': result.get('path', []), 'relations': [r.get('name') for r in result.get('relations', [])]}
        elif tool == 'read_schema':
            details = {'tables': [{'name': t.get('name'), 'column_count': len(t.get('columns', []))} for t in result.get('tables', [])]}
        elif tool == 'read_wiki' and result.get('page'):
            page = result['page']
            details = {'title': page.get('title'), 'slug': page.get('slug'), 'excerpt': str(page.get('content') or '')[:800], 'links': page.get('links', [])[:15], 'source_refs': page.get('source_refs', [])[:15]}
        elif tool == 'read_source':
            details = {'chunks': [{'id': c.get('id'), 'excerpt': str(c.get('content') or '')[:500]} for c in result.get('chunks', [])[:3]]}
        elif tool == 'read_evidence':
            details = {'evidence': [{'title': e.get('title'), 'locator': e.get('locator'), 'revision': e.get('revision')} for e in result.get('evidence', [])]}
        elif tool in {'validate_plan','validate_intent'}:
            details = {k: result[k] for k in ('valid', 'candidate_id', 'errors', 'repeat_count') if k in result}
            plan = result.get('plan_summary') or result.get('plan') or {}
            details.update({k: plan[k] for k in ('metrics', 'dimensions', 'grain', 'relationship_path') if k in plan})
        return details

    def _tool(self, function):
        @wraps(function)
        async def invoke(*args, **kwargs):
            if getattr(self,'task_guard',None):self.task_guard()
            goal = kwargs.pop('analysis_goal', '')
            # Only documented lookup parameters appear in the public trace.
            lookup = {k: str(v)[:200] for k, v in kwargs.items() if k in {'query', 'name', 'object_name', 'source', 'target', 'slug', 'document_id'}}
            self.event('agent.tool.started', {'tool': function.__name__, 'summary': f'调用 {function.__name__}', 'analysis_goal': goal, 'lookup': lookup})
            signature = (function.__name__, json.dumps(kwargs, sort_keys=True, default=str))
            if self.role == 'reviewer' and signature in self.seen_tools:
                result = {'available': True, 'unchanged': True, 'message': '本阶段已返回相同工具观察，内容未变化。请使用已有证据作出决定，不要重复读取。'}
                self.event('agent.tool.completed', {'tool': function.__name__, 'summary': result['message'], 'available': True, 'observation': {'reused': True}})
                return json.dumps(result, ensure_ascii=False)
            self.seen_tools.add(signature)
            try:
                result = await function(*args, **kwargs)
                if getattr(self,'task_guard',None):self.task_guard()
            except (PlanValidationError,TaskSupersededError):
                raise
            except Exception as exc:
                # Do not expose HTTP headers, URLs or database connection strings.
                result = {'available': False, 'error': type(exc).__name__, 'message': '工具暂不可用；辅助知识可以跳过，必需数据缺失则说明缺口'}
            if function.__name__=='request_context':
                from .context_encoding import encode_context
                result=encode_context(result)
            self.event('agent.tool.completed', {'tool': function.__name__, 'summary': str(result.get('message') or f'{function.__name__} 已返回核验数据'), 'available': result.get('available', True), 'observation': self._public_observation(function.__name__, result)})
            return json.dumps(result, ensure_ascii=False, default=str)
        tool = StructuredTool.from_function(coroutine=invoke)
        required_review={}
        if self.role=='reviewer' and function.__name__=='request_context':
            required_review={'field_refs':(list[str],Field(...,min_length=1,max_length=16,description='缺失的对象.字段引用')),
                             'acceptance':(str,Field(...,min_length=1,max_length=500,description='补齐后核验的具体条件'))}
        tool.args_schema = create_model(f'{function.__name__}Input', __base__=tool.args_schema,
                                       analysis_goal=(str, Field(default='', max_length=300, description='一句话说明本次核验什么业务问题或证据缺口；不填隐藏思维链。')),**required_review)
        tool.args_schema.model_config = {**tool.args_schema.model_config, 'extra': 'forbid'}
        tool.args_schema.model_rebuild(force=True)
        def invalid_arguments(error):
            errors = [{'path': '.'.join(map(str, item['loc'])), 'type': item['type'],'message':item['msg']} for item in error.errors(include_input=False)][:8]
            message = '工具参数结构错误：'+'；'.join(e['path']+': '+e['message'] for e in errors)
            if any(e['path']=='analysis' and e['type']=='missing' for e in errors):
                message+='。validate_plan必须把source_steps等方案字段放入analysis对象；不要放在工具参数根层。'
            self.event('agent.validation.failed', {'summary': message, 'tool': function.__name__, 'errors': errors})
            return json.dumps({'valid': False, 'message': message, 'errors': errors}, ensure_ascii=False)
        tool.handle_validation_error = invalid_arguments
        return tool

    def tools(self):
        async def read_object(name: str) -> dict:
            """读取对象全部字段与直接关联目录；具体关联条件用find_relation_path按路径读取。"""
            if name not in self.objects:
                return {'available': False, 'message': '对象不在当前业务域，请从给定对象目录选择'}
            from .query_stages import stage_group_keys
            obj = compact_definition(self.objects[name])
            relations = [r for r in self.ontology.get('relations', []) if name in (r['from'], r['to'])]
            relations = [compact_definition(r) for r in relations]
            return compact_observation({'object': obj,
                    'field_refs': [f"{name}.{prop['name']}" for prop in self.objects[name].get('properties', [])],
                    'stage_group_keys': stage_group_keys(name, self.ontology),
                    'field_reference_rule': '执行方案只使用field_refs；属性label和column是说明，不能替代属性name。',
                    'relations': relations, 'metrics': [compact_definition(m) for m in self.ontology.get('metrics', [])]})

        async def find_relation_path(source: str, target: str) -> dict:
            """核验两个本体对象的关联路径，同时返回可能存在不同业务角色的关联。"""
            if source not in self.objects or target not in self.objects:
                return {'available': False, 'message': '不能查询当前本体之外的对象'}
            relations = self.ontology.get('relations', [])
            try:
                names = self.workflow.analyst._connect_objects({source, target}, relations)
            except ValueError:
                return {'available': False, 'message': '对象之间缺少已配置关联，需要补充本体映射'}
            return {'path': names, 'relations': [compact_definition(r) for r in relations if r['name'] in names or (r['from'] in (source, target) and r['to'] in (source, target))], 'message': '路径可连通；存在多个业务关系角色时仍需明确口径。跨表字段在阶段关联后使用，不能放入单源预聚合的group_by。'}

        async def read_schema(object_name: str) -> dict:
            """读取当前本体对象对应的已扫描数据库表结构，不返回数据行和连接凭据。"""
            if object_name not in self.objects:
                return {'available': False, 'message': '对象不在当前业务域'}
            source = datasource_service.repository.get(self.objects[object_name].get('dataSourceId') or self.ontology['dataSourceId'])
            table = self.objects[object_name].get('table', '').split('.')[-1]
            rows = source.scan_result.get('tables') or []
            found = [r for r in rows if str(r.get('name') or r.get('table_name') or '').split('.')[-1] == table]
            return {'tables': found, 'message': '返回已扫描结构；未命中表示需在数据源页面重新扫描' if not found else '已取得对应物理表结构'}

        async def validate_plan(analysis: AnalysisPayload) -> dict:
            """校验完整候选方案；成功返回candidate_id供最终提交，不生成或执行SQL。"""
            value = analysis.model_dump(mode='json') if isinstance(analysis, BaseModel) else AnalysisPayload.model_validate(analysis).model_dump(mode='json')
            try:
                if self.staged and not {'order_by','limit'} <= analysis.model_fields_set:
                    raise ValueError('完整执行方案必须显式填写analysis.order_by和analysis.limit；排序用数组，不排序用[]，不设业务TopN用null。不能只写入analysis_steps。')
                if self.staged:
                    for i,step in enumerate(analysis.source_steps or []):
                        if not {'order_by','limit'} <= step.query.model_fields_set:
                            raise ValueError(f'source_steps[{i}].query必须显式填写order_by和limit；无排序用[]，无业务TopN用null。')
                if self.bounded:
                    from .prepared_context import source_diagnostics
                    problems=source_diagnostics(self.run,self.ontology,analysis.source_steps or [])
                    if problems:raise CandidateDiagnostics(problems)
                outcome = self.workflow.analyst._build_outcome({**value, **getattr(self, '_intent_metadata', {})}, self.run.question, self.run.evidence, self.ontology, self.scope, 'llm')
                if outcome.plan is None:
                    return {'valid': False, 'message': '方案还有待确认的业务口径，应先向用户澄清'}
                from .query_stages import validate_query_shape
                checks = validate_query_shape(outcome.plan, self.ontology)
                if self.bounded:
                    from .prepared_context import business_contract_diagnostics
                    steps=outcome.plan.source_steps or [SimpleNamespace(name='single',query=outcome.plan)]
                    problems=business_contract_diagnostics(self.run,self.ontology,steps)
                    if problems:raise CandidateDiagnostics(problems)
                if self.revision_base is not None:
                    unchanged = check_revision(self.revision_base, outcome.plan, self.pending_findings)
                    if unchanged:
                        unresolved=[f for f in self.pending_findings if f['id'] in unchanged]
                        raise ValueError('复核要求的执行字段尚未修订：'+json.dumps(unresolved,ensure_ascii=False))
                if self.intent_mode:
                    # Compile program-generated local templates before review;
                    # a structural compiler error must not start an LLM loop.
                    from .planner import compile_plan
                    from semora_contracts import QueryPlan
                    from semora_query_core.sources import source_ontology
                    for step in outcome.plan.source_steps:
                        local = QueryPlan.model_validate(step.query.model_dump(mode='json'))
                        local.filters += [QueryFilter(field=p.target_field, operator='in', value=[None]) for b in step.inputs for p in b.pairs]
                        compile_plan(local, source_ontology(self.ontology, step.data_source_id))
                    if not outcome.plan.source_steps:
                        compile_plan(outcome.plan, source_ontology(self.ontology, outcome.plan.data_source_id))
                candidate_id = self.registry.register(value,self.artifact_binding())
                self.protocol_stage = 'delivering'
                current = execution_view(outcome.plan)
                previous = self.previous_validated_plan
                if previous != current:
                    changes = []
                    def diff(before, after, path=''):
                        if before == after:
                            return
                        if isinstance(before, dict) and isinstance(after, dict):
                            for key in sorted(before.keys() | after.keys()):
                                diff(before.get(key), after.get(key), path + '/' + key)
                        elif isinstance(before, list) and isinstance(after, list) and len(before) == len(after):
                            for i, (old, new) in enumerate(zip(before, after)):
                                diff(old, new, path + '/' + str(i))
                        else:
                            changes.append({'path': path, 'before': before, 'after': after})
                    if previous is not None:
                        diff(previous, current)
                    self.event('plan.candidate.validated', {
                        'summary': outcome.requirement.summary,
                        'conclusions': outcome.requirement.analysis_steps,
                        'changes': changes[:20], 'additional_changes': max(0, len(changes) - 20),
                        'revision': previous is not None,
                    })
                    self.previous_validated_plan = deepcopy(current)
                return {'valid': True, 'candidate_id': candidate_id, 'plan_summary': {
                            'metrics': outcome.plan.metrics, 'dimensions': outcome.plan.dimensions,
                            'fields': outcome.plan.fields, 'grain': outcome.plan.grain,
                            'relationship_path': outcome.plan.relationship_path,
                            'source_steps': [step.name for step in outcome.plan.source_steps],
                            'order_by': [item.model_dump(mode='json') for item in outcome.plan.order_by],
                            'limit': outcome.plan.limit},
                        'structural_checks': checks, 'message': '方案校验通过。最终payload只提交candidate_id，程序复用该方案；后续复核和执行门禁仍生效。'}
            except ValueError as exc:
                self.protocol_stage = 'planning'
                if isinstance(exc,CandidateDiagnostics):
                    errors=exc.issues
                elif isinstance(exc, ValidationError):
                    errors = [{'path': '.'.join(map(str, e['loc'])), 'message': e['msg']} for e in exc.errors(include_input=False)][:8]
                else:
                    errors = [{'path': 'analysis', 'message': str(exc)[:1000]}]
                signature = json.dumps({'value': value, 'errors': errors}, sort_keys=True, ensure_ascii=False)
                count = self.registry.record_failure(signature)
                if count >= 3:
                    self.event('agent.validation.stalled', {'summary': '同一无效方案重复提交3次，停止无进展的修订', 'errors': errors})
                    if not self.bounded:raise PlanValidationError('同一无效方案重复提交3次；请依据错误路径修订执行字段') from exc
                    self.protocol_stage='finalizing'
                invalid_id=str(uuid4())
                self.invalid_candidate={'id':invalid_id,'analysis':deepcopy(value),'binding':self.candidate_binding()}
                suggestions=[]
                for error in errors:
                    path=re.sub(r'\([^)]*\)','',error['path'])
                    path=re.sub(r'\[(\d+)\]',r'.\1',path).removeprefix('analysis.')
                    if path.endswith('.include_source_fields'):
                        suggestions.append({'path':path,'value_json':'[]','condition':'仅当无需额外携带来源列、可使用当前源实体维度时采用；不改变选人、筛选、聚合或排序。'})
                    elif error.get('replacement_json') is not None:
                        suggestions.append({'path':path,'value_json':error['replacement_json'],'condition':'落实用户已确认的范围，不改变前置实体选取或权限。'})
                return {'valid': False, 'errors': errors, 'repeat_count': count,
                        'invalid_candidate_id':invalid_id,'suggested_patches':suggestions,
                        'message': '; '.join(e['path'] + ': ' + e['message'] for e in errors),
                        'repair_hint': '优先调用patch_plan对invalid_candidate_id按错误路径局部替换，不重新抄写整份方案。保留其他执行条件；通过后使用candidate_id提交。'}

        async def validate_intent(intent: QueryIntent) -> dict:
            """提交业务查询意图。程序负责关联路径、数据源、关联键、分步查询和预聚合；不执行SQL。"""
            intent = QueryIntent.model_validate(intent)
            from .planning_recovery import failure,preserve_unrelated
            if self.intent_repair_base is not None and not preserve_unrelated(self.intent_repair_base,intent.model_dump(mode='json'),self.intent_repair_paths):
                return failure(self,intent,kind='compiler_validation',path='intent',message='定向补正改动了未授权的其他业务条件；已停止本次补正，原需求和方案均保留。')
            self.run.agent_memory['last_intent_candidate']=[intent.model_dump(mode='json')]
            self.event('intent.submitted', {'summary':intent.summary, 'subject':intent.subject, 'fields':intent.fields, 'group_by':intent.group_by, 'metrics':intent.metrics, 'measure_names':[m.name for m in intent.measures], 'selection_object':intent.selection.object if intent.selection else None, 'selection_limit':intent.selection.limit if intent.selection else None})
            from .intent_planner import compile_intent, IntentPlanningError
            from .prepared_context import authorized_objects
            available = authorized_objects(self.run, self.ontology)
            ontology = {**self.ontology, 'objects': list(available.values()),
                        'relations': [r for r in self.ontology.get('relations', []) if {r['from'], r['to']} <= available.keys()]}
            try:
                value = compile_intent(intent, ontology, self.scope)
                self.run.agent_memory.pop('planning_normalizations',None)
                if (intent.measures or intent.metrics) and not intent.group_by and value.get('dimensions') and not value.get('source_steps'):
                    normalization={'kind':'entity_grain','subject':intent.subject,'dimensions':value['dimensions'],
                        'basis':'输出包含主体唯一标识，其他属性沿已发布对一关系取得，按主体逐条统计。'}
                    self.run.agent_memory['planning_normalizations']=[normalization]
                    self.event('planning.normalized',{'summary':'已依据唯一标识补齐逐主体统计粒度，保留明细与聚合指标',**normalization})
                # Reuse the established structural/permission validation, but the
                # model never receives or edits the executable contract.
                analysis = AnalysisPayload.model_validate({k:v for k,v in value.items() if k in AnalysisPayload.model_fields})
                self._intent_metadata = {k:value[k] for k in ('query_intent','execution_mode','data_source_id')}
                try:
                    result = await validate_plan(analysis)
                finally:
                    self._intent_metadata = {}
                if result.get('valid'):
                    self.registry.replace_validated(result['candidate_id'],value,self.artifact_binding())
                    return {'valid':True, 'candidate_id':result['candidate_id'], 'summary':intent.summary,
                            'message':'业务意图已由程序编译并通过结构校验，接下来由独立Reviewer复核。'}
                # A structural compiler limitation is not a business question or
                # an instruction for the model to manufacture a physical plan.
                self.event('intent.compilation.failed', {'kind':'compiler_validation','errors':result.get('errors',[])})
                errors=result.get('errors') or []
                detail='；'.join(e.get('message','') for e in errors[:3]) or '未形成可验证的执行方案'
                return failure(self,intent,kind='compiler_validation',path='intent',message='程序规划校验未通过：'+detail,errors=errors)
            except IntentPlanningError as exc:
                return failure(self,intent,kind=exc.kind,path=exc.path,message=str(exc),options=exc.options,reference=exc.reference)
            except ValidationError as exc:
                errors=[{'path':'.'.join(map(str,e['loc'])),'message':e['msg']} for e in exc.errors(include_input=False)][:8]
                return failure(self,intent,kind='compiler_validation',path='intent',message='程序生成的执行契约未通过校验：'+'；'.join(e['path']+': '+e['message'] for e in errors),errors=errors)

        async def patch_plan(invalid_candidate_id:str, patches:Annotated[list[PlanPatch],Field(min_length=1,max_length=8)]) -> dict:
            """只修订上一份无效方案的出错路径，保留其余条件，并重新执行完整校验；不执行SQL。"""
            candidate=self.invalid_candidate
            if not candidate or candidate['id']!=invalid_candidate_id or candidate['binding']!=self.candidate_binding():
                return {'valid':False,'message':'无效候选引用已失效；只能修改当前需求、权限和本体下最新一份无效方案。'}
            updated=deepcopy(candidate['analysis'])
            try:
                for patch in patches:
                    if self.allowed_patch_paths is not None and patch.path not in self.allowed_patch_paths:
                        return {'valid':False,'message':'本次仅修订已确定的范围缺口：'+', '.join(sorted(self.allowed_patch_paths))}
                    parts=patch.path.split('.')
                    if parts[0] not in AnalysisPayload.model_fields or any(p.startswith('__') for p in parts):raise ValueError('补丁路径不属于查询方案')
                    parent=updated
                    for part in parts[:-1]:parent=parent[int(part)] if isinstance(parent,list) else parent[part]
                    value=json.loads(patch.value_json)
                    if isinstance(parent,list):parent[int(parts[-1])]=value
                    elif isinstance(parent,dict):parent[parts[-1]]=value
                    else:raise ValueError('补丁路径不可替换')
                analysis=AnalysisPayload.model_validate(updated)
            except (ValueError,KeyError,IndexError,TypeError):
                return {'valid':False,'message':'补丁路径或替换值无效；候选未修改，请使用准确路径和JSON值。'}
            return await validate_plan(analysis)

        async def enter_planning() -> dict:
            """上下文足以形成方案时进入规划，获取完整候选校验契约。"""
            self.protocol_stage = 'planning'
            return {'stage': 'planning', 'message': '现在调用validate_plan；缺少证据时用resume_exploration返回检索。'}

        async def resume_exploration(reason: str) -> dict:
            """候选缺少证据或需进一步修改时返回探索；保留已有事实。"""
            self.protocol_stage = 'exploring'
            return {'stage': 'exploring', 'message': '已返回探索；已有事实与需求保持完整。', 'reason': reason[:300]}

        async def read_task_artifact(artifact_id: str) -> dict:
            """按任务记忆引用读取完整历史产物；不会授予执行或审批权限。"""
            result = read_artifact(self.run, self.ontology, self.scope, artifact_id)
            if self.intent_mode:
                from .intent_planner import semantic_context
                result = semantic_context(result)
                from .model_context import project_knowledge
                result = project_knowledge(result,self.run)
            return result

        async def read_evidence() -> dict:
            """读取当前任务实际检索到的知识引用，不编造任何新来源。"""
            from .model_context import project_knowledge
            return project_knowledge({'evidence': [e.model_dump(mode='json') for e in self.run.evidence]},self.run)

        async def inspect_artifacts() -> dict:
            """只读检查当前已确认需求、查询方案、SQL门禁和结果统计；不执行查询。"""
            return {name: getattr(self.run, name).model_dump(mode='json') if getattr(self.run, name) else None for name in ('requirement', 'plan', 'sql', 'gate')}

        async def request_context(objects: Annotated[list[str],Field(min_length=1,max_length=4)], purpose: Literal['output','filter','time','join','aggregation','sort','sql_error'], reason: str,
                                  field_refs: Annotated[list[str] | None,Field(max_length=16,description='复核缺失的具体对象.字段引用')] = None,
                                  acceptance: Annotated[str,Field(max_length=500,description='补齐后要核验的具体条件')] = '') -> dict:
            """仅补齐完成当前需求缺少的核心对象，最多一次；不得扩展可选分析目标。业务歧义直接澄清。"""
            from .prepared_context import supplement_context
            if not self.context_state:return {'available':False,'message':'当前准备资料缺失，停止扩展'}
            if self.role=='reviewer':
                from .review_context import supplement_review
                result=supplement_review(self,objects,purpose,reason,field_refs or [],acceptance)
                if result.get('finalize'):self.protocol_stage='finalizing'
                return result
            result=supplement_context(self.workflow,self.run,self.ontology,self.context_state,objects,purpose,reason,self.scope)
            if result.get('prepared_context'):
                from .task_handoff import project_context
                result={**result,'prepared_context':project_context({'prepared_context':result['prepared_context']},self.role)['prepared_context']}
            if self.intent_mode:
                from .intent_planner import semantic_context
                result = semantic_context(result)
            if self.intent_mode or self.role=='reviewer':
                from .model_context import project_knowledge
                result=project_knowledge(result,self.run)
            return result

        functions = [read_object, find_relation_path, read_schema, read_evidence]
        if self.bounded:functions.append(request_context)
        if self.role == 'analyst' and os.getenv('AGENT_TASK_MEMORY', 'false').lower() == 'true':
            functions.append(read_task_artifact)
        if self.role == 'reviewer':
            functions.append(inspect_artifacts)
        else:
            functions.extend([validate_plan, *([patch_plan] if self.bounded else []), *([enter_planning, resume_exploration] if self.staged else []), *self.knowledge_tools()])
        if self.intent_mode:
            functions = [f for f in functions if f.__name__ in {'request_context','read_evidence','read_task_artifact'}] + [validate_intent]
        return [self._tool(f) for f in functions]

    def knowledge_tools(self):
        kb_ids = self.ontology.get('knowledgeBaseIds') or []
        discoveries = self.run.agent_memory.setdefault('knowledge_discovery', [])

        def resolve_kb(kb_id):
            # With exactly one binding, routing is a deterministic fact, not
            # something the model needs to reconstruct from a UUID.
            if not kb_id and len(kb_ids) == 1:
                return kb_ids[0]
            return kb_id if kb_id in kb_ids else None

        def scope_error():
            return {'available': False, 'error': 'invalid_knowledge_base', 'allowed_kb_ids': kb_ids,
                    'message': '知识库ID缺失或不属于当前绑定范围。请使用 allowed_kb_ids 中的ID；未访问其他知识库。'}

        def allowed(kb_id, kind, ref):
            return kb_id in kb_ids and any(r == {'kb_id': kb_id, 'kind': kind, 'ref': ref} for r in discoveries)

        def remember(kb_id, kind, ref):
            value = {'kb_id': kb_id, 'kind': kind, 'ref': ref}
            if ref and value not in discoveries:
                discoveries.append(value)

        def research_step(tool, summary):
            if self.run.knowledge_research is None:
                self.run.knowledge_research = KnowledgeResearchResult(provider=self.knowledge_provider.provider, agent_id='semora-analyst-react', session_id=self.run.id)
            self.run.knowledge_research.steps.append(KnowledgeResearchStep(kind='tool_result', tool=tool, summary=summary))
            self.run.knowledge_research.evidence = self.run.evidence

        async def search_wiki(query: Annotated[str, Field(min_length=1, max_length=64, description='每次只填一个短关键词、别名或表名。禁止用空格/逗号/竖线拼多个关键词。看完本次结果后再调用下一次。')], kb_id: str = '') -> dict:
            """用一个关键词搜索绑定知识库的Wiki入口。零命中后换短词或别名再次调用。"""
            kb_id = resolve_kb(kb_id)
            if kb_id is None:
                return scope_error()
            keyword = query.strip()
            terms = [term for term in re.split(r'[\s,，、;；|]+', keyword) if term]
            if not keyword or len(terms) != 1 or re.search(r'[\s,，、;；|]', keyword):
                return {'available': False, 'error': 'single_keyword_required', 'suggested_keywords': terms[:6], 'message': '一次只允许检索一个关键词。请从 suggested_keywords 中选一个单独调用，观察返回后再决定下一个词；本次没有发出检索请求。'}
            # Escape regex syntax: this Agent tool exposes literal keyword
            # search, not the upstream's raw regular-expression API.
            rows = await self.knowledge_provider._wiki_search(kb_id, re.escape(keyword), limit=8)
            for row in rows:
                remember(kb_id, 'page', row.get('slug'))
            message = f'检索“{keyword}”：返回 {len(rows)} 个 Wiki 页面。'
            if not rows:
                message += '仅此关键词零命中，不能判定整个知识库没有内容。请换更短词、别名或中英文表名，单独再次调用 search_wiki。'
            else:
                message += '请先阅读相关页面，再按证据缺口决定是否搜索下一个关键词。'
            if len(rows) == 8:
                message += '已达到本次返回上限，8 不是全库命中总数；需要时换更具体的关键词。'
            research_step('search_wiki', message)
            return {'keyword': keyword, 'count': len(rows), 'limit': 8, 'possibly_truncated': len(rows) == 8, 'message': message,
                    'pages': [{'slug': r.get('slug'), 'title': r.get('title'), 'summary': str(r.get('summary') or r.get('content') or '')[:600]} for r in rows]}

        async def read_wiki(slug: str, kb_id: str = '') -> dict:
            """阅读已经搜索发现或被已读页面关联引用的Wiki全文，发现新链接和原文来源。"""
            kb_id = resolve_kb(kb_id)
            if kb_id is None:
                return scope_error()
            if not allowed(kb_id, 'page', slug):
                return {'available': False, 'message': '该页面尚未被检索发现，请先搜索入口'}
            page = await self.knowledge_provider._wiki_page(kb_id, slug)
            links = (page.get('out_links') or []) + (page.get('in_links') or [])
            for link in links:
                remember(kb_id, 'page', link.get('slug') if isinstance(link, dict) else link)
            for source in page.get('source_refs') or []:
                if isinstance(source, str):
                    remember(kb_id, 'source', source)
            page = {**page, 'slug': page.get('slug') or slug}
            evidence = self.knowledge_provider._page_evidence(kb_id, page)
            if not any(e.knowledge_base_id == kb_id and e.locator == evidence.locator for e in self.run.evidence):
                self.run.evidence.append(evidence)
            research_step('read_wiki', f'阅读 {evidence.title}，取得来源和关联页面')
            return {'page': {'slug': slug, 'title': page.get('title'), 'content': str(page.get('content') or '')[:8000], 'links': links[:30], 'source_refs': page.get('source_refs') or []}}

        async def read_source(document_id: str, kb_id: str = '', page: int = 1) -> dict:
            """回溯已读Wiki引用的原始文档分块，核实业务定义。"""
            kb_id = resolve_kb(kb_id)
            if kb_id is None:
                return scope_error()
            if not allowed(kb_id, 'source', document_id):
                return {'available': False, 'message': '原文没有出现在已读取的证据引用中，不能访问'}
            chunks, _ = await self.knowledge_provider._request('GET', f'/chunks/{quote(document_id, safe="")}', params={'page': max(1, min(page, 20)), 'page_size': 10})
            research_step('read_source', f'回溯文档 {document_id} 第 {page} 页分块')
            return {'chunks': [{'id': r.get('id'), 'content': str(r.get('content') or '')[:3000]} for r in chunks if isinstance(r, dict)] if isinstance(chunks, list) else []}

        return [search_wiki, read_wiki, read_source]
