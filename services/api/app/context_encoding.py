"""Lossless model-only encoding. Authoritative ontology and facts stay unchanged."""
from copy import deepcopy


def encode_context(context):
    result=deepcopy(context)
    packet=result.get('prepared_context') or (result if isinstance(result.get('objects'),list) else None)
    if not packet:return result
    for entry in packet.get('objects',[]):
        obj=entry.get('object') or {}
        props=obj.get('properties') or []
        if not isinstance(props,list) or len(props)<=32:continue
        common=[k for k in props[0] if all(k in p for p in props)]
        extras=any(set(p)-set(common) for p in props)
        rows=[[p[k] for k in common]+([{k:v for k,v in p.items() if k not in common}] if extras else []) for p in props]
        obj['properties_table']={'columns':common,'rows':rows,'extra_column':extras}
        obj.pop('properties')
        refs=[obj['name']+'.'+p['name'] for p in props]
        if entry.get('field_refs')==refs:
            entry.pop('field_refs')
            entry['field_reference_format']=obj['name']+'.{name}'
        # Row values, NULL, absent properties, enums and units are all retained.
    return result


def decode_context(context):
    """Round-trip validator; models read the column names and rows directly."""
    result=deepcopy(context)
    packet=result.get('prepared_context') or (result if isinstance(result.get('objects'),list) else {})
    for entry in packet.get('objects',[]):
        obj=entry.get('object') or {}
        table=obj.pop('properties_table',None)
        if not table:continue
        keys=table['columns'];props=[]
        for row in table['rows']:
            prop=dict(zip(keys,row[:len(keys)]))
            if table['extra_column']:prop.update(row[-1])
            props.append(prop)
        obj['properties']=props
        if entry.pop('field_reference_format',None):
            entry['field_refs']=[obj['name']+'.'+p['name'] for p in props]
    return result
