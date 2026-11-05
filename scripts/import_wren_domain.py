"""Validate a supplied MDL against the scanned database and import its ontology."""
import argparse
from collections import Counter
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import urllib.request
import sqlglot
from sqlglot import exp


def request(path, method='GET', body=None):
    data = None if body is None else json.dumps(body, ensure_ascii=False).encode('utf-8')
    req = urllib.request.Request('http://127.0.0.1:8000/api/v1'+path, data=data, method=method, headers={'Content-Type':'application/json'})
    with urllib.request.urlopen(req, timeout=60) as response:
        return json.load(response)


def inspect(source, draft):
    scan = request('/datasources/'+draft['dataSourceId']+'/scan', 'POST')
    tables = {(t['schema'],t['name']):t for t in scan['tables']}
    models = {m['name']:m for m in source['models']}
    issues = []
    for model in source['models']:
        ref = model['tableReference']
        table = tables.get((ref['schema'],ref['table']))
        if not table:
            issues.append({'model':model['name'],'error':'physical table missing'})
            continue
        columns = {c['name'] for c in table['columns']}
        for column in model['columns']:
            if column['name'] not in columns:
                issues.append({'model':model['name'],'column':column['name'],'error':'physical column missing'})
        if model.get('primaryKey') and model['primaryKey'] not in columns:
            issues.append({'model':model['name'],'error':'primary key column missing'})
    for relation in source.get('relationships',[]):
        for name in relation['models']:
            if name not in models:
                issues.append({'relation':relation['name'],'error':'model missing','model':name})
        condition = sqlglot.parse_one(relation['condition'], read='mysql')
        for column in condition.find_all(exp.Column):
            model, field = column.table, column.name
            if model not in models or field not in {c['name'] for c in models[model]['columns']}:
                issues.append({'relation':relation['name'],'error':'relation field missing','field':model+'.'+field})
    return {'models':len(models),'columns':sum(len(m['columns']) for m in models.values()),'relationships':len(source.get('relationships',[])),'keyless_models':[m['name'] for m in models.values() if not m.get('primaryKey')],'parallel_model_pairs':sum(n>1 for n in Counter(tuple(sorted(r['models'])) for r in source.get('relationships',[])).values()),'issues':issues}


def convert(source, draft, source_hash):
    objects = []
    for model in source['models']:
        properties = []
        for column in model['columns']:
            description = column.get('description') or column['name']
            sensitive = bool(re.search(r'密码|口令|证件|身份证|手机号|电话|客户名称|地址|住址|PASSWORD|CERTID',description+' '+column['name'],re.I))
            properties.append({'name':column['name'],'column':column['name'],'type':column['type'],'label':description.split('；')[0].strip() or column['name'],'sensitive':sensitive,'description':description,'metadata':column.get('properties',{})})
        ref = model['tableReference']
        objects.append({'name':model['name'],'label':model.get('description') or model['name'],'table':ref['schema']+'.'+ref['table'],'primaryKey':model.get('primaryKey') or '', 'properties':properties,'metadata':model.get('properties',{})})
    relations = []
    cardinality = {'ONE_TO_MANY':'1:N','MANY_TO_ONE':'N:1','ONE_TO_ONE':'1:1','MANY_TO_MANY':'N:N'}
    for value in source.get('relationships',[]):
        if value['joinType'] not in cardinality:
            raise ValueError('Unknown cardinality: '+value['joinType'])
        relations.append({'name':value['name'],'label':value.get('properties',{}).get('description') or value['name'],'from':value['models'][0],'to':value['models'][1],'cardinality':cardinality[value['joinType']],'joinType':'JOIN','condition':value['condition'],'metadata':value.get('properties',{})})
    return {**draft,'version':'0.2.0','description':'从用户提供的 Wren MDL 导入。保留原始来源和关系验证状态；指标口径尚未定义。','objects':objects,'relations':relations,'metrics':[], 'scopeField':'','timeField':'','defaultFilters':[], 'metadata':{'importFormat':'wren-mdl','sourceSha256':source_hash,'sourceCatalog':source.get('catalog'),'sourceDataSource':source.get('dataSource'),'keylessModels':[m['name'] for m in source['models'] if not m.get('primaryKey')]}}


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('file')
    parser.add_argument('--apply',action='store_true')
    args=parser.parse_args()
    path=Path(args.file)
    content=path.read_bytes()
    source=json.loads(content.decode('utf-8-sig'))
    draft=request('/ontology/drafts/bank_analysis')
    report=inspect(source,draft)
    print(json.dumps(report,ensure_ascii=True))
    if report['issues'] or not args.apply:
        return
    timestamp=datetime.now().strftime('%Y%m%d-%H%M%S')
    out=Path('artifacts/imports')/timestamp
    out.mkdir(parents=True,exist_ok=False)
    (out/'original-mdl.json').write_bytes(content)
    (out/'previous-draft.json').write_text(json.dumps(draft,ensure_ascii=False,indent=2),encoding='utf-8')
    converted=convert(source,draft,hashlib.sha256(content).hexdigest())
    (out/'converted-ontology.json').write_text(json.dumps(converted,ensure_ascii=False,indent=2),encoding='utf-8')
    (out/'validation-report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    saved=request('/ontology/drafts/bank_analysis','PUT',converted)
    assert len(saved['objects'])==report['models'] and len(saved['relations'])==report['relationships']
    assert saved.get('metadata',{}).get('sourceSha256')==converted['metadata']['sourceSha256'], 'Server dropped import provenance'
    assert sum(len(obj['properties']) for obj in saved['objects'])==report['columns']
    expected_objects={item['name']:item for item in converted['objects']}
    for item in saved['objects']:
        expected=expected_objects[item['name']]
        assert item.get('metadata')==expected.get('metadata'), 'Server dropped model evidence'
        expected_properties=[{key:value for key,value in prop.items() if key!='metadata' or value} for prop in expected['properties']]
        assert item['properties']==expected_properties, 'Server changed column definitions or evidence'
    expected_relations={item['name']:item for item in converted['relations']}
    for item in saved['relations']:
        assert item['condition']==expected_relations[item['name']]['condition']
        assert item.get('metadata')==expected_relations[item['name']].get('metadata'), 'Server dropped relationship evidence'
    print(json.dumps({'imported':True,'backup':str(out),'objects':len(saved['objects']),'relations':len(saved['relations']),'metrics':len(saved['metrics'])}))


if __name__=='__main__':main()
