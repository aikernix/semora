from semora_agent.prompts import CONTEXT_SELECTION
from semora_agent.legacy_prompts import INFORMATION_ASSESSMENT
"""Prepare bounded, version-bound planning inputs. Never read business rows."""
import asyncio
import json
import re
from copy import deepcopy

from pydantic import BaseModel,ConfigDict,Field,field_validator,model_validator
from typing import Literal
import sqlglot
from sqlglot import exp
from semora_contracts import ClarificationQuestion
from semora_agent.intent_rules import REQUEST_INTERPRETATION_RULES

from .agent_context import compact_definition
from .datasources import datasource_service
from .knowledge import knowledge_service
from .task_memory import binding,digest


class ContextSelection(BaseModel):
    model_config=ConfigDict(extra='forbid')
    objects:list[str]=Field(default_factory=list,max_length=8,description='当前目录中完成需求必需的准确对象name。')
    knowledge_keywords:list[str]=Field(default_factory=list,max_length=2,description='需要补充的业务知识主题，每项一个短关键词。')


class EntitySelection(BaseModel):
    model_config=ConfigDict(extra='forbid')
    entity_field:str=Field(min_length=1)
    time_field:str=Field(min_length=1)
    limit:int=Field(ge=1,le=1000,strict=True)
    direction:Literal['asc','desc']='desc'
    mode:Literal['entities','records']='entities'


class InformationAssessment(BaseModel):
    model_config=ConfigDict(extra='forbid')
    status:Literal['sufficient','ambiguous','missing_core']
    reason:str=Field(min_length=1,max_length=1000)
    clarifications:list[ClarificationQuestion]=Field(default_factory=list,max_length=3)
    missing_objects:list[str]=Field(default_factory=list,max_length=4)
    purpose:Literal['output','filter','time','join','aggregation','sort']|None=None
    selection:EntitySelection|None=None

    @field_validator('clarifications','missing_objects',mode='before')
    @classmethod
    def normalize_unused_lists(cls,value):
        return [] if value is None else value

    @field_validator('purpose',mode='before')
    @classmethod
    def normalize_empty_purpose(cls,value):
        # This field applies only when requesting missing core context.
        if isinstance(value,str) and not value.strip():return None
        return value

    @model_validator(mode='after')
    def require_supplement_purpose(self):
        if self.status=='missing_core' and self.purpose is None:
            raise ValueError('核心资料补充必须说明purpose：output/filter/time/join/aggregation/sort')
        for question in self.clarifications:
            question.allow_free_text=True
        return self


def input_binding(run,ontology,scope):
    facts={f.question_id:{'field':f.field,'value':f.value} for f in run.user_answers}
    scans={}
    for source_id in {o.get('dataSourceId') or ontology.get('dataSourceId') for o in authorized_objects(run,ontology).values()}:
        try:
            source=datasource_service.repository.get(source_id)
            scans[source_id]={'at':source.last_scan_at,'status':source.status}
        except KeyError:scans[source_id]='not_registered'
    return digest({'context_protocol':6,'binding':binding(run,ontology,scope),'question':run.question,'answers':facts,
        'answer_context':run.agent_memory.get('confirmed_fact_context',[]),'scans':scans})


def confirmed_fact_context(workflow,run):
    """Keep the meaning of a selected option, including legacy explanations."""
    previous=(run.continuation or {}).get('confirmed_fact_context',[])
    saved=run.agent_memory.get('confirmed_fact_context',[])
    definitions=[];seen=set();current=run
    for _ in range(12):
        if current.id in seen:break
        seen.add(current.id)
        drafts=[current.requirement_draft.model_dump(mode='json') if current.requirement_draft else {},
                *[r.get('draft') or {} for r in reversed(current.alignment_rounds)],(current.continuation or {}).get('draft') or {}]
        definitions.extend(q for d in drafts for q in d.get('clarifications',[]))
        parent=getattr(workflow.store,'runs',{}).get((current.continuation or {}).get('source_run_id'))
        if parent is None or parent.domain!=run.domain:break
        current=parent
    rows=[]
    for fact in run.user_answers:
        row=fact.model_dump(mode='json')
        inherited=next((r for r in [*saved,*previous] if all(r.get(k)==row.get(k) for k in ('question_id','field','value')) and r.get('selected_option_description')),None)
        if inherited:
            row.update({k:inherited[k] for k in ('clarification_question','selected_option_description') if k in inherited})
        else:
            for q in definitions:
                if q.get('id')!=fact.question_id or q.get('field')!=fact.field:continue
                option=next((o for o in q.get('options',[]) if fact.value in (o.get('value'),o.get('id'))),None)
                if option:
                    row.update(clarification_question=q.get('question',''),selected_option_description=option.get('description',''))
                    break
        rows.append(row)
    run.agent_memory['confirmed_fact_context']=rows
    return rows


def authorized_objects(run,ontology):
    allowed=run.authorization.allowed_data_source_ids if run.authorization else None
    return {o['name']:o for o in ontology.get('objects',[]) if allowed is None or (o.get('dataSourceId') or ontology.get('dataSourceId')) in allowed}


def transfer_templates(objects,relations,ontology):
    """Publish exact key wiring, never infer a relationship from column names."""
    templates=[]
    for relation in relations:
        if relation['from'] not in objects or relation['to'] not in objects or relation['from']==relation['to']:continue
        try:tree=sqlglot.parse_one(relation['condition'],read='postgres')
        except (sqlglot.errors.ParseError,KeyError):continue
        def equalities(node):
            if isinstance(node,exp.Paren):return equalities(node.this)
            if isinstance(node,exp.And):return equalities(node.this)+equalities(node.expression)
            if isinstance(node,exp.EQ) and all(isinstance(c,exp.Column) for c in (node.this,node.expression)):return [(node.this,node.expression)]
            raise ValueError('not an equijoin')
        try:conditions=equalities(tree)
        except ValueError:continue
        for source,target in [(relation['from'],relation['to']),(relation['to'],relation['from'])]:
            pairs=[]
            for left,right in conditions:
                columns={left.table:left.name,right.table:right.name}
                def ref(name):
                    if name not in columns:return None
                    props=[p for p in objects[name].get('properties',[]) if columns.get(name) in (p['name'],p.get('column')) and not p.get('sensitive')]
                    return name+'.'+props[0]['name'] if len(props)==1 else None
                source_ref,target_ref=ref(source),ref(target)
                if not source_ref or not target_ref:break
                pairs.append({'source_field':source_ref,'target_field':target_ref})
            if len(pairs)!=len(conditions):continue
            source_id=objects[source].get('dataSourceId') or ontology.get('dataSourceId')
            target_id=objects[target].get('dataSourceId') or ontology.get('dataSourceId')
            templates.append({'relation':relation['name'],'source_object':source,'source_data_source_id':source_id,
                'target_object':target,'target_data_source_id':target_id,'key_pairs':pairs,
                'source_query_must_output':[p['source_field'] for p in pairs],
                'target_local_relationships':[r['name'] for r in relations if all((objects[n].get('dataSourceId') or ontology.get('dataSourceId'))==target_id for n in (r['from'],r['to']))]})
    return templates


def stage_join_contracts(name,ontology):
    """Exact per-edge aggregation keys, expressed in semantic property refs."""
    obj=next((o for o in ontology.get('objects',[]) if o['name']==name),None)
    if obj is None:return []
    refs={p.get('column',p['name']):name+'.'+p['name'] for p in obj.get('properties',[])}
    contracts=[]
    for relation in ontology.get('relations',[]):
        if name not in (relation['from'],relation['to']):continue
        try:tree=sqlglot.parse_one(relation['condition'],read='postgres')
        except (KeyError,sqlglot.errors.ParseError):continue
        if tree.find(exp.Or) or tree.find(exp.Not):continue
        columns={c.name for c in tree.find_all(exp.Column) if c.table==name}
        if columns and columns<=refs.keys():
            contracts.append({'relation':relation['name'],'group_by':sorted(refs[c] for c in columns)})
    return contracts


def source_diagnostics(run,ontology,steps):
    """Return independent structural defects together; never repair semantics."""
    objects=authorized_objects(run,ontology)
    sources={name:o.get('dataSourceId') or ontology.get('dataSourceId') for name,o in objects.items()}
    fields={name+'.'+p['name']:sources[name] for name,o in objects.items() for p in o.get('properties',[])}
    templates=transfer_templates(objects,[r for r in ontology.get('relations',[]) if {r['from'],r['to']}<=objects.keys()],ontology)
    issues=[];previous={}
    def issue(path,message):issues.append({'path':path,'message':message})
    for i,step in enumerate(steps):
        base=f'source_steps[{i}]({step.name})';query=step.query
        local={n for n,s in sources.items() if s==step.data_source_id}
        if not local:
            issue(base+'.data_source_id',f'{step.data_source_id!r}不是当前授权的完整数据源ID。逐字复制source_layout中的ID：{sorted(set(sources.values()))}；不能截断或缩写。')
            continue
        if query.data_source_id!=step.data_source_id:issue(base+'.query.data_source_id',f'必须与步骤ID完全一致：{step.data_source_id}')
        if query.grain is not None and query.grain not in local:issue(base+'.query.grain',f'当前源grain只能是：{sorted(local)}')
        if query.calculations and query.grain is None:issue(base+'.query.grain',f'存在计算指标，必须指定当前源本体对象：{sorted(local)}')
        stage_names={s.name for s in query.stages}
        for j,stage in enumerate(query.stages):
            for contract in stage_join_contracts(stage.source,ontology):
                if contract['relation'] not in query.relationship_path:continue
                if set(stage.group_by)!=set(contract['group_by']):
                    issue(base+f'.query.stages[{j}].group_by',f'阶段 {stage.name} 通过关系 {contract["relation"]} 关联时，分组键须为 {contract["group_by"]}；不能使用对端对象的列或额外明细列，否则无法证明关联唯一。')
        metrics={c.name for c in query.calculations}
        refs=[(f'fields[{j}]',r) for j,r in enumerate(query.fields)]+[(f'dimensions[{j}]',r) for j,r in enumerate(query.dimensions)]
        refs += [(f'filters[{j}].field',f.field) for j,f in enumerate(query.filters)]
        refs += [(f'calculations[{j}].field',c.field) for j,c in enumerate(query.calculations) if c.field]
        for path,ref in refs:
            if ref in fields and fields[ref]!=step.data_source_id:
                issue(base+'.query.'+path,f'{ref}已发布，但属于数据源{fields[ref]}，不属于当前步骤{step.data_source_id}。这是步骤归属错误，不是缺字段。')
            elif ref in metrics and path.startswith(('fields','dimensions')):
                issue(base+'.query.'+path,f'{ref}是计算指标名，应放query.metrics，不是fields/dimensions中的物理属性。')
            elif ref not in fields and ref.split('.')[0] not in stage_names:
                issue(base+'.query.'+path,f'{ref}不是当前已发布对象.属性，请从该源source_layout.field_refs逐字选择；阶段字段需完整阶段.字段引用。')
        local_relations={r['name'] for r in ontology.get('relations',[]) if {r['from'],r['to']}<=local}
        for relation in query.relationship_path:
            if relation not in local_relations:issue(base+'.query.relationship_path',f'{relation}不能用于当前源本地JOIN；可用：{sorted(local_relations)}。步骤间关系写inputs.relation。')
        for j,b in enumerate(step.inputs):
            prior=previous.get(b.source_step)
            if not prior:continue
            expected=[t for t in templates if t['relation']==b.relation and t['source_data_source_id']==prior.data_source_id and t['target_data_source_id']==step.data_source_id]
            actual={(p.source_field,p.target_field) for p in b.pairs}
            if expected and not any(actual=={(p['source_field'],p['target_field']) for p in t['key_pairs']} for t in expected):
                issue(base+f'.inputs[{j}].key_pairs',f'必须使用已发布关系的准确键：{[t["key_pairs"] for t in expected]}；不能用实体键冒充明细键，必要时增加同源传键步骤。')
            outputs=set(prior.query.fields+prior.query.dimensions+prior.query.metrics)
            for p in b.pairs:
                if p.source_field not in outputs:issue(base+f'.inputs[{j}].source_field',f'{p.source_field}必须由{b.source_step}实际输出；当前输出{sorted(outputs)}。')
            if b.include_source_fields:
                target_outputs=set(query.fields+query.dimensions+query.metrics)
                missing=[p.target_field for p in b.pairs if p.target_field not in target_outputs]
                if missing:issue(base+f'.inputs[{j}].include_source_fields',f'补充来源列需要当前输出保留关联键{missing}。若当前按另一粒度汇总，不应硬加分组键改变口径；若可直接使用本源实体维度，应移除非必需的include_source_fields。')
        previous[step.name]=step
    return issues[:12]


def selection_diagnostics(run,ontology,steps):
    """Check entity membership ordering separately from final display ordering."""
    saved=(run.agent_memory.get('prepared_context') or [{}])[-1]
    selection=(saved.get('assessment') or {}).get('selection')
    if not selection or not steps:return []
    objects=authorized_objects(run,ontology)
    entity=selection['entity_field'];time_field=selection['time_field']
    equivalents={entity}
    templates=transfer_templates(objects,[r for r in ontology.get('relations',[]) if {r['from'],r['to']}<=objects.keys()],ontology)
    for _ in range(len(objects)):
        for t in templates:
            if len(t['key_pairs'])!=1:continue
            for p in t['key_pairs']:
                if p['source_field'] in equivalents:equivalents.add(p['target_field'])
    chosen=next(((i,s) for i,s in enumerate(steps) if s.query.limit is not None),None)
    if chosen is None:return [{'path':'selection.limit','message':f'用户要求按事件时间选{selection["limit"]}个不同实体，尚未落实选取步骤。'}]
    i,step=chosen;q=step.query;errors=[]
    def fail(message):errors.append({'path':f'source_steps[{i}]({step.name}).query.selection','message':message})
    if q.limit!=selection['limit']:fail(f'选取数量必须为{selection["limit"]}，不能用最终展示排序替换实体选取。')
    obj=objects.get(entity.split('.')[0],{})
    prop=next((p for p in obj.get('properties',[]) if entity==obj.get('name','')+'.'+p['name']),{})
    raw_unique=(bool(obj.get('primaryKey')) and time_field.split('.')[0]==obj.get('name') and prop.get('column')==obj.get('primaryKey') and not q.relationship_path and not q.calculations)
    if selection.get('mode')=='records':
        if not raw_unique:fail('按记录取前N必须使用事件对象已发布的主键与时间字段，并在单对象明细查询中选取；允许同一业务实体重复，不要求按业务实体聚合。')
        if entity not in q.fields+q.dimensions:fail('按记录选取必须输出已发布的记录主键以传递所选记录，不能改为实体去重。')
        expected=time_field
    elif raw_unique:
        expected=time_field
    else:
        entity_outputs=set(q.fields+q.dimensions)
        if not entity_outputs or not entity_outputs<=equivalents or not set(q.dimensions)&equivalents:
            fail(f'必须按实体键{entity}（或已发布等值关系的同义键）分组后取前N；不能选出N条明细再去重。当前fields/dimensions混入明细列时会选错实体，请将明细查询移到后续步骤。')
        function='max' if selection['direction']=='desc' else 'min'
        def is_time(reference):
            if reference==time_field:return True
            for stage in q.stages:
                if not set(stage.group_by)&equivalents:continue
                for measure in stage.measures:
                    if reference==stage.name+'.'+measure.name and measure.function==function and measure.field==time_field:return True
            return False
        calculation=next((c for c in q.calculations if c.function==function and is_time(c.field) and (not q.metrics or c.name in q.metrics)),None)
        expected=calculation.name if calculation else None
        if not expected:fail(f'按{entity}分组，用{function}({time_field})得到每个实体的事件时间，作为选取排序指标；最终展示指标不得改变选取成员。')
    if expected and (not q.order_by or q.order_by[0].field!=expected or q.order_by[0].direction!=selection['direction']):
        fail(f'选取阶段必须首先按{expected} {selection["direction"]}排序，选取后才按展示指标重新排序。')
    return errors


def selection_plan_shape(workflow,run,ontology,scope,state):
    """Provide legal query wiring; the Analyst must still fill business filters."""
    selection=(state.get('assessment') or {}).get('selection')
    if not selection:return None
    objects=authorized_objects(run,ontology)
    entity,time_field=selection['entity_field'],selection['time_field']
    entity_object,time_object=entity.split('.')[0],time_field.split('.')[0]
    source_id=objects[entity_object].get('dataSourceId') or ontology.get('dataSourceId')
    if (objects[time_object].get('dataSourceId') or ontology.get('dataSourceId'))!=source_id:return None
    local_names={n for n,o in objects.items() if (o.get('dataSourceId') or ontology.get('dataSourceId'))==source_id}
    relations=[r for r in ontology.get('relations',[]) if {r['from'],r['to']}<=local_names]
    try:path=workflow.analyst._connect_objects({entity_object,time_object},relations)
    except ValueError:return None
    if selection.get('mode')=='records':
        return {'business_filters_must_be_filled':True,'rule':'用户选择按记录取前N，保留重复业务实体。以记录主键和事件时间取明细，不按业务实体分组；仍须补齐确认的业务filters。',
            'query_shape':{'domain':run.domain,'data_source_id':source_id,'intent':'按事件时间选取明细记录','grain':entity_object,
                'fields':list(dict.fromkeys([entity,time_field])),'dimensions':[],'metrics':[],'calculations':[],
                'relationship_path':[],'data_scope':scope,'order_by':[{'field':time_field,'direction':selection['direction']},{'field':entity,'direction':'asc'}],'limit':selection['limit']}}
    return {'business_filters_must_be_filled':True,
        'rule':'此为程序根据实体选取约束生成的合法结构，不含业务过滤条件；必须补齐已确认的日期、状态、数据范围等filters后校验。不能把示例无过滤当作不限范围。允许按等价语义调整，但不能将N个实体改为N条明细。',
        'query_shape':{'domain':run.domain,'data_source_id':source_id,'intent':'按事件时间选取不同实体','grain':entity_object,
            'fields':[entity],'dimensions':[entity],'metrics':['selected_event_time'],
            'calculations':[{'name':'selected_event_time','function':'max' if selection['direction']=='desc' else 'min','field':time_field}],
            'relationship_path':path,'data_scope':scope,'order_by':[{'field':'selected_event_time','direction':selection['direction']},{'field':entity,'direction':'asc'}],
            'limit':selection['limit']}}


def business_contract_diagnostics(run,ontology,steps):
    issues=selection_diagnostics(run,ontology,steps)
    # An exact retry cannot supersede the meaning of a confirmed option.
    # Restrict this legacy recovery rule to explicit unchanged retries; new
    # requests may legitimately narrow an earlier all-history requirement.
    if run.question.strip().rstrip('。！!') not in {'重试','继续','再试一次','重新执行','按原口径继续'}:return issues
    published={o['name']+'.'+p['name'] for o in authorized_objects(run,ontology).values() for p in o.get('properties',[])}
    unrestricted=set()
    for fact in run.agent_memory.get('confirmed_fact_context',[]):
        text=fact.get('selected_option_description') or ''
        if (fact.get('field') in published and re.search(r'(全部|所有).*?(记录|历史)',text)
            and re.search(r'不限时间|不限制时间',text) and not re.search(r'不是|并非|不再|仅|只',text)):
            unrestricted.add(fact['field'])
    if not unrestricted:return issues
    by_name={s.name:i for i,s in enumerate(steps)}
    def ancestors(index):
        reached=set();pending=[index]
        while pending:
            current=pending.pop()
            if current in reached:continue
            reached.add(current)
            pending.extend(by_name[b.source_step] for b in getattr(steps[current],'inputs',[]) if b.source_step in by_name)
        return reached
    selection=(run.agent_memory.get('prepared_context') or [{}])[-1].get('assessment',{}).get('selection')
    chosen=next((i for i,s in enumerate(steps) if s.query.limit is not None),None) if selection else None
    protected=ancestors(chosen) if chosen is not None else set()
    lookup=set()
    for i,step in enumerate(steps):
        refs=set(step.query.fields+step.query.dimensions)
        refs.update(c.field for c in step.query.calculations if c.field)
        refs.update(m.field for stage in step.query.stages for m in stage.measures if m.field)
        if refs&unrestricted:lookup.update(ancestors(i))
    for i in sorted(lookup-protected):
        step=steps[i];base=f'source_steps[{i}]({step.name}).query'
        def required(path,value):
            issues.append({'path':path,'message':'已确认选项明确要求全部历史记录、不限时间；实体选取条件只保留在前置选取阶段，后续查找不能再增加记录范围筛选。权限data_scope、关联键传递和门禁仍照常执行。','replacement_json':value})
        if step.query.filters:required(base+'.filters','[]')
        if step.query.time_range:required(base+'.time_range','null')
        for j,stage in enumerate(step.query.stages):
            if stage.filters:required(base+f'.stages[{j}].filters','[]')
            for k,measure in enumerate(stage.measures):
                if measure.filters:required(base+f'.stages[{j}].measures[{k}].filters','[]')
    return issues


def context_packet(workflow,run,ontology,names):
    from .query_stages import stage_group_keys
    available=authorized_objects(run,ontology)
    selected=set(names)
    if not selected <= available.keys():raise ValueError('上下文请求包含未发布或未授权对象')
    relations=[r for r in ontology.get('relations',[]) if r['from'] in available and r['to'] in available]
    gaps=[]
    if len(selected)>1:
        try:
            paths=workflow.analyst._connect_objects(selected,relations)
            for r in relations:
                if r['name'] in paths:selected.update([r['from'],r['to']])
        except ValueError:gaps.append('部分对象未配置可连通关系；不得猜测关联条件')
    if len(selected)>12:raise ValueError('所需关联上下文超过12个对象，请缩小本次查询范围')
    objects=[];schemas=[]
    for name in sorted(selected):
        obj=available[name]
        objects.append({'object':compact_definition(obj),
            'field_refs':[name+'.'+p['name'] for p in obj.get('properties',[])],
            'stage_group_keys':stage_group_keys(name,ontology),'stage_join_contracts':stage_join_contracts(name,ontology)})
        source_id=obj.get('dataSourceId') or ontology.get('dataSourceId')
        try:
            source=datasource_service.repository.get(source_id)
            table=obj.get('table','').split('.')[-1]
            matches=[t for t in source.scan_result.get('tables',[]) if str(t.get('name') or t.get('table_name') or '').split('.')[-1]==table]
            # Only scanned structural metadata. Never include secret refs or rows.
            tables=[{k:deepcopy(t[k]) for k in ('name','table_name','schema','columns','primary_key','foreign_keys') if k in t} for t in matches]
            schemas.append({'object':name,'tables':tables,'scanned':bool(tables)})
        except KeyError:schemas.append({'object':name,'tables':[],'scanned':False})
    metrics=[]
    for metric in ontology.get('metrics',[]):
        refs=set(re.findall(r'\b([A-Za-z_][A-Za-z0-9_]*)\.',str(metric.get('expression',''))))
        if metric.get('grain') in selected or refs and refs<=selected:metrics.append(compact_definition(metric))
    source_layout=[]
    for source_id in sorted({available[n].get('dataSourceId') or ontology.get('dataSourceId') or '' for n in selected}):
        local_names={n for n in selected if (available[n].get('dataSourceId') or ontology.get('dataSourceId') or '')==source_id}
        source_layout.append({'data_source_id':source_id,'grain_options':sorted(local_names),
            'field_refs':[ref for item in objects if item['object']['name'] in local_names for ref in item['field_refs']],
            'local_relationship_path_options':[r['name'] for r in relations if r['from'] in local_names and r['to'] in local_names],
            'boundary_relations':[compact_definition(r) for r in relations if (r['from'] in local_names)!=(r['to'] in local_names) and {r['from'],r['to']}<=selected]})
    return {'selected_objects':sorted(selected),'objects':objects,'schemas':schemas,'source_layout':source_layout,
        'transfer_templates':transfer_templates({n:available[n] for n in selected},[r for r in relations if {r['from'],r['to']}<=selected],ontology),
        'relations':[compact_definition(r) for r in relations if r['from'] in selected and r['to'] in selected],
        'metrics':metrics,'gaps':gaps,
        'coverage_check':['目标与输出','必要筛选及时间','粒度与去重','关联与预聚合','排序与数量'],
        'rule':'逐项判断已有信息是否足够。足够即规划；业务含义不确定即澄清；只有缺少完成需求必需的核心对象时才能申请一次补充。不要扩展可选目标。'}


async def prepare_context(workflow,run,question,ontology,scope):
    key=input_binding(run,ontology,scope)
    saved=(run.agent_memory.get('prepared_context') or [{}])[-1]
    if saved.get('binding')==key:
        workflow.emit(run,'context.reused',run.status,{'summary':'复用当前需求版本的准备资料，不重新探索','objects':saved['packet']['selected_objects']},'context_service')
        return saved
    available=authorized_objects(run,ontology)
    kb_ids=ontology.get('knowledgeBaseIds') or []
    selection=ContextSelection(objects=list(available)) if len(available)<=8 else None
    if selection is None or kb_ids:
        raw=await workflow.analyst.model.complete_json(
            CONTEXT_SELECTION,
            json.dumps({'request':question,'confirmed_answers':run.agent_memory.get('confirmed_fact_context') or [f.model_dump(mode='json') for f in run.user_answers],
                'object_catalog':[{k:o.get(k) for k in ('name','label','description')} for o in available.values()],
                'metrics':[{k:m.get(k) for k in ('name','label','grain')} for m in ontology.get('metrics',[])],
                'knowledge_available':bool(kb_ids),'output_contract':ContextSelection.model_json_schema()},ensure_ascii=False))
        selection=ContextSelection.model_validate(raw)
    if not set(selection.objects)<=available.keys():raise ValueError('上下文选择包含未发布或未授权对象，已停止准备')
    for term in selection.knowledge_keywords:
        if not term or len(term)>64 or re.search(r'[\s,，、;；|]',term):raise ValueError('知识检索必须使用单个短关键词')
    packet=context_packet(workflow,run,ontology,selection.objects)
    packet['knowledge']=[]
    async def collect_knowledge():
        searches=0;pages=set();sources=set()
        for term in selection.knowledge_keywords:
            for kb in kb_ids:
                if searches>=4:return
                searches+=1
                rows=await knowledge_service._wiki_search(kb,re.escape(term))
                for row in rows[:2]:
                    slug=row.get('slug')
                    if not slug or (kb,slug) in pages or len(pages)>=3:continue
                    page=await knowledge_service._wiki_page(kb,slug);pages.add((kb,slug))
                    evidence=knowledge_service._page_evidence(kb,page)
                    if not any(e.knowledge_base_id==kb and e.locator==evidence.locator for e in run.evidence):run.evidence.append(evidence)
                    item={'knowledge_base_id':kb,'slug':slug,'title':page.get('title'),
                        'content':str(page.get('content') or page.get('summary') or '')[:8000],
                        'links':page.get('out_links') or [],'source_refs':page.get('source_refs') or []}
                    packet['knowledge'].append(item)
                    for source in page.get('source_refs') or []:
                        if not isinstance(source,str) or (kb,source) in sources or len(sources)>=2:continue
                        from urllib.parse import quote
                        chunks,_=await knowledge_service._request('GET','/chunks/'+quote(source,safe=''),params={'page':1,'page_size':5})
                        sources.add((kb,source))
                        item.setdefault('sources',[]).append({'document_id':source,'chunks':[{'id':c.get('id'),'content':str(c.get('content') or '')[:2000]} for c in chunks] if isinstance(chunks,list) else []})
    if kb_ids and selection.knowledge_keywords:
        try:
            async with asyncio.timeout(20):await collect_knowledge()
        except Exception:
            packet['knowledge_warning']='辅助知识准备未全部完成，保留已取得证据；不阻断基于本体与用户口径的规划'
    saved={'binding':key,'packet':packet,'supplements':0}
    run.agent_memory['prepared_context']=[saved]
    workflow.emit(run,'context.prepared',run.status,{'summary':'已准备相关对象、关系、扫描结构及辅助证据；进入规划，不开放自由探索',
        'objects':packet['selected_objects'],'relations':len(packet['relations']),'knowledge_pages':len(packet['knowledge'])},'context_service')
    return saved


def supplement_context(workflow,run,ontology,state,names,purpose,reason,scope):
    if state.get('binding')!=input_binding(run,ontology,scope):raise ValueError('准备资料的权限或版本已变化，需要重新准备')
    if not reason.strip():raise ValueError('必须说明缺少哪个核心信息及其对结果的影响')
    available=authorized_objects(run,ontology)
    if not names or len(names)>4 or not set(names)<=available.keys():raise ValueError('只允许申请当前授权目录中最多4个核心对象')
    if purpose=='sql_error' and not any(e.event=='compilation.revision.required' for e in run.events):
        raise ValueError('当前尚未形成SQL编译错误证据，不能声称存在SQL错误而扩展检索')
    existing=set(state['packet']['selected_objects'])
    if set(names)<=existing:
        return {'available':True,'already_prepared':True,'prepared_context':state['packet'],
            'message':'所需资料已在prepared_context中；已直接返回原文，不重新检索。业务口径不明确请澄清，不要重复读取。'}
    if state['supplements']>=1:
        return {'available':False,'message':'本次核心上下文补充预算已用完；请交付具体业务澄清或受阻原因，不继续扩展。'}
    state['supplements']+=1
    packet=context_packet(workflow,run,ontology,existing|set(names))
    packet['knowledge']=state['packet'].get('knowledge',[])
    state['packet']=packet
    workflow.emit(run,'context.supplemented',run.status,{'summary':'为完成当前需求补齐核心上下文','objects':names,'purpose':purpose,'reason':reason[:300]},'context_service')
    return {'available':True,'prepared_context':packet,'message':'核心资料已补齐。现在校验方案或提交具体澄清，不扩展可选目标。'}


def review_context(workflow,run,ontology,plan,scope):
    available=authorized_objects(run,ontology)
    names=set()
    def collect(value):
        if isinstance(value,str):
            if value in available:names.add(value)
            prefix=value.split('.',1)[0]
            if '.' in value and prefix in available:names.add(prefix)
        elif isinstance(value,dict):
            for v in value.values():collect(v)
        elif isinstance(value,list):
            for v in value:collect(v)
    collect(plan.model_dump(mode='json'))
    for r in ontology.get('relations',[]):
        if r['name'] in plan.relationship_path:names.update([r['from'],r['to']])
    packet=context_packet(workflow,run,ontology,names)
    packet['scan_status']=[{'object':row['object'],'scanned':row['scanned']} for row in packet.pop('schemas')]
    packet['physical_validation']='物理映射由编译器和门禁验证；复核保留完整相关本体定义、字段类型、关系和扫描状态，不重复发送物理表的全部扫描列。'
    return {'binding':input_binding(run,ontology,scope),'packet':packet,'supplements':0}


async def assess_information(workflow,run,question,ontology,scope,state):
    """Business sufficiency only, before SQL planning; at most one replenishment."""
    if state.get('assessment'):return InformationAssessment.model_validate(state['assessment'])
    for attempt in range(2):
        packet=state['packet']
        def event(name,data):
            workflow.emit(run,'context.model.'+name.removeprefix('agent.request.'),run.status,{**data,'operation':'information_assessment'},'context_service')
        raw=await workflow.analyst.model.complete_json(
            INFORMATION_ASSESSMENT,
            json.dumps({'request':question,'latest_user_request':run.question,
                'answer_precedence':'最新明确需求覆盖冲突的旧卡片选项，保留不冲突的口径；不能要求两个阶段使用相同排序依据。',
                'confirmed_answers':run.agent_memory.get('confirmed_fact_context') or [f.model_dump(mode='json') for f in run.user_answers],
                'objects':packet['objects'],'relations':packet['relations'],'metrics':packet['metrics'],
                'knowledge':packet.get('knowledge',[]),'gaps':packet['gaps'],
                'remaining_core_supplements':1-state['supplements'],'output_contract':InformationAssessment.model_json_schema()},ensure_ascii=False),on_request_event=event)
        assessment=InformationAssessment.model_validate(raw)
        if assessment.selection:
            refs={r for o in packet['objects'] for r in o['field_refs']}
            if not {assessment.selection.entity_field,assessment.selection.time_field}<=refs:
                raise ValueError('实体选取约束必须使用已准备的完整本体字段引用')
        if assessment.status=='ambiguous' and not assessment.clarifications:raise ValueError('业务歧义必须交付具体澄清问题')
        if assessment.status=='sufficient' and (assessment.clarifications or assessment.missing_objects):raise ValueError('信息充足判断不能同时声明缺口')
        workflow.emit(run,'context.assessed',run.status,{'status':assessment.status,'summary':assessment.reason,
            'questions':len(assessment.clarifications),'missing_objects':assessment.missing_objects},'context_service')
        if assessment.status!='missing_core':
            state['assessment']=assessment.model_dump(mode='json')
            workflow.store.save_run(run)
            return assessment
        if not assessment.missing_objects:raise ValueError('核心信息缺失必须定位到具体对象')
        if attempt==1:raise ValueError('一次核心补充后仍缺少必要信息：'+assessment.reason)
        result=supplement_context(workflow,run,ontology,state,assessment.missing_objects,assessment.purpose,assessment.reason,scope)
        if not result.get('available') or result.get('already_prepared'):raise ValueError('核心资料已提供或补充预算不足；应明确口径缺口而非重复取资料')
    raise AssertionError('unreachable')
