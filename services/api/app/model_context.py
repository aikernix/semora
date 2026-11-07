"""Model-facing evidence content; original provenance stays in run state."""
from copy import deepcopy
import hashlib
import json


def project_knowledge(value, run):
    references=run.agent_memory.setdefault('model_evidence_refs',[{}])[-1]

    def register(source,content):
        source={**source,'content_hash':hashlib.sha256(str(content).encode()).hexdigest()}
        encoded=json.dumps(source,sort_keys=True,ensure_ascii=False,default=str)
        ref='K'+hashlib.sha256(encoded.encode()).hexdigest()[:12]
        references.setdefault(ref,deepcopy(source))
        return ref

    def evidence(item):
        if not isinstance(item,dict):return item
        if item.get('ref') and 'content' in item:return deepcopy(item)
        if 'reference' in item:
            result=deepcopy(item);result['reference']=evidence(item['reference']);return result
        if not any(k in item for k in ('document_id','knowledge_base_id','locator')):
            return visit(item)
        source={k:deepcopy(item[k]) for k in ('knowledge_base_id','document_id','chunk_id','locator','revision','title') if k in item}
        content=item.get('summary',item.get('content',''))
        result={'ref':register(source,content),'content':content,'status':item.get('definition_status',item.get('status','reference'))}
        for k in ('title','scope','conflicts','pending_confirmation'):
            if k in item:result[k]=deepcopy(item[k])
        return result

    def knowledge(item):
        if not isinstance(item,dict):return [item]
        if item.get('ref') and 'content' in item:return [deepcopy(item)]
        source={k:deepcopy(item[k]) for k in ('knowledge_base_id','slug','title','revision','source_refs','links') if k in item}
        content=item.get('content','')
        result={'ref':register(source,content),'content':content,'status':item.get('definition_status',item.get('status','reference'))}
        for k in ('title','scope','conflicts','pending_confirmation'):
            if k in item:result[k]=deepcopy(item[k])
        rows=[result]
        for original in item.get('sources') or []:
            for chunk in original.get('chunks') or []:
                rows.append({'ref':register({'knowledge_base_id':item.get('knowledge_base_id'),
                    'document_id':original.get('document_id'),'chunk_id':chunk.get('id')},chunk.get('content','')),
                    'content':chunk.get('content',''),'status':'reference'})
        return rows

    def visit(node):
        if isinstance(node,list):return [visit(v) for v in node]
        if not isinstance(node,dict):return node
        result={}
        for key,child in node.items():
            if key=='evidence' and isinstance(child,list):result[key]=[evidence(v) for v in child]
            elif key=='knowledge' and isinstance(child,list):result[key]=[r for v in child for r in knowledge(v)]
            else:result[key]=visit(child)
        return result
    return visit(value)


def execution_contract(plan):
    """Explain only operations present in this immutable plan, not a tutorial."""
    from .intent_planner import source_output_contract
    contract=source_output_contract(plan)
    contract.pop('rule',None)
    if len({s.data_source_id for s in plan.source_steps})>1:
        contract['execution_model']={'kind':'independent_data_source_connections',
            'sql_scope':'每条SQL仅访问其data_source_id内的对象；跨数据源字段由SQL之后的执行器操作产生。'}
    for index,step in enumerate(contract['steps']):
        spec=plan.source_steps[index]
        step['data_source_id']=spec.data_source_id
        complete=index<len(contract['steps'])-1 or bool(spec.regroup or spec.membership)
        step['sql_limit_role']='business_top_n' if spec.query.limit is not None else ('overflow_detection' if complete else 'result_preview')
        if complete:step['on_incomplete_input']='stop_before_key_transfer_or_merge'
        operations=[]
        if step['executor_enrichment']:
            operations.append({'kind':'enrich_source_fields',
                'available_after':'source_sql',
                'meaning':'SQL返回后按完整关联键核验来源唯一性并补齐来源列；这些列不是该SQL数据源的可查询列。'})
        if step['membership']:
            operations.append({'kind':'subject_membership',
                'meaning':'按主体标识判断范围内是否存在任一匹配记录，返回来源步骤中的符合主体。',
                'requires_complete_sets':True})
        elif step['regroup']:
            step['sql_result_grain']='partial_aggregate_by_transfer_key'
            operations.append({'kind':'regroup_partials',
                'execution_location':'executor_after_enrichment',
                'meaning':'完整未舍入局部结果补齐来源列后，按业务维度合并；业务排序和Top N在合并后应用。SQL行数保护用于检测溢出，溢出即停止。',
                'requires_complete_sets':True})
        if operations:step['result_operations']=operations
    return contract


def intent_field_meanings(intent):
    """Reviewers see values rather than the Analyst's input tool schema."""
    from semora_contracts import QueryIntent, IntentSelection, IntentExistence,IntentMeasure
    meanings={}
    if any(m.filters or m.null_value is not None for m in intent.measures):
        meanings['measures']={k:IntentMeasure.model_fields[k].description for k in ('filters','null_value')}
    for key in ('having','optional_relationships'):
        if getattr(intent,key):meanings[key]=QueryIntent.model_fields[key].description
    for key,model in (('selection',IntentSelection),('existence',IntentExistence)):
        if getattr(intent,key,None):
            meanings[key]={k:f.description for k,f in model.model_fields.items() if f.description}
    if intent.selection:
        meanings['record_scope']=QueryIntent.model_fields['record_scope'].description
        meanings['order_by']=QueryIntent.model_fields['order_by'].description
    return meanings


def output_definitions(plan, ontology):
    """Business column definitions for explanations, without source navigation."""
    intent=plan.query_intent
    refs=set((intent.fields+intent.group_by) if intent else plan.fields+plan.dimensions)
    columns=[{'field':obj['name']+'.'+prop['name'],**{k:prop[k] for k in ('label','type','description','unit') if k in prop}}
        for obj in ontology.get('objects',[]) for prop in obj.get('properties',[])
        if obj['name']+'.'+prop['name'] in refs]
    calculations=intent.measures if intent else plan.calculations
    calculation_refs={m.field for m in calculations if m.field}
    columns += [{'field':obj['name']+'.'+prop['name'],**{k:prop[k] for k in ('label','type','description','unit') if k in prop}}
        for obj in ontology.get('objects',[]) for prop in obj.get('properties',[])
        if obj['name']+'.'+prop['name'] in calculation_refs-refs]
    return {'fields':columns,'calculations':[m.model_dump(mode='json') for m in calculations],
        'metrics':[m for m in ontology.get('metrics',[]) if m['name'] in plan.metrics]}


def business_requirement(run):
    requirement=run.requirement
    if requirement is None:return None
    result={'summary':requirement.summary,'confirmed_facts':[f.model_dump(mode='json') for f in requirement.confirmed_facts]}
    if requirement.query_intent:result['query_intent']=requirement.query_intent.model_dump(mode='json')
    else:
        result.update({k:getattr(requirement,k) for k in ('subject','metrics','dimensions','filters','time_semantics','assumptions')})
        result={k:([v.model_dump(mode='json') if hasattr(v,'model_dump') else v for v in values] if isinstance(values,list) else values) for k,values in result.items()}
    result['evidence']=[e.model_dump(mode='json') for e in requirement.evidence]
    return project_knowledge(result,run)
