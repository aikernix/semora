"""Resolve business intent into typed local/source execution contracts. No IO."""
from __future__ import annotations
from copy import deepcopy
import sqlglot
from sqlglot import exp
from semora_contracts import QueryIntent,QueryCalculation
from .stages import optional_objects
from .sources import object_source, source_ontology


class IntentPlanningError(ValueError):
    def __init__(self, message, *, kind='configuration', path='intent', options=None, reference=None):
        self.kind, self.path, self.options = kind, path, options or []
        self.reference=reference
        super().__init__(message)


class IntentCompiler:
    def __init__(self, ontology, scope):
        self.ontology, self.scope = ontology, scope
        self.objects = {o['name']: o for o in ontology['objects']}
        self.relations = {r['name']: r for r in ontology.get('relations', [])}
        self.published = {m['name']: m for m in ontology.get('metrics', [])}

    def field(self, ref):
        parts = ref.split('.')
        if len(parts) != 2 or parts[0] not in self.objects:
            raise IntentPlanningError(f'未发布的业务字段：{ref}', path='fields',reference=ref)
        props = [p for p in self.objects[parts[0]].get('properties', []) if p['name'] == parts[1]]
        if len(props) != 1:
            raise IntentPlanningError(f'未发布或不唯一的业务字段：{ref}', path='fields',reference=ref)
        return parts[0], props[0]

    def identity(self, name):
        obj = self.objects[name]
        props = [p for p in obj['properties'] if p.get('column') == obj.get('primaryKey')]
        if len(props) != 1:
            raise IntentPlanningError(f'{name} 未发布唯一的对象标识，不能稳定选取前N个对象')
        return name + '.' + props[0]['name']

    def path(self, start, target, roles):
        if start == target:
            return []
        unknown = set(roles) - self.relations.keys()
        if unknown:
            raise IntentPlanningError('未发布的业务关系：' + ', '.join(sorted(unknown)), path='relationship_roles')
        available = [r for r in self.relations.values() if not roles or r['name'] in roles]
        found = []
        def visit(current, seen, route):
            if len(found) >= 2:
                return
            if current == target:
                found.append(route)
                return
            if len(route) >= 8:
                return
            for relation in available:
                other = relation['to'] if relation['from'] == current else relation['from'] if relation['to'] == current else None
                if other and other not in seen:
                    visit(other, seen | {other}, route + [relation])
        visit(start, {start}, [])
        if not found:
            raise IntentPlanningError(f'已发布关系无法连接 {start} 与 {target}，请核对本体关系配置')
        if len(found) != 1:
            options = [[r['name'] for r in route] for route in found]
            raise IntentPlanningError(f'{start} 与 {target} 存在不同业务关联角色，需选择具体关系',
                                      kind='ambiguity', path='relationship_roles', options=options)
        return found[0]

    def tree(self, anchor, required, roles):
        selected = {}
        if required - self.objects.keys():
            raise IntentPlanningError('计算引用了未发布或未授权的业务对象')
        for target in sorted(required - {anchor}):
            for r in self.path(anchor, target, roles):
                selected[r['name']] = r
        # A union of unique paths must remain a tree (no silent duplicate roles).
        parent = {}
        def root(n):
            while n in parent:
                n = parent[n]
            return n
        for r in selected.values():
            a, b = root(r['from']), root(r['to'])
            if a == b:
                raise IntentPlanningError('业务关系角色形成环，请明确本次所用关系', kind='ambiguity', path='relationship_roles')
            parent[a] = b
        return list(selected.values())

    def keys(self, relation, source):
        target = relation['to'] if relation['from'] == source else relation['from']
        def split(node):
            if isinstance(node, exp.Paren):
                return split(node.this)
            if isinstance(node, exp.And):
                return split(node.this) + split(node.expression)
            if isinstance(node, exp.EQ) and all(isinstance(c, exp.Column) for c in (node.this, node.expression)):
                return [(node.this, node.expression)]
            raise IntentPlanningError('已发布关系必须为AND连接的等值字段映射：' + relation['name'])
        pairs = []
        for left, right in split(sqlglot.parse_one(relation['condition'], read='postgres')):
            columns = {left.table: left.name, right.table: right.name}
            refs = []
            for obj in (source, target):
                props = [p for p in self.objects[obj]['properties'] if columns.get(obj) in (p['name'], p.get('column'))]
                if len(props) != 1:
                    raise IntentPlanningError('关系关联列未映射为唯一已发布属性：' + relation['name'])
                refs.append(obj + '.' + props[0]['name'])
            pairs.append({'source_field': refs[0], 'target_field': refs[1]})
        return pairs

    def local(self, intent, anchor, fields, groups, filters, measures, metrics, roles, *, order=None, limit=None, required_refs=()):
        refs = fields + groups + [f['field'] for f in filters] + [m['field'] for m in measures] + list(required_refs)
        refs += [f['field'] for m in measures for f in m.get('filters',[])]
        required = {anchor} | {self.field(f)[0] for f in refs}
        for name in metrics:
            if name not in self.published:
                raise IntentPlanningError('未发布指标：' + name, path='metrics')
            required.update(c.table for c in sqlglot.parse_one(self.published[name]['expression'], read='postgres').find_all(exp.Column))
        source = object_source(self.objects[anchor], self.ontology)
        scope_field = source_ontology(self.ontology, source).get('scopeField')
        if self.scope.get('branch_ids') and scope_field:
            required.add(scope_field.split('.')[0])
        relations = self.tree(anchor, required, roles)
        nodes = required | {n for r in relations for n in (r['from'], r['to'])}
        if any(object_source(self.objects[n], self.ontology) != source for n in nodes):
            raise IntentPlanningError('该局部业务计算需要跨数据源聚合，当前执行能力无法证明其准确性', kind='capability')
        if fields and (measures or metrics) and not groups:
            groups=self.entity_groups(anchor,fields,relations)
        if (measures or metrics) and set(fields)-set(groups):
            raise IntentPlanningError('明细字段不属于指定汇总维度；请确认是按当前维度汇总，还是按对象逐条展示关联统计。',kind='ambiguity',path='group_by')
        query = dict(domain=self.ontology['domain'], data_source_id=source, intent=intent.summary,
                     grain=anchor, fields=fields, dimensions=groups, filters=filters,
                     metrics=metrics + [m['name'] for m in measures], calculations=measures,
                     relationship_path=[r['name'] for r in relations], stages=[],
                     order_by=order or [], limit=limit, assumptions=intent.assumptions,
                     time_range=None, data_scope=deepcopy(self.scope))
        optional=[r['name'] for r in relations if r['name'] in intent.optional_relationships]
        if optional:query['optional_relationships']=optional
        nullable=optional_objects(anchor,relations,optional)
        if any(f['field'].split('.')[0] in nullable for f in filters):
            raise IntentPlanningError('共同筛选会排除声明保留的未匹配主体；仅影响指标的条件应放入该指标filters',kind='invalid_intent',path='filters')
        self.preaggregate(query, relations)
        return query

    def entity_groups(self,anchor,fields,relations):
        """Infer entity-grain output only from its key and proven to-one attributes."""
        try:key=self.identity(anchor)
        except IntentPlanningError:
            raise IntentPlanningError('明细与统计同时输出时，需要明确每行代表的对象及其唯一标识。',kind='ambiguity',path='group_by')
        if key not in fields:
            raise IntentPlanningError('输出同时包含明细与聚合，但尚未明确每行的唯一标识；请确认结果按哪个对象逐条展示或按哪些维度汇总。',kind='ambiguity',path='group_by')
        reached={anchor};pending=list(relations)
        while pending:
            progress=False
            for relation in list(pending):
                sides=str(relation.get('cardinality') or '?:?').split(':')
                if len(sides)!=2:continue
                source,target=relation['from'],relation['to']
                if source in reached and sides[1]=='1':reached.add(target)
                elif target in reached and sides[0]=='1':reached.add(source)
                else:continue
                pending.remove(relation);progress=True
            if not progress:break
        if not {self.field(f)[0] for f in fields}<=reached:
            raise IntentPlanningError('部分明细可能在同一主体下存在多条，不能自动合并；请确认结果逐主体展示还是逐关联记录展示。',kind='ambiguity',path='group_by')
        return list(dict.fromkeys(fields))

    def preaggregate(self, query, relations):
        """Reduce independent many-side facts, then let QueryStages prove correctness."""
        if not query['metrics'] or not relations:
            return
        measures = {}
        def add(obj,function,field,filters):
            import json
            signature=(function,field,json.dumps(filters,sort_keys=True,default=str))
            items=measures.setdefault(obj,{})
            if signature not in items:
                items[signature]=dict(name='m_'+str(len(items)),function=function,field=field,filters=filters)
        for m in query['calculations']:
            if m['function']=='count_distinct':continue
            obj=self.field(m['field'])[0]
            if any(self.field(f['field'])[0]!=obj for f in m.get('filters',[])):
                raise IntentPlanningError('指标条件必须与指标原始字段属于同一对象',path='measures')
            add(obj,m['function'],m['field'],m.get('filters',[]))
        expressions = [self.published[m]['expression'] for m in query['metrics'] if m in self.published]
        for expression in expressions:
            for agg in sqlglot.parse_one(expression, read='postgres').find_all(exp.AggFunc):
                cols = list(agg.find_all(exp.Column))
                if len(cols) == 1 and isinstance(agg.this, exp.Column):
                    col = cols[0]
                    function = agg.key.lower()
                    if function in {'sum', 'avg', 'min', 'max', 'count'}:
                        add(col.table,function,col.table+'.'+col.name,[])
        if not measures:
            return
        # Only reduce a leaf many-side whose rows otherwise multiply another
        # fact's additive aggregate. Never invent a stage for an unmeasured leaf.
        additive = {obj for obj, items in measures.items() if any(k[0] in {'sum','avg','count'} for k in items)}
        nullable=optional_objects(query['grain'],relations,query.get('optional_relationships',[]))
        for obj, items in measures.items():
            incident = [r for r in relations if obj in (r['from'], r['to'])]
            if len(incident) != 1 or not (additive - {obj}):
                continue
            r = incident[0]
            sides = str(r.get('cardinality', '?:?')).split(':')
            side = sides[0 if r['from'] == obj else 1] if len(sides) == 2 else '?'
            if side != 'N':
                continue
            keys = [p['source_field'] for p in self.keys(r, obj)]
            if any(f.split('.')[0] == obj and f not in keys for f in query['fields'] + query['dimensions']):
                continue
            # Stage-local filters restrict the matched population (inner stage).
            local_filters = [f for f in query['filters'] if f['field'].split('.')[0] == obj]
            if any(f['operator'] in {'is_null','is_not_null'} or f.get('value') is None for f in local_filters):
                raise IntentPlanningError('当前预聚合不能证明空值筛选与原始外关联语义一致', kind='capability', path='filters')
            query['filters'] = [f for f in query['filters'] if f not in local_filters]
            query['stages'].append(dict(name='reduce_' + str(len(query['stages'])), source=obj,
                group_by=keys, measures=list(items.values()), filters=local_filters,
                join_type='inner' if local_filters or not (r.get('optional', False) or obj in nullable) else 'left'))

    def compile(self, intent):
        if intent.existence:
            return self.compile_existence(intent)
        if intent.subject not in self.objects:
            raise IntentPlanningError('请选择已发布的业务主体', path='subject')
        if len({m.name for m in intent.measures}) != len(intent.measures) or {m.name for m in intent.measures} & self.published.keys():
            raise IntentPlanningError('指标名称重复或覆盖已发布定义', path='measures')
        fields, groups = list(intent.fields), list(intent.group_by)
        measures = [m.model_dump(mode='json') for m in intent.measures]
        filters = [f.model_dump(mode='json') for f in intent.filters]
        refs = fields + groups + [f['field'] for f in filters] + [m['field'] for m in measures]
        refs += [f['field'] for m in measures for f in m.get('filters',[])]
        for ref in refs:
            self.field(ref)
        if not (fields or measures or intent.metrics):
            raise IntentPlanningError('未声明输出字段或指标', path='fields')
        required = {intent.subject} | {f.split('.')[0] for f in refs}
        for name in intent.metrics:
            if name not in self.published:
                raise IntentPlanningError('未发布指标：' + name, path='metrics')
            required.update(c.table for c in sqlglot.parse_one(self.published[name]['expression'], read='postgres').find_all(exp.Column))
        # First resolve semantic roles, before assigning any source or step.
        relations = self.tree(intent.subject, required, intent.relationship_roles)
        if not set(intent.optional_relationships)<={r['name'] for r in relations}:
            raise IntentPlanningError('可选关系必须属于本次使用的已发布关系',path='optional_relationships')
        if any(f.field not in set(intent.metrics)|{m.name for m in intent.measures} for f in intent.having):
            raise IntentPlanningError('分组后筛选只能引用本次输出的指标name',path='having')
        steps = []
        previous = None
        current = intent.subject
        selection = intent.selection
        if selection:
            if selection.object not in self.objects:
                raise IntentPlanningError('选取对象未发布', path='selection.object')
            key = self.identity(selection.object)
            self.field(selection.time_field)
            sf = [f.model_dump(mode='json') for f in selection.filters]
            if selection.unit == 'entities':
                sm = [dict(name='selection_time', function='max' if selection.direction == 'desc' else 'min', field=selection.time_field)]
                sq = self.local(intent, selection.object, [], [key], sf, sm, [], selection.relationship_roles,
                    order=[dict(field='selection_time',direction=selection.direction),dict(field=key,direction='asc')], limit=selection.limit)
            else:
                if selection.time_field.split('.')[0] != selection.object:
                    raise IntentPlanningError('记录选取的时间字段必须属于所选记录', path='selection.time_field')
                sq = self.local(intent, selection.object, list(dict.fromkeys([key, selection.time_field])), [], sf, [], [], selection.relationship_roles,
                    order=[dict(field=selection.time_field,direction=selection.direction),dict(field=key,direction='asc')],limit=selection.limit)
            steps.append(dict(name='select_members', label='按已确认顺序选取对象', data_source_id=sq['data_source_id'],query=sq,inputs=[]))
            previous = steps[-1]
            current = selection.object
            # Reuse selection window only when the business scope explicitly says so.
            if intent.record_scope == 'selection_window':
                filters = list({str(f): f for f in [*filters, *sf]}.values())
            elif intent.record_scope == 'selected_records' and selection.unit == 'entities':
                raise IntentPlanningError('选取不同实体后，需要明确查选取时段的记录还是全部关联记录',kind='ambiguity',path='record_scope')
        # Execution graph is a chain of source regions. Branching federated
        # reductions require an explicit capability, never an invented join.
        final_refs = fields + groups + [m['field'] for m in measures]
        final_nodes = {r.split('.')[0] for r in final_refs}
        for name in intent.metrics:
            final_nodes.add(self.published[name]['grain'])
        final_sources = {object_source(self.objects[n], self.ontology) for n in final_nodes}
        # The business subject describes the result, not the physical first
        # source. A foreign WHERE population must be reduced before reading
        # the result source. Do not make Analyst choose execution order.
        filter_prefix = False
        if (selection is None and len(final_sources) == 1
            and object_source(self.objects[intent.subject],self.ontology) in final_sources):
            result_source = next(iter(final_sources))
            foreign_filter_objects = sorted({f['field'].split('.')[0] for f in filters
                if object_source(self.objects[f['field'].split('.')[0]], self.ontology) != result_source})
            if foreign_filter_objects:
                filter_sources = {object_source(self.objects[n], self.ontology) for n in foreign_filter_objects}
                if len(filter_sources) == 1:
                    current = foreign_filter_objects[0]
                    filter_prefix = True
        start_source = object_source(self.objects[current], self.ontology)
        if not final_sources:
            final_sources = {start_source}
        # A requested foreign dimension may be enriched only if it comes from
        # a boundary-key-unique predecessor; runtime verifies that uniqueness.
        target_nodes = sorted(n for n in final_nodes if object_source(self.objects[n],self.ontology) != start_source)
        target = target_nodes[0] if target_nodes else intent.subject
        route = self.path(current, target, intent.relationship_roles)
        # All required final objects must live in the final source; predecessor
        # display fields are handled below, never used as foreign groupings.
        final_source = object_source(self.objects[target], self.ontology)
        if any(object_source(self.objects[m['field'].split('.')[0]],self.ontology) != final_source for m in measures):
            raise IntentPlanningError('跨源指标联合汇总尚不受支持，不能改变用户计算粒度',kind='capability',path='measures')
        incoming = None
        local_anchor = current
        local_roles = []
        for r in route:
            other = r['to'] if r['from'] == current else r['from']
            if object_source(self.objects[other],self.ontology) == object_source(self.objects[current],self.ontology):
                local_roles.append(r['name']); current = other
                continue
            pairs = self.keys(r, current)
            needed = [p['source_field'] for p in pairs]
            # Preserve requested predecessor display fields for enrichment.
            carry = [f for f in fields if object_source(self.objects[f.split('.')[0]],self.ontology) == object_source(self.objects[current],self.ontology)]
            if previous and previous is steps[-1] and previous['query']['data_source_id'] == object_source(self.objects[current],self.ontology) and not incoming and not local_roles and not any(f not in previous['query']['filters'] and object_source(self.objects[f['field'].split('.')[0]],self.ontology)==previous['data_source_id'] for f in filters):
                previous['query']['fields'] = list(dict.fromkeys(previous['query']['fields'] + needed + carry)) if not previous['query']['metrics'] else previous['query']['fields']
                if any(k not in previous['query']['fields'] + previous['query']['dimensions'] for k in needed):
                    # Entity selection must not add a non-grouped detail key.
                    incoming = dict(kind='identity',relation='__identity__',source_step=previous['name'],key_pairs=[dict(source_field=self.identity(local_anchor),target_field=self.identity(local_anchor))])
                else:
                    incoming = dict(relation=r['name'],source_step=previous['name'],key_pairs=pairs,include_source_fields=carry)
                    local_anchor = other; local_roles=[];current=other
                    continue
            lf = [f for f in filters if object_source(self.objects[f['field'].split('.')[0]],self.ontology) == object_source(self.objects[current],self.ontology)]
            q = self.local(intent,local_anchor,list(dict.fromkeys(needed+carry)),[],lf,[],[],intent.relationship_roles)
            if incoming:
                q['fields']=list(dict.fromkeys(q['fields']+[p['target_field'] for p in incoming['key_pairs']]))
            if previous and not incoming:
                incoming=dict(kind='identity',relation='__identity__',source_step=previous['name'],key_pairs=[dict(source_field=self.identity(local_anchor),target_field=self.identity(local_anchor))])
            step=dict(name='lookup_'+str(len(steps)),label='按已发布关系传递关联键',data_source_id=q['data_source_id'],query=q,inputs=[incoming] if incoming else [])
            steps.append(step);previous=step
            incoming=dict(relation=r['name'],source_step=step['name'],key_pairs=pairs,include_source_fields=carry)
            local_anchor=other;local_roles=[];current=other
        # Same-source Top N then aggregate/details uses a strict PK semi-join.
        if previous and incoming is None:
            key=self.identity(selection.object)
            incoming=dict(kind='identity',relation='__identity__',source_step=previous['name'],key_pairs=[dict(source_field=key,target_field=key)])
            local_anchor=selection.object
        foreign_fields=[f for f in fields if object_source(self.objects[f.split('.')[0]],self.ontology)!=final_source]
        if foreign_fields and (not incoming or set(foreign_fields)-set(incoming.get('include_source_fields',[]))):
            raise IntentPlanningError('所需前置字段无法在保持唯一关联的条件下传递',kind='capability',path='fields')
        business_groups=list(groups)
        regroup=None
        foreign_groups=[f for f in groups if object_source(self.objects[f.split('.')[0]],self.ontology)!=final_source]
        if foreign_groups:
            if not incoming:
                raise IntentPlanningError('跨源维度汇总缺少唯一关联依据',kind='capability',path='group_by')
            source_keys=[p['source_field'] for p in incoming['key_pairs']]
            target_keys=[p['target_field'] for p in incoming['key_pairs']]
            # GROUP BY a complete published PK permits its dependent display
            # attributes to be enriched after aggregation by the matching key.
            if any(self.identity(k.split('.')[0])!=k for k in source_keys):
                raise IntentPlanningError('当前跨源维度未包含完整唯一标识，不能通过关联键分组冒充原粒度',kind='capability',path='group_by')
            if not set(source_keys)<=set(foreign_groups):
                # Boundary rows may belong to the same business entity. First
                # aggregate per unique boundary, enrich its declared dimensions,
                # then merge complete partials at the requested grain.
                if (not measures or intent.metrics or set(fields)-set(business_groups)
                    or any(m['function'] not in {'sum','count','min','max'} for m in measures)):
                    raise IntentPlanningError('当前跨源再汇总仅支持明确分组下的SUM、COUNT、MIN、MAX；不能合并平均值、去重计数或额外明细',kind='capability',path='group_by')
                regroup=dict(group_by=business_groups,
                    measures=[dict(name=m['name'],function=m['function']) for m in measures],
                    order_by=[o.model_dump(mode='json') for o in intent.order_by],limit=intent.limit)
            source_step=next(s for s in steps if s['name']==incoming['source_step'])
            if source_step['query']['metrics']:
                raise IntentPlanningError('前置聚合不能无损携带额外维度',kind='capability',path='group_by')
            source_step['query']['fields']=list(dict.fromkeys(source_step['query']['fields']+foreign_groups))
            incoming['include_source_fields']=list(dict.fromkeys(incoming.get('include_source_fields',[])+foreign_groups))
            groups=list(dict.fromkeys([f for f in groups if f not in foreign_groups]+target_keys))
        local_filters=[f for f in filters if object_source(self.objects[f['field'].split('.')[0]],self.ontology)==final_source]
        # Reject a filter in an unvisited source instead of silently dropping it.
        assigned=[f for s in steps for f in s['query']['filters']]+local_filters
        if any(f not in assigned for f in filters):
            raise IntentPlanningError('筛选条件位于未覆盖的数据源；当前分步链不能无损执行该筛选',kind='capability',path='filters')
        final_fields=[f for f in fields if f not in foreign_fields]
        if incoming and incoming.get('include_source_fields'):
            keys=[p['target_field'] for p in incoming['key_pairs']]
            if measures or intent.metrics:
                if not set(keys)<=set(groups):
                    raise IntentPlanningError('逐记录汇总并输出记录属性时，最终业务分组必须保留该记录的唯一标识。请核对需求所要求的粒度并修正group_by，不改变用户口径。',kind='invalid_intent',path='group_by')
            else:
                final_fields=list(dict.fromkeys(final_fields+keys))
        final_anchor=intent.subject if filter_prefix else local_anchor
        q=self.local(intent,final_anchor,final_fields,groups,local_filters,measures,list(intent.metrics),intent.relationship_roles,
                     order=[] if regroup else [o.model_dump(mode='json') for o in intent.order_by],limit=None if regroup else intent.limit,
                     required_refs=[p['target_field'] for p in incoming['key_pairs']] if incoming else [])
        if regroup and (intent.having or any(m.null_value is not None for m in intent.measures)):
            raise IntentPlanningError('跨源再汇总后的筛选与空值填充尚未支持，不能在局部结果提前应用',kind='capability',path='having')
        if intent.optional_relationships and any(r in intent.optional_relationships for s in steps for b in s['inputs'] for r in [b['relation']]):
            raise IntentPlanningError('跨源边界暂不支持保留未匹配主体；不得退化为半连接',kind='capability',path='optional_relationships')
        if intent.having:q['having']=[f.model_dump(mode='json') for f in intent.having]
        if steps:
            steps.append(dict(name='result',label='生成最终查询结果',data_source_id=q['data_source_id'],query=q,inputs=[incoming],regroup=regroup))
            if len(steps)>8:
                raise IntentPlanningError('分步查询超过8步上限',kind='capability')
        value={**q,'summary':intent.summary,'subject':intent.subject,'analysis_steps':intent.analysis_steps,
               'output_fields':list(dict.fromkeys(fields+business_groups+q['metrics'])),'query_intent':intent.model_dump(mode='json'),
               'execution_mode':'sequential_queries' if steps else 'single_query'}
        if steps:
            value['source_steps']=steps
        return value

    def compile_existence(self,intent):
        rule=intent.existence
        relation=self.relations.get(rule.relation)
        if not relation or rule.anchor not in (relation['from'],relation['to']):
            raise IntentPlanningError('存在性条件必须指定已发布关系及其来源对象',path='existence')
        if intent.metrics or intent.measures or intent.group_by or not intent.fields:
            raise IntentPlanningError('当前存在性筛选支持对象明细；聚合必须在完成筛选后另行规划',kind='capability',path='existence')
        target=relation['to'] if relation['from']==rule.anchor else relation['from']
        if any(self.field(f.field)[0]!=target for f in rule.filters):
            raise IntentPlanningError('存在性目标条件必须属于该关系的目标对象',path='existence.filters')
        pairs=self.keys(relation,rule.anchor)
        identity=self.identity(intent.subject)
        source_id=object_source(self.objects[rule.anchor],self.ontology)
        refs=[*intent.fields,identity,*[p['source_field'] for p in pairs]]
        if any(object_source(self.objects[self.field(f)[0]],self.ontology)!=source_id for f in refs):
            raise IntentPlanningError('存在性筛选的输出和来源标识需位于同一源区域',kind='capability',path='existence')
        base=intent.model_copy(deep=True)
        base.existence=None
        base.fields=list(dict.fromkeys(refs))
        base.order_by=[];base.limit=None
        value=self.compile(base)
        steps=value.get('source_steps') or [dict(name='membership_source',label='读取待核验对象及关联键',
            data_source_id=source_id,query={k:v for k,v in value.items() if k in {
                'domain','data_source_id','intent','grain','fields','dimensions','filters','metrics','calculations',
                'relationship_path','stages','order_by','limit','assumptions','time_range','data_scope'}},inputs=[])]
        previous=steps[-1]
        if previous['data_source_id']!=source_id:
            raise IntentPlanningError('存在性筛选来源区域不一致',kind='capability',path='existence')
        target_id=object_source(self.objects[target],self.ontology)
        query=self.local(intent,target,[p['target_field'] for p in pairs],[p['target_field'] for p in pairs],
            [f.model_dump(mode='json') for f in rule.filters],[],[],[],limit=None)
        membership=dict(source_step=previous['name'],group_by=[identity],fields=list(intent.fields),
            present=rule.present,order_by=[o.model_dump(mode='json') for o in intent.order_by],limit=intent.limit)
        steps.append(dict(name='existence_result',label='核验关联记录存在性并筛选对象',data_source_id=target_id,
            query=query,inputs=[dict(relation=rule.relation,source_step=previous['name'],key_pairs=pairs)],membership=membership))
        if len(steps)>8:raise IntentPlanningError('分步查询超过8步上限',kind='capability')
        value.update(source_steps=steps,query_intent=intent.model_dump(mode='json'),
            fields=list(intent.fields),order_by=membership['order_by'],limit=intent.limit,
            output_fields=list(intent.fields),execution_mode='sequential_queries')
        return value


def compile_intent(intent, ontology, scope):
    return IntentCompiler(ontology,scope).compile(QueryIntent.model_validate(intent))
