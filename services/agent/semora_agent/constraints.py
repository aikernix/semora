"""Checks for explicit business predicates, shared by analysis and repair."""
from __future__ import annotations

import json
import re

from semora_contracts import QueryFilter


def filter_key(item):
    return json.dumps(item.model_dump(mode='json'), sort_keys=True, ensure_ascii=False)


def explicit_filters(texts, ontology):
    """Recognize only unambiguous, scalar comparisons against published fields.

    This is an additional consistency check, not an NL-to-SQL parser. Ambiguous
    field names and unsupported syntax must be handled by the semantic Reviewer.
    """
    aliases = {}
    for obj in ontology.get('objects', []):
        for prop in obj.get('properties', []):
            field = f"{obj['name']}.{prop['name']}"
            names = {field, str(prop['name'])}
            if prop.get('column'):
                names.update({str(prop['column']), f"{obj['name']}.{prop['column']}"})
            for alias in names:
                aliases.setdefault(alias.lower(), set()).add(field)
    result = {}
    pattern = r'(?<![A-Za-z0-9_.])([A-Za-z_][\w]*(?:\.[A-Za-z_][\w]*)?)\s*(>=|<=|!=|<>|=|>|<)\s*(\x27[^\x27]*\x27|"[^"]*"|-?\d+(?:\.\d+)?(?![A-Za-z0-9_.]))'
    null_pattern = r'(?<![A-Za-z0-9_.])([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)?)\s*(IS\s+NOT\s+NULL\b|IS\s+NULL\b|不为空|为空)'
    operators = {'>': 'gt', '>=': 'gte', '<': 'lt', '<=': 'lte', '=': 'eq', '!=': 'ne', '<>': 'ne'}
    for text in texts:
        # Disjunction/negation is not safely representable by the plan's AND list.
        text = str(text).replace('`', '')
        guard_text = re.sub(r'\bIS\s+NOT\s+NULL\b', 'IS_PRESENT', text, flags=re.I)
        if re.search(r'\b(?:OR|NOT)\b|或者|或是|不要|不使用|不采用|并非', guard_text, re.I):
            continue
        for match in re.finditer(null_pattern, text, re.I):
            name, operator = match.groups()
            fields = aliases.get(name.lower(), set())
            if len(fields) == 1:
                item = QueryFilter(field=next(iter(fields)), operator='is_not_null' if re.search(r'NOT|不为', operator, re.I) else 'is_null')
                result[filter_key(item)] = item
        for match in re.finditer(pattern, text):
            name, operator, literal = match.groups()
            fields = aliases.get(name.lower(), set())
            if len(fields) != 1:
                continue
            value = literal[1:-1] if literal[0] in "\x27\"" else float(literal) if '.' in literal else int(literal)
            item = QueryFilter(field=next(iter(fields)), operator=operators[operator], value=value)
            result[filter_key(item)] = item
    return list(result.values())


def missing_explained_filters(plan, assumptions, ontology):
    def applied(query):
        actual = {filter_key(item) for item in query.filters}
        actual.update(filter_key(f) for c in query.calculations for f in c.filters)
        actual.update(metric_predicates(query, ontology))
        for stage in query.stages:
            actual.update(filter_key(item) for item in stage.filters)
            actual.update(filter_key(item) for measure in stage.measures for item in measure.filters)
        return actual
    # A predicate inside a published metric is already applied at metric scope;
    # moving it into WHERE could change the denominator or other measures.
    # Stage-local predicates are already compiled at their declared scope.
    # Reviewer checks whether that scope matches the user's business meaning;
    # do not move them into WHERE and change LEFT JOIN preservation.
    steps=getattr(plan,'source_steps',[])
    # With source steps only those queries execute; root fields are metadata.
    # Coverage here does not authorize moving conditions between scopes.
    actual=set().union(*(applied(step.query) for step in steps)) if steps else applied(plan)
    return [item for item in explicit_filters(assumptions, ontology) if filter_key(item) not in actual]


def validate_explicit_filters(plan, assumptions, ontology):
    missing=missing_explained_filters(plan,assumptions,ontology)
    if missing:
        raise ValueError('业务条件只写入说明，未落实到 filters：' + ', '.join(filter_key(item) for item in missing))


def validate_intent_filter_coverage(plan):
    """Hard-check declared conditions, without treating narrative as authority."""
    intent=plan.query_intent
    if intent is None:return
    steps=plan.source_steps
    selection=next((s for s in steps if s.name=='select_members'),None)
    def keys(query):
        values=[*query.filters,*[f for s in query.stages for f in s.filters],
                *[f for s in query.stages for m in s.measures for f in m.filters],
                *[f for c in query.calculations for f in c.filters]]
        return {filter_key(f) for f in values}
    downstream=set().union(*(keys(s.query) for s in steps if s is not selection)) if steps else keys(plan)
    expected={filter_key(f) for f in intent.filters}
    if expected-downstream:
        raise ValueError('程序规划遗漏已声明的查询筛选条件')
    final=steps[-1].query if steps else plan
    if final.having!=intent.having:
        raise ValueError('程序规划改变了已声明的分组后筛选')
    optional={r for s in steps for r in s.query.optional_relationships} if steps else set(plan.optional_relationships)
    if optional!=set(intent.optional_relationships):
        raise ValueError('程序规划遗漏了保留未匹配主体的关系语义')
    actual={c.name:c for c in final.calculations}
    for measure in intent.measures:
        calculation=actual.get(measure.name)
        if calculation is None or calculation.filters!=measure.filters or calculation.null_value!=measure.null_value:
            raise ValueError('程序规划改变了指标条件或空值填充')
    if intent.existence:
        final=steps[-1] if steps else None
        rule=intent.existence
        if (final is None or final.membership is None or not final.inputs
            or final.inputs[0].relation!=rule.relation or final.membership.present!=rule.present
            or {filter_key(f) for f in rule.filters}-keys(final.query)):
            raise ValueError('程序规划遗漏已声明的关联存在性条件')
    if intent.selection:
        if selection is None or {filter_key(f) for f in intent.selection.filters}-keys(selection.query):
            raise ValueError('程序规划遗漏已声明的选取条件')
        selected={filter_key(f) for f in intent.selection.filters}
        if intent.record_scope=='selection_window' and selected-downstream:
            raise ValueError('程序规划遗漏已声明的后续范围条件')
        if intent.record_scope=='all_related_records' and (selected-expected)&downstream:
            raise ValueError('程序规划将仅用于选取的条件错误施加到后续查询')


def metric_predicates(plan, ontology):
    expressions = [str(metric.get('expression') or '') for metric in ontology.get('metrics', []) if metric.get('name') in plan.metrics]
    return {filter_key(item) for item in explicit_filters(expressions, ontology)}


def compatible_repair(old_plan, new_plan, requirement, ontology):
    """Only preserve confirmed semantics; otherwise require another confirmation."""
    fields = {'metrics', 'calculations', 'stages', 'source_steps', 'dimensions', 'fields', 'time_range', 'grain', 'relationship_path', 'data_scope', 'order_by', 'limit','having','optional_relationships'}
    if old_plan.model_dump(include=fields) != new_plan.model_dump(include=fields):
        return False
    old_filters = {filter_key(item) for item in old_plan.filters}
    new_filters = {filter_key(item) for item in new_plan.filters}
    confirmed = {filter_key(item) for item in requirement.filters}
    texts = requirement.assumptions + [fact.value for fact in requirement.confirmed_facts]
    confirmed.update({filter_key(item) for item in explicit_filters(texts, ontology)} - metric_predicates(old_plan, ontology))
    # New prose is not user authorization, even if execution fields stay unchanged.
    return (old_filters <= new_filters and new_filters - old_filters <= confirmed
            and new_plan.assumptions == old_plan.assumptions)
