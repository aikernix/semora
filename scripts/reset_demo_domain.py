"""One-off local demo reset. Run only after taking a PostgreSQL backup."""
import json
import os
import re
import urllib.request

import psycopg

SOURCE = 'c48a3b49-29ac-48ad-bcde-461503e3e102'
DOMAIN = 'bank_analysis'
KB = 'c6a77307-4c92-411f-b2c0-71dbf412f02f'


def create_domain():
    with psycopg.connect(os.environ['METADATA_DATABASE_URL']) as connection:
        result = connection.execute('SELECT scan_result FROM semora_meta.datasources WHERE id=%s', (SOURCE,)).fetchone()[0]
    names = {'ACCT_LOAN': ('loan', '贷款账户'), 'CUSTOMER_INFO': ('customer', '客户基本信息'), 'ORG_INFO': ('organization', '机构')}
    objects = []
    for table in result['tables']:
        if table['name'] not in names:
            continue
        primary = [column['name'] for column in table['columns'] if column['primary_key']]
        if len(primary) != 1:
            raise RuntimeError(f"{table['name']} requires a confirmed single primary key")
        name, label = names[table['name']]
        properties = []
        for column in table['columns']:
            physical_type = column['type'].lower()
            kind = 'decimal' if physical_type.startswith(('decimal', 'numeric')) else 'integer' if 'int' in physical_type else 'date' if physical_type == 'date' else 'timestamp' if physical_type.startswith(('datetime', 'timestamp')) else 'string'
            sensitive = column['sensitivity'] != 'internal' or bool(re.search(r'客户名称|手机号|电话|证件|身份证|住址|地址', column.get('comment', '')))
            properties.append({'name': column['name'].lower(), 'column':column['name'], 'label':column.get('comment', '').split('；')[0] or column['name'], 'type':kind, 'sensitive':sensitive, 'nullable':column['nullable']})
        objects.append({'name':name,'label':label,'table':table['schema']+'.'+table['name'],'primaryKey':primary[0],'properties':properties})
    assert len(objects) == 3
    draft = {'domain':DOMAIN,'name':'银行数据分析','version':'0.1.0','description':'基于 askoda_bank_demo 的银行数据分析域。贷款、客户、机构对象从真实 Schema 导入；关系、指标及机构范围口径待核对，尚未发布。','dataSourceId':SOURCE,'knowledgeBaseIds':[KB],'objects':objects,'relations':[],'metrics':[],'defaultFilters':[]}
    request = urllib.request.Request('http://127.0.0.1:8000/api/v1/ontology/drafts/'+DOMAIN, data=json.dumps(draft,ensure_ascii=False).encode(),headers={'Content-Type':'application/json'},method='PUT')
    with urllib.request.urlopen(request,timeout=30) as response:
        saved = json.load(response)
    print(json.dumps({'created_domain':saved['domain'],'objects':len(saved['objects'])}))


def cleanup():
    # Deliberately targets this local Compose PostgreSQL, never the external MySQL.
    with psycopg.connect('postgresql://semora:semora@postgres:5432/semora_business') as connection:
        with connection.cursor() as cursor:
            cursor.execute("CREATE TEMP TABLE old_runs AS SELECT id FROM semora_meta.runs WHERE COALESCE(payload->>'domain','retail_credit') IN ('retail_credit','deposit')")
            cursor.execute("SELECT count(*) FROM old_runs")
            counts = {'runs':cursor.fetchone()[0]}
            # Local demo reset only: immutable test ledgers are removed inside
            # this transaction; normal application ledger protection is unchanged.
            cursor.execute('SET LOCAL session_replication_role = replica')
            for table in ('audit_archives','audit_events'):
                cursor.execute(f'DELETE FROM semora_meta.{table} WHERE run_id IN (SELECT id FROM old_runs)')
                counts[table] = cursor.rowcount
            for table in ('checkpoint_writes','checkpoint_blobs','checkpoints'):
                cursor.execute(f'DELETE FROM semora_meta.{table} WHERE thread_id IN (SELECT id FROM old_runs)')
                counts[table] = cursor.rowcount
            cursor.execute('DELETE FROM semora_meta.runs WHERE id IN (SELECT id FROM old_runs)')
            cursor.execute("DELETE FROM semora_meta.sessions WHERE domain='retail_credit'")
            counts['sessions'] = cursor.rowcount
            cursor.execute("DELETE FROM semora_meta.datasources WHERE id IN ('retail-credit-pg','1b1c1193-4ce9-4ef4-87ff-5968573f93ef','a21f7129-907f-4af3-8af4-1c381032f1d5')")
            counts['datasources'] = cursor.rowcount
            for table in ('candidates','snapshots','drafts'):
                cursor.execute(f"DELETE FROM ontology_core.{table} WHERE domain='retail_credit'")
                counts[table] = cursor.rowcount
            for table in ('payments','loans','customers','branches'):
                cursor.execute(f'DELETE FROM public.{table}')
                counts['demo_'+table] = cursor.rowcount
    print(json.dumps({'removed_local_demo':counts}))


if __name__ == '__main__':
    import sys
    if sys.argv[1:] == ['create']:
        create_domain()
    elif sys.argv[1:] == ['cleanup-after-backup']:
        cleanup()
    else:
        raise SystemExit('Use create or cleanup-after-backup after taking a backup')
