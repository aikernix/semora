"""Bounded business-intent correction; never asks a model to edit physical plans."""
from copy import deepcopy
import re


def reference_paths(value,reference,prefix=''):
    result=[]
    if isinstance(value,dict):
        for key,child in value.items():result+=reference_paths(child,reference,prefix+'/'+key)
    elif isinstance(value,list):
        for index,child in enumerate(value):result+=reference_paths(child,reference,prefix+'/'+str(index))
    elif value==reference and re.search(r'/(?:fields|group_by)/\d+$|/(?:field|time_field)$',prefix):result.append(prefix)
    return result


def preserve_unrelated(base,current,paths):
    def masked(value):
        value=deepcopy(value)
        for path in paths+['/summary','/analysis_steps']:
            parts=path.strip('/').split('/');parent=value
            try:
                for part in parts[:-1]:parent=parent[int(part)] if isinstance(parent,list) else parent[part]
                if isinstance(parent,list):parent[int(parts[-1])]='__correction__'
                else:parent[parts[-1]]='__correction__'
            except (KeyError,IndexError,TypeError,ValueError):return None
        return value
    left,right=masked(base),masked(current)
    return left is not None and right is not None and left==right


def failure(toolkit,intent,*,kind,path,message,options=None,reference=None,errors=None):
    value=intent.model_dump(mode='json')
    record={'kind':kind,'path':path,'message':message,'errors':errors or []}
    toolkit.run.agent_memory.setdefault('planning_diagnostics',[]).append(record)
    del toolkit.run.agent_memory['planning_diagnostics'][:-4]
    base=getattr(toolkit,'intent_repair_base',None)
    if base is not None and not preserve_unrelated(base,value,toolkit.intent_repair_paths):
        toolkit.run.agent_memory['last_intent_candidate']=[deepcopy(base)]
    else:toolkit.run.agent_memory['last_intent_candidate']=[value]
    allowed=reference_paths(value,reference) if reference else ['/'+path.replace('.','/')]
    repairable=kind not in {'ambiguity','capability','compiler_validation'} and path.split('.')[0] in value and bool(allowed)
    if repairable and not toolkit.intent_repair_only:
        toolkit.intent_repair_only=True
        toolkit.intent_repair_base=value
        toolkit.intent_repair_paths=allowed
        toolkit.protocol_stage='planning'
        return {'valid':False,'kind':'intent_correction','message':message,'allowed_paths':allowed,
                'correction_contract':'仅补正指定业务意图字段，保留其余筛选、指标和用户口径；不再检索，不编写SQL或执行步骤。无法确定时提交澄清。'}
    toolkit.protocol_stage='finalizing'
    return {'valid':False,'kind':kind,'path':path,'message':message,
            'business_options':options or [],'errors':errors or []}
