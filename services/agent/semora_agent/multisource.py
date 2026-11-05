"""Deterministic source boundaries and cross-source key contracts, no DB access."""
from __future__ import annotations

import copy
import re

import sqlglot
from sqlglot import exp


def object_source(obj, ontology):
    return str(obj.get('dataSourceId') or ontology.get('dataSourceId') or '')


def source_ontology(ontology, source_id):
    result = copy.deepcopy(ontology)
    result['objects'] = [obj for obj in result.get('objects', []) if object_source(obj, ontology) == source_id]
    if not result['objects']:
        raise ValueError('数据源未绑定当前本体的任何对象')
    names = {o['name'] for o in result['objects']}
    result['relations'] = [r for r in result.get('relations', []) if r['from'] in names and r['to'] in names]
    result['metrics'] = [m for m in result.get('metrics', []) if m['grain'] in names and
                         {o for o, _ in re.findall(r'\b(\w+)\.(\w+)\b', m.get('expression', ''))} <= names]
    result['defaultFilters'] = [f for f in result.get('defaultFilters', []) if f['field'].split('.')[0] in names]
    result['dataSourceId'] = source_id
    scope = (ontology.get('sourceScopes') or {}).get(source_id) or ontology.get('scopeField') or ''
    result['scopeField'] = scope if scope.split('.')[0] in names else ''
    if str(result.get('timeField', '')).split('.')[0] not in names:
        result.pop('timeField', None)
    artifact = ontology.get('_wrenArtifact') or {}
    if 'sourceProjects' in artifact:
        result['_wrenArtifact'] = artifact['sourceProjects'].get(source_id)
        if not result['_wrenArtifact']:
            raise ValueError('已发布Wren产物缺少该数据源投影')
    elif len(names) != len(ontology.get('objects', [])):
        result.pop('_wrenArtifact', None)
    return result


def output_alias(reference):
    return reference.replace('.', '_')


def output_fields(query):
    return set(query.fields + query.dimensions + query.metrics)


def property_definition(reference, ontology):
    parts = reference.split('.')
    if len(parts) != 2:
        raise ValueError('跨源关联键必须引用已发布的对象属性')
    obj = next((o for o in ontology['objects'] if o['name'] == parts[0]), None)
    prop = next((p for p in (obj or {}).get('properties', []) if p['name'] == parts[1]), None)
    if not obj or not prop:
        raise ValueError(f'跨源关联键未发布：{reference}')
    return obj, prop


def type_family(value):
    value = str(value or '').lower().split('(')[0]
    if value in {'string', 'text', 'varchar', 'char', 'varchar2'}:
        return 'text'
    if value in {'int', 'integer', 'bigint', 'smallint'}:
        return 'integer'
    if value in {'decimal', 'numeric', 'number', 'float', 'double'}:
        return 'number'
    return value


def validate_source_steps(plan, ontology):
    steps = plan.source_steps
    if len(steps) < 2:
        raise ValueError('跨源查询至少包含两个步骤')
    if len({s.data_source_id for s in steps}) < 2:
        raise ValueError('同一数据源应使用普通QueryPlan，不能伪装为跨源查询')
    previous = {}
    relations = {r['name']: r for r in ontology.get('relations', [])}
    for index, step in enumerate(steps):
        if step.name in previous:
            raise ValueError('查询步骤名称重复')
        if step.data_source_id != step.query.data_source_id or step.query.domain != plan.domain:
            raise ValueError('步骤数据源或业务域绑定不一致')
        local = source_ontology(ontology, step.data_source_id)
        if plan.data_scope.get('branch_ids') and not local.get('scopeField'):
            raise ValueError(f'数据源 {step.data_source_id} 未配置机构范围映射sourceScopes，不能跨源绕过权限')
        if step.query.data_scope != plan.data_scope:
            raise ValueError('步骤不得改变用户授权数据范围')
        if index == 0 and step.inputs or index > 0 and not step.inputs:
            raise ValueError('第一步不能依赖后续步骤，后续步骤必须依赖已完成的步骤')
        for binding in step.inputs:
            source = previous.get(binding.source_step)
            if not source:
                raise ValueError('跨源步骤存在循环或前向引用')
            if binding.source_field not in output_fields(source.query):
                raise ValueError('关联键未包含在上一步输出中')
            source_obj, source_prop = property_definition(binding.source_field, ontology)
            target_obj, target_prop = property_definition(binding.target_field, ontology)
            if object_source(source_obj, ontology) != source.data_source_id or object_source(target_obj, ontology) != step.data_source_id:
                raise ValueError('关联键不属于绑定步骤的数据源')
            if source_prop.get('sensitive') or target_prop.get('sensitive'):
                raise ValueError('不能使用会被脱敏的敏感字段作为跨源关联键，请配置非敏感内部标识')
            family = type_family(source_prop.get('type'))
            if family != type_family(target_prop.get('type')) or family not in {'text', 'integer', 'number'}:
                raise ValueError('跨源关联键类型不兼容，须先在本体配置一致的内部标识')
            relation = relations.get(binding.relation)
            if not relation or {relation['from'], relation['to']} != {source_obj['name'], target_obj['name']}:
                raise ValueError('跨源关联缺少已发布关系，不能按同名字段猜测')
            condition = sqlglot.parse_one(relation['condition'], read='postgres')
            if not isinstance(condition, exp.EQ) or not all(isinstance(c, exp.Column) for c in (condition.this, condition.expression)):
                raise ValueError('跨源关联首版只支持明确的单键等值关系')
            pairs = {(c.table, c.name) for c in (condition.this, condition.expression)}
            expected = {(source_obj['name'], source_prop['column']), (target_obj['name'], target_prop['column'])}
            semantic = {tuple(binding.source_field.split('.')), tuple(binding.target_field.split('.'))}
            if pairs not in (expected, semantic):
                raise ValueError('步骤传递字段与已发布跨源关系不一致')
            if set(binding.include_source_fields) - output_fields(source.query):
                raise ValueError('补充列必须来自已完成步骤的实际输出')
            if binding.include_source_fields and binding.target_field not in output_fields(step.query):
                raise ValueError('补充上一步字段时，当前输出必须保留关联键，不能在汇总后猜测关联')
        previous[step.name] = step
    return {'execution': 'sequential_sources', 'steps': len(steps), 'result_step': steps[-1].name}
