"""Compile business intent using published metadata only. No model or database access."""
from __future__ import annotations

from copy import deepcopy
from semora_query_core.intent import IntentPlanningError, IntentCompiler, compile_intent


def enabled():
    import os
    return os.getenv('AGENT_QUERY_INTENT', 'true').lower() == 'true'


def semantic_context(value):
    """Lossless business projection; executable artifacts remain with the Owner."""
    if isinstance(value, list):
        return [semantic_context(v) for v in value]
    if not isinstance(value, dict):
        # Owner's analysis request can contain serialized prior artifacts.
        if isinstance(value, str) and value.lstrip().startswith(('{', '[')):
            import json
            try:
                return semantic_context(json.loads(value))
            except (ValueError, TypeError):
                pass
        return value
    if value.get('query_intent'):
        return {'query_intent': deepcopy(value['query_intent']), **{
            k: semantic_context(v) for k, v in value.items()
            if k in {'confirmed_facts', 'summary', 'confirmed_at', 'assumptions', 'evidence'}}}
    excluded = {'source_steps', 'stages', 'source_layout', 'transfer_templates', 'selection_plan_shape',
                'schemas', 'stage_join_contracts', 'stage_group_keys', 'dataSourceId', 'data_source_id', 'datasources', 'table',
                'column', 'primaryKey', 'condition', 'physical_sql', 'semantic_sql', 'parameters',
                'rejected_sql', 'artifact_refs', 'rejected_plan', 'candidate_plan'}
    projected = {k: semantic_context(v) for k, v in value.items() if k not in excluded}
    # Older confirmed sessions predate QueryIntent. Preserve their full business
    # operations (filters, selections, aggregates), without execution wiring.
    if value.get('source_steps'):
        projected['previous_business_operations'] = [semantic_context({
            k:v for k,v in step.get('query', {}).items()
            if k in {'intent','grain','fields','dimensions','filters','metrics','calculations','order_by','limit','time_range','assumptions','stages'}})
            for step in value['source_steps']]
    if value.get('stages'):
        projected['aggregate_definitions'] = [{'object':s.get('source'), 'measures':semantic_context(s.get('measures',[])),
                                               'filters':semantic_context(s.get('filters',[]))} for s in value['stages']]
    return projected






def source_output_contract(plan):
    """Declared columns after Executor enrichment, not just SQL projection."""
    from semora_query_core.sources import output_alias
    steps=[]
    for step in plan.source_steps:
        sql_fields=list(dict.fromkeys(step.query.fields+step.query.dimensions+step.query.metrics))
        enrichment=[{'field':field,'column':binding.source_step+'__'+output_alias(field),
                     'from_step':binding.source_step, 'key_pairs':[p.model_dump(mode='json') for p in binding.pairs],
                     'unique_key_check':'executor_runtime_required'}
                    for binding in step.inputs for field in binding.include_source_fields]
        final_columns=[output_alias(f) for f in sql_fields]+[f['column'] for f in enrichment]
        if step.regroup:
            final_columns=[output_alias(f) for f in step.regroup.group_by]+[m.name for m in step.regroup.measures]
        if step.membership:
            final_columns=[output_alias(f) for f in step.membership.fields]
        steps.append({'step':step.name,'sql_columns':[output_alias(f) for f in sql_fields],
                      'executor_enrichment':enrichment,
                      'regroup':step.regroup.model_dump(mode='json') if step.regroup else None,
                      'membership':step.membership.model_dump(mode='json') if step.membership else None,
                      'final_columns':final_columns})
    return {'steps':steps, 'rule':'Executor在目标SQL执行后，按完整键核验来源唯一性，再追加executor_enrichment列；若有regroup，在完整局部结果上按声明业务维度合并可加指标，最后排序和限量。最终业务维度可不包含传递键，不得要求追加关联键改变业务粒度。失败时停止，不交付部分结果。'}


def intent_repair_route(decision):
    """Compatibility only. New callers use the independent review policy."""
    from semora_agent.review_policy import intent_repair_route as route
    return route(decision)
