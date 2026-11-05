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
    pattern = r'(?<![\w.])([A-Za-z_][\w]*(?:\.[A-Za-z_][\w]*)?)\s*(>=|<=|!=|<>|=|>|<)\s*(\x27[^\x27]*\x27|"[^"]*"|-?\d+(?:\.\d+)?)(?![\w.])'
    null_pattern = r'(?<![\w.])([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)?)\s*(IS\s+NOT\s+NULL\b|IS\s+NULL\b|不为空|为空)'
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


def validate_explicit_filters(plan, assumptions, ontology):
    actual = {filter_key(item) for item in plan.filters}
    # A predicate inside a published metric is already applied at metric scope;
    # moving it into WHERE could change the denominator or other measures.
    actual.update(metric_predicates(plan, ontology))
    # Stage-local predicates are already compiled at their declared scope.
    # Reviewer checks whether that scope matches the user's business meaning;
    # do not move them into WHERE and change LEFT JOIN preservation.
    for stage in plan.stages:
        actual.update(filter_key(item) for item in stage.filters)
        actual.update(filter_key(item) for measure in stage.measures for item in measure.filters)
    missing = [item for item in explicit_filters(assumptions, ontology) if filter_key(item) not in actual]
    if missing:
        raise ValueError('业务条件只写入说明，未落实到 filters：' + ', '.join(filter_key(item) for item in missing))


def metric_predicates(plan, ontology):
    expressions = [str(metric.get('expression') or '') for metric in ontology.get('metrics', []) if metric.get('name') in plan.metrics]
    return {filter_key(item) for item in explicit_filters(expressions, ontology)}


def compatible_repair(old_plan, new_plan, requirement, ontology):
    """Only preserve confirmed semantics; otherwise require another confirmation."""
    fields = {'metrics', 'calculations', 'stages', 'dimensions', 'fields', 'time_range', 'grain', 'relationship_path', 'data_scope'}
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
