"""Create an isolated synthetic lookup table and a declarative cross-source domain.

Reads a few non-sensitive identifiers through the existing read-only Executor.
Never writes to the source database. Only the local demo PostgreSQL schema is
created/updated; balances are invented test values, not business data.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re

import httpx
import psycopg


def identifier(value):
    if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', value):
        raise ValueError('示例物理标识符必须是安全标识符')
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--api', default='http://127.0.0.1:8000/api/v1')
    parser.add_argument('--executor', default='http://127.0.0.1:8002')
    parser.add_argument('--source-id')
    parser.add_argument('--table', default='ACCT_LOAN')
    parser.add_argument('--key', default='CUSTOMERID')
    parser.add_argument('--row-id', default='SERIALNO')
    args = parser.parse_args()
    env = Path(__file__).resolve().parents[1] / '.env'
    if env.exists():
        for line in env.read_text(encoding='utf-8-sig').splitlines():
            if '=' in line and not line.lstrip().startswith('#'):
                key, value = line.split('=', 1)
                os.environ.setdefault(key, value.strip().strip('"').strip("'"))
    headers = {'Authorization': f"Bearer {os.environ['SEMORA_DEMO_TOKEN']}"} if os.getenv('SEMORA_DEMO_TOKEN') else {}
    with httpx.Client(base_url=args.api, headers=headers, timeout=40) as api:
        sources = api.get('/datasources').raise_for_status().json()
        matches = [s for s in sources if s['id'] == args.source_id] if args.source_id else [s for s in sources if s['type'] == 'mysql' and s['status'] == 'healthy']
        if len(matches) != 1:
            raise ValueError('请用--source-id指定一个已通过只读验证的MySQL来源库')
        source = matches[0]
        if source['type'] != 'mysql':
            raise ValueError('此演示脚本的来源查询使用MySQL方言')
        table, key, row_id = map(identifier, (args.table, args.key, args.row_id))
        response = httpx.post(f'{args.executor}/internal/v1/query',
            headers={'X-Semora-Internal-Token': os.getenv('INTERNAL_TOKEN', 'semora-dev-internal')},
            json={'sql': f'SELECT DISTINCT `{key}` AS entity_id FROM `{table}` WHERE `{key}` IS NOT NULL ORDER BY `{key}` LIMIT 5',
                  'parameters': {}, 'sensitive_columns': [], 'datasource': {'datasource_id': source['id'], 'type': source['type'], 'secret_ref': source['secret_ref']}}, timeout=35)
        sample = response.raise_for_status().json()
        if not sample['rows'] or sample['summary'].get('masked_columns'):
            raise ValueError('来源必须具有可读取的非敏感示例关联键')
        if any(not isinstance(row[0], str) for row in sample['rows']):
            raise ValueError('此演示模板使用文本关联键；数值键需先配置一致的数据类型，不能隐式转码')
        keys = [str(row[0]) for row in sample['rows']]
        dsn = os.getenv('SEMORA_DEMO_PG_URL', 'postgresql://semora:semora@127.0.0.1:15433/semora_business')
        with psycopg.connect(dsn) as connection:
            connection.execute('CREATE SCHEMA IF NOT EXISTS semora_cross_source_demo')
            connection.execute('CREATE TABLE IF NOT EXISTS semora_cross_source_demo.lookup_values(entity_id text PRIMARY KEY,sample_amount numeric(18,2) NOT NULL,currency text NOT NULL)')
            for index, entity_id in enumerate(keys):
                connection.execute('INSERT INTO semora_cross_source_demo.lookup_values VALUES(%s,%s,%s) ON CONFLICT(entity_id) DO UPDATE SET sample_amount=excluded.sample_amount,currency=excluded.currency',
                                   (entity_id, (index + 1) * 100, 'CNY'))
            connection.execute('GRANT USAGE ON SCHEMA semora_cross_source_demo TO semora_reader')
            connection.execute('GRANT SELECT ON semora_cross_source_demo.lookup_values TO semora_reader')
        target = next((s for s in sources if s['name'] == '跨源演示关联库 · PostgreSQL'), None)
        if not target:
            target = api.post('/datasources', json={'name': '跨源演示关联库 · PostgreSQL', 'type': 'postgresql', 'secret_ref': 'DATASOURCE_RETAIL_CREDIT_PG_URL'}).raise_for_status().json()
        api.post(f"/datasources/{target['id']}/test").raise_for_status()
        api.post(f"/datasources/{target['id']}/scan").raise_for_status()
        domain = {'domain': 'cross_source_demo', 'name': '跨源分步问数演示', 'version': '1.0.0',
            'description': '先在来源MySQL统计编号对应的记录数，再用这些编号查询另一实例PostgreSQL的合成演示金额；金额为测试数据，不代表真实业务余额。',
            'dataSourceId': source['id'], 'knowledgeBaseIds': [], 'objects': [
                {'name': 'records', 'label': '来源记录', 'table': table, 'primaryKey': row_id, 'dataSourceId': source['id'],
                 'properties': [{'name': 'record_id', 'label': '记录编号', 'column': row_id, 'type': 'string', 'sensitive': False},
                                {'name': 'entity_id', 'label': '客户关联编号', 'column': key, 'type': 'string', 'sensitive': False}]},
                {'name': 'lookup', 'label': '演示关联账户', 'table': 'semora_cross_source_demo.lookup_values', 'primaryKey': 'entity_id', 'dataSourceId': target['id'],
                 'properties': [{'name': 'entity_id', 'label': '客户关联编号', 'column': 'entity_id', 'type': 'string', 'sensitive': False},
                                {'name': 'sample_amount', 'label': '合成演示金额', 'column': 'sample_amount', 'type': 'decimal', 'sensitive': False},
                                {'name': 'currency', 'label': '币种', 'column': 'currency', 'type': 'string', 'sensitive': False}]},
            ], 'relations': [{'name': 'records_lookup', 'label': '客户编号对应演示账户', 'from': 'records', 'to': 'lookup', 'condition': 'records.entity_id = lookup.entity_id', 'cardinality': 'N:1', 'joinType': 'JOIN'}], 'metrics': []}
        existing = api.get('/ontology/publication-status', params={'domain': domain['domain']})
        if existing.is_success and existing.json().get('published_version'):
            # Idempotent reuse; never overwrite an immutable published version.
            print(json.dumps({'domain': domain['domain'], 'already_published': True, 'synthetic_rows': len(keys)}, ensure_ascii=False))
            return
        api.put(f"/ontology/drafts/{domain['domain']}", json=domain).raise_for_status()
        api.post('/ontology/publish', params={'domain': domain['domain']}).raise_for_status()
        print(json.dumps({'domain': domain['domain'], 'published': True, 'synthetic_rows': len(keys), 'source_a': source['id'], 'source_b': target['id']}, ensure_ascii=False))


if __name__ == '__main__':
    main()
