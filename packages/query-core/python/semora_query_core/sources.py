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
    if len({s.data_source_id for s in steps}) < 2 and plan.execution_mode != 'sequential_queries':
        raise ValueError('同一数据源应使用普通QueryPlan，不能伪装为跨源查询')
    previous = {}
    relations = {r['name']: r for r in ontology.get('relations', [])}
    for index, step in enumerate(steps):
        if step.name in previous:
            raise ValueError('查询步骤名称重复')
        if step.data_source_id != step.query.data_source_id or step.query.domain != plan.domain:
            raise ValueError('步骤数据源或业务域绑定不一致')
        references = list(dict.fromkeys(step.query.dimensions + step.query.fields)) + step.query.metrics
        aliases = [output_alias(reference) for reference in references]
        if len(aliases) != len(set(aliases)):
            raise ValueError('步骤输出列别名冲突，请为指标选择不与字段冲突的名称')
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
            relation = relations.get(binding.relation)
            identity = binding.kind == 'identity'
            if identity:
                if binding.relation != '__identity__' or source.data_source_id != step.data_source_id or len(binding.pairs) != 1:
                    raise ValueError('对象标识传递只能用于同源单一已发布主键')
                pair = binding.pairs[0]
                obj, prop = property_definition(pair.source_field, ontology)
                if pair.source_field != pair.target_field or not obj.get('primaryKey') or prop.get('column') != obj['primaryKey']:
                    raise ValueError('对象标识传递必须使用同一个已发布主键，不能猜测同名字段')
                relation = {'from':obj['name'], 'to':obj['name'], 'condition': f"{obj['name']}.{prop['column']} = {obj['name']}.{prop['column']}"}
            if not relation:
                raise ValueError('跨源关联缺少已发布关系，不能按同名字段猜测')
            condition = sqlglot.parse_one(relation['condition'], read='postgres')
            def equalities(node):
                if isinstance(node, exp.Paren):
                    return equalities(node.this)
                if isinstance(node, exp.And):
                    return equalities(node.this) + equalities(node.expression)
                if not isinstance(node, exp.EQ) or not all(isinstance(c, exp.Column) for c in (node.this, node.expression)):
                    raise ValueError('跨源关联只支持AND连接的等值字段映射')
                return [frozenset((c.table, c.name) for c in (node.this, node.expression))]
            remaining = equalities(condition)
            for pair in binding.pairs:
                try:
                    source_obj, source_prop = property_definition(pair.source_field, ontology)
                    target_obj, target_prop = property_definition(pair.target_field, ontology)
                except ValueError as exc:
                    columns = {(c.table, c.name) for c in condition.find_all(exp.Column)}
                    candidates = [f"{obj['name']}.{prop['name']}" for obj in ontology['objects'] for prop in obj.get('properties', [])
                                  if (obj['name'], prop.get('column')) in columns or (obj['name'], prop['name']) in columns]
                    raise ValueError(f'步骤{step.name}.inputs(source_step={binding.source_step})字段引用无效：source_field={pair.source_field}, target_field={pair.target_field}。必须使用完整对象.属性；已发布关系{binding.relation}可核验字段：{candidates}。请核对来源与目标方向，不自动映射。') from exc
                if object_source(source_obj, ontology) != source.data_source_id or object_source(target_obj, ontology) != step.data_source_id:
                    raise ValueError('关联键不属于绑定步骤的数据源')
                if pair.source_field not in output_fields(source.query):
                    raise ValueError(f'步骤{step.name}.inputs(source_step={binding.source_step}).source_field={pair.source_field}：已发布关联键未包含在上一步输出中；请把该字段加入{binding.source_step}.query.fields或dimensions。当前输出：{sorted(output_fields(source.query))}')
                if source_prop.get('sensitive') or target_prop.get('sensitive'):
                    raise ValueError('不能使用会被脱敏的敏感字段作为跨源关联键，请配置非敏感内部标识')
                family = type_family(source_prop.get('type'))
                if family != type_family(target_prop.get('type')) or family not in {'text', 'integer', 'number'}:
                    raise ValueError('跨源关联键类型不兼容，须先在本体配置一致的内部标识')
                if {relation['from'], relation['to']} != {source_obj['name'], target_obj['name']}:
                    raise ValueError(f'步骤{step.name}.inputs.relation={binding.relation}只连接{relation["from"]}与{relation["to"]}，不能用{pair.source_field}→{pair.target_field}冒充边界键。请复制transfer_templates中该关系的完整key_pairs，并让source_step输出真实source_field；若前步只有实体键，先通过已发布关系增加同源明细键查询步骤，不需要用户确认技术拆步。不能按同名字段猜测。')
                expected = frozenset(((source_obj['name'], source_prop['column']), (target_obj['name'], target_prop['column'])))
                semantic = frozenset((tuple(pair.source_field.split('.')), tuple(pair.target_field.split('.'))))
                match = next((item for item in remaining if item in (expected, semantic)), None)
                if match is None:
                    raise ValueError('步骤传递字段与已发布跨源关系不一致')
                remaining.remove(match)
            if remaining:
                raise ValueError('步骤必须传递已发布关系的全部复合键')
            if set(binding.include_source_fields) - output_fields(source.query):
                raise ValueError('补充列必须来自已完成步骤的实际输出')
            if binding.include_source_fields and any(pair.target_field not in output_fields(step.query) for pair in binding.pairs):
                missing=[pair.target_field for pair in binding.pairs if pair.target_field not in output_fields(step.query)]
                raise ValueError(f'步骤{step.name}.inputs.include_source_fields补充上一步字段时，当前输出必须保留关联键：{missing}，不能在汇总后猜测关联。若当前按另一粒度汇总且所需维度已在目标源，应移除非必需include_source_fields，不能硬加分组键改变用户口径。')
        previous[step.name] = step
        if step.membership:
            spec=step.membership
            if index!=len(steps)-1 or step.regroup or len(step.inputs)!=1:
                raise ValueError('存在性集合筛选只能用于最终步骤')
            binding=step.inputs[0]
            source=previous.get(spec.source_step)
            if source is None or spec.source_step!=binding.source_step or source is step:
                raise ValueError('存在性筛选来源必须与已执行依赖一致')
            if (step.query.metrics or step.query.stages or step.query.order_by
                or step.query.limit is not None or source.query.metrics):
                raise ValueError('存在性检查不得使用截取或聚合后的关联集合')
            if output_fields(step.query)!={p.target_field for p in binding.pairs}:
                raise ValueError('存在性目标查询必须完整输出约定关联键')
            if not set(spec.fields+spec.group_by)<=output_fields(source.query):
                raise ValueError('存在性筛选输出和分组必须来自来源真实输出')
            for ref in spec.fields+spec.group_by:
                _,prop=property_definition(ref,ontology)
                if prop.get('sensitive'):raise ValueError('存在性输出标识不能使用脱敏字段')
            group_props=[property_definition(f,ontology) for f in spec.group_by]
            if not any(p.get('column')==o.get('primaryKey') for o,p in group_props):
                raise ValueError('存在性筛选必须保留业务主体的唯一标识')
            if any(o.field not in spec.fields for o in spec.order_by):
                raise ValueError('存在性结果排序必须引用实际输出字段')
        if step.regroup:
            rg=step.regroup
            if index != len(steps)-1 or not step.inputs or not step.inputs[0].include_source_fields:
                raise ValueError('跨源再汇总只能位于最后一步且必须有唯一键补充维度')
            binding=step.inputs[0]
            source=previous[binding.source_step]
            for pair in binding.pairs:
                obj,prop=property_definition(pair.source_field,ontology)
                if prop.get('column') != obj.get('primaryKey'):
                    raise ValueError('跨源再汇总必须由完整已发布主键唯一映射业务维度')
            if source.query.metrics or step.query.limit is not None or step.query.order_by:
                raise ValueError('跨源再汇总前不得截取或排序部分聚合结果')
            foreign=set(binding.include_source_fields)
            local=set(step.query.dimensions)
            if not set(rg.group_by)<=foreign|local or not set(rg.group_by)&foreign:
                raise ValueError('再汇总分组必须来自实际补充维度或局部分组')
            if set(step.query.fields)-local:
                raise ValueError('再汇总不得携带未分组明细')
            expected={m.name:m.function for m in step.query.calculations}
            if ({m.name:m.function for m in rg.measures}!=expected
                or set(expected)!=set(step.query.metrics) or len(rg.measures)!=len(expected)):
                raise ValueError('再汇总必须完整匹配可合并的局部计算指标')
            if len(set(rg.group_by))!=len(rg.group_by):
                raise ValueError('再汇总维度重复')
            final_columns=[output_alias(f) for f in rg.group_by]+list(expected)
            if len(set(final_columns))!=len(final_columns):
                raise ValueError('再汇总最终输出列别名冲突')
            if any(o.field not in set(rg.group_by)|set(expected) for o in rg.order_by):
                raise ValueError('再汇总排序字段必须位于最终业务输出')
    return {'execution': 'sequential_sources', 'steps': len(steps), 'result_step': steps[-1].name,
            'key_transfer': {'semantics': 'deduplicated_tuple_semi_join', 'null_keys': 'excluded',
                'source_key_uniqueness_required': any(b.include_source_fields for s in steps for b in s.inputs),
                'enrichment': '来源字段补充仍在运行时验证键唯一性；重复键只用于过滤时先去重，不放大目标行'}}
