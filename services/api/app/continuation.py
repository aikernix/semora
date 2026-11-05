"""Explicit business handoff between attempts; never transfer execution authority."""
from __future__ import annotations

import json


def legacy_candidate(run):
    allowed = {'summary', 'intent', 'subject', 'metrics', 'dimensions', 'fields', 'filters', 'time_range',
               'grain', 'relationship_path', 'calculations', 'stages', 'assumptions', 'aggregation_rules',
               'output_fields', 'clarifications', 'analysis_steps'}
    for message in reversed(run.agent_memory.get('analyst', [])):
        if message.get('type') != 'ai':
            continue
        for call in message.get('data', {}).get('tool_calls', []):
            arguments = call.get('args')
            if not isinstance(arguments, dict):
                continue
            payload = arguments.get('payload')
            if call.get('name') == 'AgentSubmission' and isinstance(payload, dict):
                return {key: value for key, value in payload.items() if key in allowed}
    return None


def legacy_confirmations(run):
    # Older runs did not persist answers separately. Only inspect framework-built
    # Human messages; never promote model messages or tool output to user facts.
    for item in reversed(run.agent_memory.get('analyst', [])):
        if item.get('type') != 'human':
            continue
        try:
            request = json.loads(item.get('data', {}).get('content', '')).get('request', '')
        except (ValueError, TypeError, AttributeError):
            continue
        if isinstance(request, str) and '。用户补充确认：' in request:
            return request.rsplit('。用户补充确认：', 1)[1]
    return ''


def handoff(source, session):
    previous = source.continuation or {}
    candidate = (source.agent_memory.get('last_candidate') or [{}])[-1]
    facts = {f['question_id']: f for f in previous.get('confirmed_facts', [])}
    if source.requirement:
        facts.update({f.question_id: f.model_dump(mode='json') for f in source.requirement.confirmed_facts})
    for round in source.alignment_rounds:
        for fact in round.get('answers', []):
            if fact.get('question_id'):
                facts[fact['question_id']] = fact
    facts.update({f.question_id: f.model_dump(mode='json') for f in source.user_answers})
    reviews = [e for e in source.events if e.event in {'plan.preflight.completed', 'review.completed', 'review.revision.required'}]
    counts = dict(previous.get('role_rounds', {}))
    for event in source.events:
        if event.event == 'agent.model.started':
            role = event.data.get('agent', 'analyst')
            counts[role] = counts.get(role, 0) + 1
    draft = candidate.get('requirement') or (source.requirement_draft.model_dump(mode='json') if source.requirement_draft else previous.get('draft'))
    plan = candidate.get('plan') or source.plan or source.proposed_plan or previous.get('candidate_plan')
    if hasattr(plan, 'model_dump'):
        plan = plan.model_dump(mode='json')
    history = previous.get('question_history', []) + [source.question]
    return {
        'source_run_id': source.id, 'root_run_id': previous.get('root_run_id', source.id),
        'question_history': [history[0], *history[-11:]] if len(history) > 12 else history,
        'requirement_form': previous.get('requirement_form', {} if source.message_mode == 'new' else session.requirement_form),
        'confirmed_facts': list(facts.values()),
        'legacy_confirmations': legacy_confirmations(source) or previous.get('legacy_confirmations', ''),
        'draft': draft, 'candidate_plan': plan,
        'legacy_candidate': legacy_candidate(source) if not candidate else None,
        'confirmed_requirement': source.requirement.model_dump(mode='json') if source.requirement else previous.get('confirmed_requirement'),
        'review_feedback': reviews[-1].data if reviews else previous.get('review_feedback'),
        'previous_error': source.error,
        'failed_stage': next((e.status.value for e in reversed(source.events) if e.status.value != 'failed'), 'received'),
        'model_round_offset': sum(counts.values()), 'role_rounds': counts,
        'ontology': source.ontology.model_dump(mode='json') if source.ontology else None,
        'evidence_origin_at': previous.get('evidence_origin_at', source.created_at.isoformat()),
    }
