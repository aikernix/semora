"""Typed pre-aggregation and cardinality checks, independent of the LLM."""
from __future__ import annotations

import re

import sqlglot
from sqlglot import exp


def normalized_expression(value):
    value = re.sub(r'SUM\((\w+\.\w+)\s+WHERE\s+(.+?)\)',
                   lambda m: f'SUM(CASE WHEN {m[2]} THEN {m[1]} ELSE 0 END)', value, flags=re.I)
    return sqlglot.parse_one(value, read='postgres')


def predicate(item):
    field = exp.column(item.field.split('.', 1)[1], table=item.field.split('.', 1)[0])
    if item.operator in {'is_null', 'is_not_null'}:
        check = exp.Is(this=field, expression=exp.Null())
        return exp.Not(this=check) if item.operator == 'is_not_null' else check
    classes = {'eq': exp.EQ, 'ne': exp.NEQ, 'gt': exp.GT, 'gte': exp.GTE, 'lt': exp.LT, 'lte': exp.LTE}
    if item.operator in classes:
        return classes[item.operator](this=field, expression=exp.convert(item.value))
    if item.operator == 'in' and isinstance(item.value, list) and 0 < len(item.value) <= 1000:
        return exp.In(this=field, expressions=[exp.convert(v) for v in item.value])
    if item.operator == 'between' and isinstance(item.value, list) and len(item.value) == 2:
        return exp.Between(this=field, low=exp.convert(item.value[0]), high=exp.convert(item.value[1]))
    raise ValueError('阶段筛选的运算符或参数无效')


class QueryStages:
    def __init__(self, plan, ontology, metrics):
        self.plan, self.ontology, self.metrics = plan, ontology, metrics
        self.objects = {o['name']: o for o in ontology.get('objects', [])}
        self.relations = {r['name']: r for r in ontology.get('relations', [])}
        self.columns = {f"{o['name']}.{p['name']}": str(p['column']) for o in self.objects.values() for p in o.get('properties', []) if p.get('column')}
        # Published relation keys and primary keys are usable as internal stage
        # grouping keys even when they are not exposed as display attributes.
        for name, obj in self.objects.items():
            if obj.get('primaryKey'):
                self.columns.setdefault(f'{name}.id', obj['primaryKey'])
        self.conditions = {}
        for name in plan.relationship_path:
            if name not in self.relations:
                raise ValueError(f'本体未发布关系：{name}')
            tree = sqlglot.parse_one(self.relations[name]['condition'], read='postgres')
            self.conditions[name] = tree
            for col in tree.find_all(exp.Column):
                if col.table not in self.objects:
                    raise ValueError('关系引用了未知对象')
                self.columns.setdefault(f'{col.table}.{col.name}', col.name)
        self.by_source, self.virtual_fields, self.cte_names = {}, {}, {}
        self.stage_keys, self.replacements, self.weights = {}, {}, {}
        names = set()
        for index, stage in enumerate(plan.stages):
            if stage.name in names or stage.name in self.objects or stage.source in self.by_source:
                raise ValueError('阶段名称必须唯一且不得与本体对象重名；首版每个源对象只允许一个预聚合阶段')
            if stage.source not in self.objects:
                raise ValueError(f'阶段 {stage.name} 引用未发布对象：{stage.source}')
            names.add(stage.name)
            self.by_source[stage.source] = stage
            self.cte_names[stage.source] = f'__semora_s{index}_{stage.name[:32]}'
            keys = [self.source_column(stage, field) for field in stage.group_by]
            if len(set(keys)) != len(keys):
                raise ValueError(f'阶段 {stage.name} 的分组键重复')
            self.stage_keys[stage.source] = set(keys)
            used = {key.casefold() for key in keys}
            for item in stage.filters:
                self.source_column(stage, item.field)
                predicate(item)
            for measure_index, measure in enumerate(stage.measures):
                if measure.field:
                    self.source_column(stage, measure.field)
                elif measure.function != 'count':
                    raise ValueError(f'阶段 {stage.name} 的 {measure.name} 缺少源字段')
                for item in measure.filters:
                    self.source_column(stage, item.field)
                    predicate(item)
                if measure.name.casefold() in used:
                    raise ValueError(f'阶段 {stage.name} 的指标名称与分组键或其他指标冲突')
                used.add(measure.name.casefold())
                virtual = f'{stage.name}.{measure.name}'
                self.virtual_fields[virtual] = f'{stage.source}.{measure.name}'
                merged = f'SUM({virtual})' if measure.function in {'sum', 'count'} else f'{measure.function.upper()}({virtual})'
                if measure.function == 'avg':
                    sum_name, count_name = f'__semora_w{measure_index}_sum', f'__semora_w{measure_index}_count'
                    if {sum_name.casefold(), count_name.casefold()} & used:
                        raise ValueError('阶段指标名称与内部加权平均列冲突')
                    used.update({sum_name.casefold(), count_name.casefold()})
                    self.weights[virtual] = (sum_name, count_name)
                    for column in (sum_name, count_name):
                        self.virtual_fields[f'{stage.name}.{column}'] = f'{stage.source}.{column}'
                    merged = f'1.0 * SUM({stage.name}.{sum_name}) / NULLIF(SUM({stage.name}.{count_name}), 0)'
                # A global distinct set cannot be recovered from per-key counts.
                if measure.function != 'count_distinct':
                    for effective_filters in (measure.filters, stage.filters + measure.filters):
                        key = self.aggregate_sql(measure, measure.field or '*', effective_filters).sql(dialect='postgres', normalize=True)
                        if key in self.replacements and self.replacements[key].sql() != normalized_expression(merged).sql():
                            raise ValueError('多个阶段指标对同一源聚合定义了不同口径，请消除歧义')
                        self.replacements[key] = normalized_expression(merged)
        if plan.metrics and set(plan.fields) - set(plan.dimensions):
            raise ValueError('汇总粒度冲突：fields 包含非分组明细字段；聚合源字段放入 calculations，最终分组仅由 dimensions 决定，明细需另开查询')
        self.effective = self.effective_cardinalities()
        self.expressions = {}
        for metric_name in plan.metrics:
            tree = normalized_expression(metrics[metric_name]['expression'])
            for aggregate in list(tree.find_all(exp.AggFunc)):
                for column in aggregate.find_all(exp.Column):
                    declared = next((s for s in plan.stages if s.name == column.table), None)
                    if declared and isinstance(aggregate, exp.Sum) and any(m.name == column.name and m.function == 'count_distinct' for m in declared.measures):
                        raise ValueError('不能相加阶段COUNT DISTINCT来代替全局去重；请保留去重标识并在最终粒度计算')
                key = aggregate.sql(dialect='postgres', normalize=True)
                if key in self.replacements:
                    replacement = self.replacements[key].copy()
                    if aggregate is tree:
                        tree = replacement
                    else:
                        aggregate.replace(replacement)
                elif isinstance(aggregate, exp.Avg):
                    for column in aggregate.find_all(exp.Column):
                        stage = next((s for s in plan.stages if s.name == column.table), None)
                        if stage and any(m.name == column.name and m.function == 'avg' for m in stage.measures):
                            raise ValueError('不能直接AVG预聚合后的平均值；请对原始字段定义AVG，由编译器携带SUM和COUNT加权合并')
            for column in tree.find_all(exp.Column):
                self.check_final_reference(f'{column.table}.{column.name}')
            self.check_fanout(tree, metric_name)
            self.expressions[metric_name] = tree
        for field in plan.dimensions + plan.fields + [f.field for f in plan.filters]:
            if field in self.virtual_fields:
                raise ValueError('阶段指标只能作为聚合输入，不能直接作为最终分组字段或行过滤；请使用阶段filters或明确最终统计口径')
            self.check_final_reference(field)
        if plan.grain and plan.grain not in self.objects:
            raise ValueError('grain必须使用当前本体对象的name')
        required = self.required_objects() | {f.split('.', 1)[0] for f in plan.dimensions + plan.fields + [f.field for f in plan.filters]}
        if plan.data_scope.get('branch_ids'):
            scope = ontology.get('scopeField')
            if not scope:
                raise ValueError('缺少授权数据范围的scopeField映射')
            self.check_final_reference(scope)
            required.add(scope.split('.', 1)[0])
        anchor = plan.grain or next(iter(sorted(required)), None)
        reached, queue = {anchor}, [anchor]
        while queue:
            for other, _, _ in self.effective.get(queue.pop(), []):
                if other not in reached:
                    reached.add(other)
                    queue.append(other)
        if required - reached:
            raise ValueError('关系路径未覆盖对象：' + ', '.join(sorted(required - reached)))

    def source_column(self, stage, field):
        if field.split('.', 1)[0] != stage.source or field not in self.columns:
            raise ValueError(f'阶段 {stage.name} 只能使用源对象 {stage.source} 的已发布字段或关系键，不能使用 {field}；预聚合发生在关联之前。若最终维度来自其他对象，请按源对象关联键分组，再沿relationship_path关联该维度；不要把跨表维度放入该阶段group_by。')
        return self.columns[field]

    def check_final_reference(self, field):
        if field in self.virtual_fields:
            return
        obj = field.split('.', 1)[0]
        if obj in self.by_source and self.columns.get(field) not in self.stage_keys[obj]:
            raise ValueError(f'字段 {field} 已被阶段预聚合，不能继续引用原始明细；请将所需聚合定义为阶段measures，并用阶段名.指标名引用')

    @staticmethod
    def aggregate_sql(measure, field, filters):
        argument = field
        if filters:
            condition = exp.and_(*(predicate(f) for f in filters)).sql(dialect='postgres')
            argument = f'CASE WHEN {condition} THEN {field if field != "*" else "1"} ELSE {"0" if measure.function == "sum" else "NULL"} END'
        function = 'COUNT' if measure.function == 'count_distinct' else measure.function.upper()
        return normalized_expression(f'{function}({"DISTINCT " if measure.function == "count_distinct" else ""}{argument})')

    def effective_cardinalities(self):
        graph, connected = {}, {}
        def root(name):
            connected.setdefault(name, name)
            if connected[name] != name:
                connected[name] = root(connected[name])
            return connected[name]
        for name, condition in self.conditions.items():
            relation = self.relations[name]
            source, target = relation['from'], relation['to']
            if root(source) == root(target):
                raise ValueError('关系路径包含环或同一对象的多个角色，首版需选择明确的单一路径')
            connected[root(source)] = root(target)
            if condition.find(exp.Or) or condition.find(exp.Not):
                raise ValueError('聚合关系只支持已发布的AND等值关联；OR或否定关联无法证明不会重复累计')
            equality_columns, all_columns = {}, {}
            for col in condition.find_all(exp.Column):
                all_columns.setdefault(col.table, set()).add(self.columns.get(f'{col.table}.{col.name}', col.name))
            for eq in condition.find_all(exp.EQ):
                for col, other in ((eq.this, eq.expression), (eq.expression, eq.this)):
                    if isinstance(col, exp.Column) and isinstance(other, (exp.Column, exp.Literal, exp.Boolean)):
                        equality_columns.setdefault(col.table, set()).add(self.columns.get(f'{col.table}.{col.name}', col.name))
            sides = str(relation.get('cardinality') or '?:?').split(':')
            if len(sides) != 2:
                sides = ['?', '?']
            multiplicities = {}
            for obj, side in zip((source, target), sides):
                if obj in self.by_source:
                    keys = self.stage_keys[obj]
                    if not keys <= equality_columns.get(obj, set()) or not all_columns.get(obj, set()) <= keys:
                        raise ValueError(f'阶段 {self.by_source[obj].name} 的分组键不能证明关联唯一；group_by必须由本关系的等值关联列完整覆盖，且保留所有关联所需列')
                    side = '1'
                elif side == '?' and self.objects[obj].get('primaryKey') in equality_columns.get(obj, set()):
                    side = '1'
                multiplicities[obj] = side
            graph.setdefault(source, []).append((target, multiplicities[target], name))
            graph.setdefault(target, []).append((source, multiplicities[source], name))
        return graph

    def check_fanout(self, tree, metric):
        for aggregate in tree.find_all(exp.AggFunc):
            if isinstance(aggregate, (exp.Min, exp.Max)) or isinstance(aggregate, exp.Count) and isinstance(aggregate.this, exp.Distinct):
                continue
            columns = list(aggregate.find_all(exp.Column))
            origins = {self.by_source[c.table].source if c.table in self.by_source else c.table for c in columns}
            origins = {next((s.source for s in self.plan.stages if s.name == obj), obj) for obj in origins}
            if not origins:
                if self.conditions:
                    raise ValueError(f'指标 {metric} 在多表查询中必须明确计数源字段，不能使用COUNT(*)')
                continue
            for origin in origins:
                seen, queue = {origin}, [origin]
                while queue:
                    current = queue.pop()
                    for other, multiplicity, relation in self.effective.get(current, []):
                        if other in seen:
                            continue
                        if multiplicity != '1':
                            raise ValueError(f'指标 {metric} 存在关联放大风险：{current} → {other} 经 {relation} 不能保证每行最多匹配一行；请对多端配置stages预聚合，不能只修改说明或使用SUM(DISTINCT 金额)规避')
                        seen.add(other)
                        queue.append(other)

    def required_objects(self):
        objects = {s.source for s in self.plan.stages}
        for expression in self.expressions.values():
            for col in expression.find_all(exp.Column):
                objects.add(next((s.source for s in self.plan.stages if s.name == col.table), col.table))
        return objects

    def sensitive_metric(self, name, properties):
        refs = {f'{c.table}.{c.name}' for c in normalized_expression(self.metrics[name]['expression']).find_all(exp.Column)}
        expanded = set(refs)
        for stage in self.plan.stages:
            for measure in stage.measures:
                if f'{stage.name}.{measure.name}' in refs and measure.field:
                    expanded.add(measure.field)
        return any(properties.get(ref, {}).get('sensitive') for ref in expanded)

    def summary(self):
        return {'output_group_by': self.plan.dimensions, 'fanout_checked': True,
                'stages': [{'name': s.name, 'source': s.source, 'group_by': s.group_by, 'join_type': s.join_type, 'measures': [m.name for m in s.measures]} for s in self.plan.stages],
                'execution': 'single_statement'}

    def render(self, physical_ref, parameters):
        ctes = []
        def bound_condition(filters, prefix):
            rendered = []
            for index, item in enumerate(filters):
                predicate(item)
                if item.operator in {'is_null', 'is_not_null'}:
                    rendered.append(f"{physical_ref(item.field)} IS {'NOT ' if item.operator == 'is_not_null' else ''}NULL")
                    continue
                # Replace literal parameters before rendering, without ever
                # interpolating a user's value into SQL text.
                values = item.value if item.operator in ('in', 'between') else [item.value]
                names = []
                for value_index, value in enumerate(values):
                    name = f'{prefix}_{index}_{value_index}'
                    parameters[name] = value
                    names.append(f'%({name})s')
                left = physical_ref(item.field)
                op = {'eq': '=', 'ne': '<>', 'gt': '>', 'gte': '>=', 'lt': '<', 'lte': '<='}
                if item.operator in op:
                    rendered.append(f'{left} {op[item.operator]} {names[0]}')
                elif item.operator == 'in':
                    rendered.append(f'{left} IN ({", ".join(names)})')
                else:
                    rendered.append(f'{left} BETWEEN {names[0]} AND {names[1]}')
            return ' AND '.join(rendered)
        for stage in self.plan.stages:
            columns = [f'{physical_ref(key)} AS {self.columns[key]}' for key in stage.group_by]
            for index, measure in enumerate(stage.measures):
                argument = physical_ref(measure.field) if measure.field else '*'
                condition = bound_condition(measure.filters, f's{stage.name}_m{index}')
                def aggregate(function):
                    value = argument
                    if condition:
                        value = f'CASE WHEN {condition} THEN {argument if argument != "*" else "1"} ELSE {"0" if function == "sum" else "NULL"} END'
                    return f'{"COUNT" if function == "count_distinct" else function.upper()}({"DISTINCT " if function == "count_distinct" else ""}{value})'
                columns.append(f'{aggregate(measure.function)} AS {measure.name}')
                if measure.function == 'avg':
                    sum_name, count_name = self.weights[f'{stage.name}.{measure.name}']
                    columns += [f'{aggregate("sum")} AS {sum_name}', f'{aggregate("count")} AS {count_name}']
            sql = 'SELECT ' + ', '.join(columns) + f" FROM {self.objects[stage.source]['table']} {stage.source}"
            condition = bound_condition(stage.filters, f's{stage.name}_f')
            if condition:
                sql += ' WHERE ' + condition
            sql += ' GROUP BY ' + ', '.join(physical_ref(key) for key in stage.group_by)
            ctes.append(f'{self.cte_names[stage.source]} AS ({sql})')
        return ('WITH ' + ',\n'.join(ctes) + '\n') if ctes else ''


def metric_catalog(plan, ontology):
    metrics = {m['name']: m for m in ontology.get('metrics', [])}
    for calculation in plan.calculations:
        if calculation.name in metrics:
            raise ValueError('临时计算不能覆盖已发布指标')
        field = calculation.field or '*'
        expression = f'COUNT(DISTINCT {field})' if calculation.function == 'count_distinct' else f'{calculation.function.upper()}({field})'
        metrics[calculation.name] = {'expression': expression, 'format': 'number'}
    if any(name not in metrics for name in plan.metrics):
        raise ValueError('QueryPlan 引用了未发布指标或未定义临时指标')
    return metrics


def validate_query_shape(plan, ontology):
    if getattr(plan, 'source_steps', []):
        from semora_agent.multisource import source_ontology, validate_source_steps
        from semora_contracts import QueryPlan, QueryFilter
        result = validate_source_steps(plan, ontology)
        for step in plan.source_steps:
            local = QueryPlan.model_validate(step.query.model_dump(mode='json'))
            local.filters += [QueryFilter(field=b.target_field, operator='in', value=[None]) for b in step.inputs]
            validate_query_shape(local, source_ontology(ontology, step.data_source_id))
        return result
    return QueryStages(plan, ontology, metric_catalog(plan, ontology)).summary()


def stage_group_keys(name, ontology):
    obj = next(o for o in ontology.get('objects', []) if o['name'] == name)
    by_column = {p.get('column'): f"{name}.{p['name']}" for p in obj.get('properties', [])}
    keys = {f"{name}.{p['name']}" for p in obj.get('properties', [])}
    if obj.get('primaryKey'):
        keys.add(by_column.get(obj['primaryKey'], f'{name}.id'))
    for relation in ontology.get('relations', []):
        if name not in (relation['from'], relation['to']):
            continue
        for column in sqlglot.parse_one(relation['condition'], read='postgres').find_all(exp.Column):
            if column.table == name:
                keys.add(by_column.get(column.name, f'{name}.{column.name}'))
    return sorted(keys)
