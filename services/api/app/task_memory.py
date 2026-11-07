"""Deterministic task state. User facts are never inferred from model prose."""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from .evidence_policy import EVIDENCE_REUSE_TTL_SECONDS


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def binding(run, ontology, scope):
    return digest({'domain': run.domain, 'ontology': ontology,
                   'sources': sorted({o.get('dataSourceId') or ontology.get('dataSourceId', '') for o in ontology.get('objects', [])}),
                   'scope': scope, 'authorization': run.authorization.model_dump(mode='json') if run.authorization else None})


def build_memory(run, ontology, scope, feedback=None, now=None, prior_requirement=None):
    now = now or datetime.now(timezone.utc)
    previous = (run.agent_memory.get('task_memory') or [{}])[-1]
    c = run.continuation or {}
    inherited = c.get('task_memory') or {}
    history = deepcopy(previous.get('fact_history') or inherited.get('fact_history') or [])
    entries = [(f, 'continuation', c.get('source_run_id')) for f in c.get('confirmed_facts', [])]
    if run.requirement:
        entries += [(f.model_dump(mode='json'), 'confirmed_requirement', run.requirement.version) for f in run.requirement.confirmed_facts]
    for index, row in enumerate(run.alignment_rounds):
        entries += [(f, 'alignment_answer', str(index)) for f in row.get('answers', [])]
    entries += [(f.model_dump(mode='json'), 'user_answer', run.id) for f in run.user_answers]
    for fact, source, source_id in entries:
        if not fact.get('question_id'):
            continue
        item = {'fact': fact, 'source': source, 'source_id': source_id}
        if not any(x['fact'] == fact for x in history):
            history.append(item)
    active = {}
    for entry in history:
        active[entry['fact']['question_id']] = entry
    # Explicit current typed answers win even if their value appeared earlier.
    for fact, source, source_id in entries:
        if not fact.get('question_id'):
            continue
        active[fact['question_id']] = {'fact': fact, 'source': source, 'source_id': source_id}
    old_active = {x['fact']['question_id']: x for x in previous.get('facts', inherited.get('facts', []))}
    for key, item in active.items():
        if key in old_active and old_active[key]['fact'] != item['fact']:
            history.append(deepcopy(item))
    by_field = {}
    for entry in active.values():
        fact = entry['fact']
        by_field.setdefault(fact['field'], []).append(fact)
    conflicts = [{'field': field, 'facts': rows} for field, rows in by_field.items() if len({r['value'] for r in rows}) > 1]
    origin = c.get('evidence_origin_at', run.created_at.isoformat())
    age = max(0, (now - datetime.fromisoformat(origin.replace('Z', '+00:00'))).total_seconds())
    current_binding = binding(run, ontology, scope)
    snapshot = {'version': 1, 'binding': current_binding, 'created_at': now.isoformat(),
                'domain': run.domain, 'current_request': run.question,
                'request_history': deepcopy(c.get('question_history', [])),
                'requirement_form': deepcopy(c.get('requirement_form', {})),
                'facts': list(active.values()), 'fact_history': history, 'conflicts': conflicts,
                'unresolved': deepcopy((run.requirement_draft.model_dump(mode='json') if run.requirement_draft else c.get('draft') or {}).get('clarifications', [])),
                'evidence': [{'reference': e.model_dump(mode='json'), 'origin_at': origin, 'age_seconds': age,
                              'requires_refresh': age > EVIDENCE_REUSE_TTL_SECONDS or bool(c.get('bindings_changed'))} for e in run.evidence],
                'binding_changed': bool((previous or inherited).get('binding') and (previous or inherited)['binding'] != current_binding),
                'feedback': deepcopy(feedback), 'artifacts': {}}
    for name, value in {'candidate': (run.agent_memory.get('last_candidate') or [None])[-1],
                        'confirmed_requirement': run.requirement.model_dump(mode='json') if run.requirement else c.get('confirmed_requirement') or prior_requirement,
                        'legacy_confirmations': c.get('legacy_confirmations')}.items():
        if value:
            key = digest([name, value])
            snapshot['artifacts'][key] = {'kind': name, 'value': deepcopy(value), 'requires_revalidation': True,
                                          'origin_binding': ((inherited.get('binding') or 'unbound_historical') if name == 'confirmed_requirement' and not run.requirement else current_binding)}
            if name == 'confirmed_requirement':
                # Follow-up edits need the previous execution contract in view;
                # hiding sorting/limits behind an optional artifact read loses them.
                keys=('metrics','dimensions','fields','output_fields','filters','calculations','stages','source_steps','order_by','limit','grain','relationship_path')
                snapshot['previous_confirmed_execution'] = {
                    'values': {k:deepcopy(value[k]) for k in keys if k in value},
                    'source_run_id':c.get('source_run_id'),
                    'requires_revalidation':True,
                    'authority':'此前确认方案；本轮用户修改优先。保留未修改条件，但必须依据当前本体和权限重验。'}
    run.agent_memory['task_memory'] = [snapshot]
    return snapshot


def memory_context(snapshot, request=None):
    result = {k: deepcopy(v) for k, v in snapshot.items() if k not in {'fact_history', 'artifacts'}}
    if request == result.get('current_request'):
        result.pop('current_request', None)
    result['artifact_refs'] = [{'id': key, 'kind': v['kind']} for key, v in snapshot['artifacts'].items()]
    result['authority'] = '用户事实与原请求是依据；历史候选不是确认事实，所有执行产物必须重新校验。'
    return result


def read_artifact(run, ontology, scope, artifact_id):
    saved = (run.agent_memory.get('task_memory') or [{}])[-1]
    if saved.get('binding') != binding(run, ontology, scope):
        return {'available': False, 'message': '任务记忆版本或权限已变化，禁止读取旧产物'}
    artifact = saved.get('artifacts', {}).get(artifact_id)
    if artifact and artifact.get('origin_binding') not in {None, binding(run, ontology, scope)}:
        return {'available': False, 'message': '历史产物绑定已经变化，必须重新核验，不能视为当前确认方案'}
    return {'available': bool(artifact), 'artifact': deepcopy(artifact)}


def repair_context(error, candidate=None, findings=None):
    message = str(error or '')
    if any(term in message.lower() for term in ('timeout', '超时', '总时限', 'readtimeout')):
        kind = 'model_timeout'
    elif findings and any(f.get('kind') == 'clarification' for f in findings):
        kind = 'semantic_ambiguity'
    elif any(term in message for term in ('编译', '规划服务', 'compile')):
        kind = 'compilation'
    elif any(term in message for term in ('执行', '数据库', '截断')):
        kind = 'execution'
    else:
        kind = 'plan_structure'
    return {'kind': kind, 'error': message, 'candidate': deepcopy(candidate), 'findings': deepcopy(findings or []),
            'instruction': '超时仅诊断请求，不凭超时修改SQL。结构或技术修复必须保留确认口径；语义冲突交由用户澄清。'}
