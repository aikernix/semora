"""Import the approved six-object 51talk domain; never read business rows or actions.

Credentials are configured separately in Executor. This script only uses API
secret references, restricted metadata scans and deterministic ontology mappings.
"""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import re
import httpx

STUDENT_ID = '01a17d72-3a39-7f36-8428-32c35eb9427e'
SOURCES = {
    'CRM': ('crmnew', ['user_call_detail']),
    'APPOINT': ('talkplatform_appoint_reconstruction', ['appoint_hot']),
    'LOG': ('crm_log', ['remark_log']),
    'CONTENT': ('talkplatform_content', ['appoint_audio', 'appoint_audio_text']),
}
MAPPING = {
    STUDENT_ID: ('student', 'APPOINT', 'appoint_hot'),
    'trial_class_record': ('trial_class_record', 'APPOINT', 'appoint_hot'),
    'outbound_call_record': ('outbound_call_record', 'CRM', 'user_call_detail'),
    'sales_remark_record': ('sales_remark_record', 'LOG', 'remark_log'),
    'class_audio': ('class_audio', 'CONTENT', 'appoint_audio'),
    'class_audio_text': ('class_audio_text', 'CONTENT', 'appoint_audio_text'),
}
RELATIONS = {
    'booking_trial_class': '1:N', 'call_to_student': 'N:1',
    'class_audio_has_text': '1:N', 'sales_remark_belongs_to_student': 'N:1',
    'sales_remark_has_class_audio': 'N:N', 'trial_class_has_audio': '1:N',
}


def family(dtype):
    dtype = dtype.lower().split('(')[0].strip()
    if dtype in {'integer','int','tinyint','smallint','mediumint','bigint'}: return 'integer'
    if dtype in {'string','varchar','text','char','enum','longtext','mediumtext'}: return 'text'
    if dtype in {'decimal','numeric','float','double','number'}: return 'number'
    if dtype in {'datetime','timestamp'}: return 'timestamp'
    return dtype


def semantic_type(dtype):
    base = dtype.lower().split('(')[0].strip()
    if base == 'bigint': return 'bigint'
    return {'integer':'integer','text':'string','number':'decimal','timestamp':'datetime'}.get(family(dtype),base)


def convert(source, sources, scans, source_hash):
    issues, objects = [], []
    for original in source['object_types']:
        if original['id'] not in MAPPING: continue
        name, key, table_name = MAPPING[original['id']]
        database = SOURCES[key][0]
        table = next((t for t in scans[key].get('tables',[]) if t['schema']==database and t['name']==table_name), {})
        columns = {c['name']:c for c in table.get('columns',[])}
        props = []
        for field in original['data_properties']:
            column = (field.get('mapped_field') or {}).get('name') or field['name']
            actual = columns.get(column)
            if not actual:
                issues.append(f'{name}.{column}: physical column missing')
            elif family(actual['type']) != family(field['type']):
                issues.append(f'{name}.{column}: incompatible types {field["type"]}/{actual["type"]}')
            # User-defined classification for this domain; do not infer sensitivity.
            sensitive = False
            props.append({'name':field['name'],'label':field.get('display_name') or field['name'],
                'column':column,'type':semantic_type(actual['type']) if actual else field['type'],
                'nullable': bool((actual or {}).get('nullable',True)), 'sensitive':sensitive,
                'description':field.get('comment',''),
                'metadata':{'sourceDefinition':field,'physicalType':(actual or {}).get('type'),
                    'physicalComment':(actual or {}).get('comment',''), 'sensitivity':'confidential' if sensitive else 'internal'}})
        pk = original.get('primary_keys') or []
        if len(pk) != 1: issues.append(f'{name}: expected exactly one declared primary key')
        if name != 'student' and (not pk or not columns.get(pk[0],{}).get('primary_key')):
            issues.append(f'{name}: declared primary key does not match database')
        obj = {'name':name,'label':original['name'],'dataSourceId':sources[key]['id'],
            'table':database+'.'+table_name,'primaryKey':pk[0] if pk else '', 'properties':props,
            'metadata':{'originalObjectId':original['id'],'description':original.get('comment',''),
                        'sourceResource':original.get('data_source',{}), 'importFormat':'bkn-object-types'}}
        if name=='student': obj['projection']={'distinct':True}
        objects.append(obj)
    relations=[]
    for original in source['relation_types']:
        if original['id'] not in RELATIONS: continue
        start=MAPPING[original['source_object_type_id']][0]
        end=MAPPING[original['target_object_type_id']][0]
        terms=[start+'.'+m['source_property']['name']+' = '+end+'.'+m['target_property']['name'] for m in original['mapping_rules']]
        relations.append({'name':original['id'],'label':original['name'],'from':start,'to':end,
            'condition':' AND '.join(terms),'cardinality':RELATIONS[original['id']],'joinType':'JOIN',
            'metadata':{'originalRelationId':original['id'],'description':original.get('comment',''),'mappingRules':original['mapping_rules']}})
    if len(objects)!=6 or len(relations)!=6: issues.append('Expected exactly six objects and six relations')
    draft={'domain':'talk51','name':'51talk','version':'1.0.0','dataSourceId':sources['APPOINT']['id'],
        'knowledgeBaseIds':[],'objects':objects,'relations':relations,'metrics':[],'defaultFilters':[],
        'description':'体验课用户分析；业务范围由查询需求决定，未确认口径需澄清。',
        'metadata':{'sourceSha256':source_hash,'excludedObjects':['class_order'],
                    'excludedRelations':['student_has_order'],'actionsImported':False,'metadataVerified':not issues}}
    return draft, issues


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('file',type=Path)
    parser.add_argument('--api',default='http://127.0.0.1:8000/api/v1')
    parser.add_argument('--out',type=Path,default=Path('artifacts/talk51/1.0.0'))
    parser.add_argument('--publish',action='store_true')
    args=parser.parse_args()
    source_bytes=args.file.read_bytes()
    source=json.loads(source_bytes.decode('utf-8-sig'))
    args.out.mkdir(parents=True,exist_ok=True)
    def save(name,value):
        (args.out/name).write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8')
    with httpx.Client(base_url=args.api,timeout=45) as api:
        def call(method,path,**kwargs):
            response=api.request(method,path,**kwargs)
            if not response.is_success: raise RuntimeError(f'{method} {path}: {response.status_code} {response.text[:1500]}')
            return response.json()
        before=call('GET','/ontology/publication-status',params={'domain':'bank_analysis'})
        bank_draft=call('GET','/ontology/drafts/bank_analysis')
        existing=call('GET','/datasources')
        sources,scans,issues={},{},[]
        for key,(database,tables) in SOURCES.items():
            ref='DATASOURCE_TALK51_'+key+'_URL'
            matches=[s for s in existing if s['secret_ref']==ref]
            if len(matches)>1: raise ValueError('Duplicate datasource secret reference: '+ref)
            sources[key]=matches[0] if matches else call('POST','/datasources',json={'name':'51talk · '+database,'type':'mysql','secret_ref':ref})
            try:
                check=call('POST','/datasources/'+sources[key]['id']+'/test')
                if check.get('database')!=database or check.get('account_read_only') is not True:
                    raise ValueError('Database name or readonly account verification failed: '+key)
                scans[key]=call('POST','/datasources/'+sources[key]['id']+'/scan',params=[('tables',t) for t in tables])
            except (RuntimeError,ValueError) as exc:
                scans[key]={}; issues.append(str(exc))
        draft,mapping_issues=convert(source,sources,scans,hashlib.sha256(source_bytes).hexdigest())
        issues.extend(mapping_issues)
        draft['metadata']['metadataVerified']=not issues
        save('ontology.json',draft)
        report={'domain':'talk51','version':'1.0.0','objects':len(draft['objects']),'relations':len(draft['relations']),
            'issues':issues,'excludedObjects':['class_order'],'excludedRelations':['student_has_order'],
            'excludedActions':[a.get('id') for a in source.get('action_types',[])], 'metricsCreated':0,
            'knowledgeBaseIds':[],'sources':{k:{'id':v['id'],'database':SOURCES[k][0],'tables':SOURCES[k][1]} for k,v in sources.items()},
            'businessRowsRead':False,'published':False}
        save('import-report.json',report)
        current=api.get('/ontology/publication-status',params={'domain':'talk51'})
        if current.is_success and current.json().get('published_version'):
            raise ValueError('talk51 is already published; refusing to overwrite an immutable version')
        call('PUT','/ontology/drafts/talk51',json=draft)
        if not issues and args.publish:
            snapshot=call('POST','/ontology/publish',params={'domain':'talk51'})
            save('snapshot.json',snapshot)
            for source_id,artifact in snapshot['wrenArtifact']['sourceProjects'].items():
                save('wren-'+source_id+'.json',artifact)
            report.update(published=True,contentHash=snapshot['contentHash'],artifactVersion=snapshot['artifactVersion'])
        after=call('GET','/ontology/publication-status',params={'domain':'bank_analysis'})
        bank_after=call('GET','/ontology/drafts/bank_analysis')
        report['bankUnchanged']=before==after and bank_draft==bank_after
        save('import-report.json',report)
        hashes={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in args.out.glob('*.json') if p.name!='hashes.json'}
        save('hashes.json',hashes)
        assert report['bankUnchanged'], 'Bank domain changed during import'
        print(json.dumps({'domain':'talk51','published':report['published'],'issues':issues,'bankUnchanged':True,'output':str(args.out)},ensure_ascii=True))

if __name__=='__main__': main()
