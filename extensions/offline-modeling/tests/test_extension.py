from io import BytesIO, StringIO
import importlib
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from fastapi.testclient import TestClient

from backend.ddl import parse_catalog, statements

module = importlib.import_module('backend.app')
DDL = '''
CREATE TABLE owner (tenant INT, id INT, PRIMARY KEY (tenant, id));
CREATE TABLE ticket (tenant INT, owner_id INT, category INT);
ALTER TABLE ticket ADD CONSTRAINT ref FOREIGN KEY (tenant, owner_id) REFERENCES owner (tenant, id);
'''

def outline_result(blocks, *, title='对应资料', purpose='字段之间的资料', anchors=(), revisit=True, continues=False):
    return {'sections':[{'start_id':blocks[0]['block_id'],'end_id':blocks[-1]['block_id'],
                         'title':title,'purpose':purpose,'anchor_ids':list(anchors),
                         'revisit':revisit,'continues_previous':continues}]}

class ExtensionTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        old_root = module.ROOT
        module.ROOT = Path(self.directory.name)
        self.addCleanup(setattr, module, 'ROOT', old_root)
        self.client = TestClient(module.app)

    def test_statement_boundary_stays_correct_at_small_read_sizes(self):
        ddl = """-- note;
CREATE TABLE sample (id int, txt text DEFAULT 'a;b');
COMMENT ON TABLE sample IS $$one; two$$;
"""
        for size in [1, 3, 17, 65536]:
            self.assertEqual(len(list(statements(StringIO(ddl), 'postgres', size))), 2)
        mysql = "DELIMITER $$\nCREATE PROCEDURE p() BEGIN SELECT ';'; END$$\nDELIMITER ;\nCREATE TABLE x (id INT);"
        self.assertEqual(len(list(statements(StringIO(mysql), 'mysql', 3))), 2)
        with self.assertRaises(ValueError):
            list(statements(StringIO("CREATE TABLE t (x TEXT DEFAULT 'open"), 'postgres', 2))

    def test_request_packing_limits_large_document_blocks(self):
        from backend.model import plan_requests, request_payload, size, MAX_REQUEST_BYTES
        cards = {key:{'id':key,'comment':'字段说明' * 1300} for key in 'abcdef'}
        groups = [('a','b','c'),('b','c','d'),('a','e','f')]
        jobs = plan_requests(groups,cards)
        expected = {('a','b'),('a','c'),('b','c'),('b','d'),('c','d'),('a','e'),('a','f'),('e','f')}
        pairs = [pair for job in jobs for pair in job['pairs']]
        self.assertEqual(set(pairs),expected)
        self.assertEqual(len(pairs),len(expected))
        for job in jobs:
            evidence = [{'text':'a ' + '较长资料' * 1000,'locator':str(i)} for i in range(20)]
            payload = request_payload(job,cards,{key:{'name':key} for key in cards},evidence,'mysql',{})
            self.assertLessEqual(size(payload),MAX_REQUEST_BYTES)

    def test_late_foreign_key_comment_and_index(self):
        catalog = parse_catalog(StringIO(DDL + '''
COMMENT ON COLUMN ticket.owner_id IS '负责人';
CREATE INDEX idx ON ticket (tenant, owner_id);
'''), 'postgres', 'public')
        self.assertEqual(catalog['relations'][0]['pairs'], [('tenant','tenant'),('owner_id','id')])
        self.assertEqual(catalog['tables']['public.ticket']['columns'][1]['comment'], '负责人')
        self.assertEqual(catalog['tables']['public.ticket']['indexes'][0]['fields'], ['tenant','owner_id'])

    def test_required_ddl_and_document_declarations(self):
        self.assertEqual(self.client.post('/v1/imports',data={'name':'离线','dialect':'postgres'}).status_code,422)
        files = [('ddl',('schema.sql',DDL)),('documents',('notes.txt',
            '关联关系：ticket.category = owner.id\n\n'
            'SELECT * FROM ticket t JOIN owner o ON t.owner_id = o.id'))]
        response = self.client.post('/v1/imports',data={'name':'离线','dialect':'postgres'},files=files)
        self.assertEqual(response.status_code,202,response.text)
        path='/v1/imports/'+response.json()['id']
        listing = self.client.get('/v1/imports').json()
        self.assertEqual([(r['id'],r['name'],r['product'],r['ddl']) for r in listing],
                         [(response.json()['id'],'离线','postgresql','schema.sql')])
        before=self.client.get(path).json()
        self.assertEqual(before['status'],'preview',before)
        self.assertEqual({r['origin']:r['status'] for r in before['relations'].values()},
                         {'ddl':'active'})
        self.assertEqual(self.client.post(path+'/confirm').status_code,200)
        self.assertEqual(self.client.post(path+'/confirm').status_code,200)
        self.assertEqual(self.client.put(path,json={'tables':{}}).status_code,405)
        key, relation = next((key,r) for key,r in before['relations'].items() if r['origin']=='ddl')
        relation['status']='disabled'
        saved=self.client.put(path+'/relations/'+key,json=relation)
        self.assertEqual(saved.status_code,200,saved.text)
        self.assertEqual(saved.json()['status'],'disabled')
        self.assertEqual(saved.json()['history'][0]['origin'],'ddl')
        relation['status']='active'
        self.assertEqual(self.client.put(path+'/relations/'+key,json=relation).status_code,200)
        invalid={**relation,'pairs':[['missing','id']]}
        self.assertEqual(self.client.put(path+'/relations/'+key,json=invalid).status_code,422)
        self.assertEqual(self.client.get(path).json()['tables'],before['tables'])

    def test_integrated_offline_requires_credentials_when_main_site_is_authenticated(self):
        with patch.dict('os.environ', {'OFFLINE_REQUIRE_ACCESS_PASSWORD':'true','OFFLINE_ACCESS_PASSWORD':''}):
            self.assertEqual(self.client.get('/v1/imports').status_code,503)
        with patch.dict('os.environ', {'OFFLINE_REQUIRE_ACCESS_PASSWORD':'true','OFFLINE_ACCESS_PASSWORD':'fixture'}):
            self.assertEqual(self.client.get('/v1/imports').status_code,401)
            self.assertEqual(self.client.get('/v1/imports',auth=('offline','fixture')).status_code,200)

    def test_confirmed_upload_supports_shared_drawers_and_physical_delete(self):
        res=self.client.post('/v1/imports',data={'name':'离线抽屉','product':'postgresql','auto_confirm':'true'},files=[
            ('ddl',('schema.sql',DDL)),
            ('documents',('sample.txt','关联关系：ticket.category = owner.id'))])
        ident=res.json()['id']
        path='/v1/imports/'+ident
        row=self.client.get(path).json()
        self.assertEqual(row['status'],'ready')
        self.assertIsNone(row['analysis'])
        self.assertEqual(self.client.get(path+'/tables',params={'page_size':1}).json()['total'],2)
        detail=self.client.get(path+'/tables/detail',params={'table_id':'ticket'}).json()
        self.assertEqual(len(detail['columns']),3)
        self.assertEqual(len(detail['foreign_keys']),1)
        relationships=self.client.get(path+'/relationships',params={'status':'accepted'}).json()
        self.assertEqual(relationships['total'],1)
        self.assertTrue(all(r['decision_reason'] is None and 'evidence' not in r for r in relationships['items']))
        self.assertEqual(self.client.delete(path).status_code,204)
        self.assertEqual(self.client.get(path).status_code,404)
        self.assertFalse((module.ROOT/ident).exists())

    def test_sql_only_uses_imported_catalog_and_checks_table_scope(self):
        import httpx
        import json

        res=self.client.post('/v1/imports',data={'name':'生成 SQL','product':'postgresql','auto_confirm':'true'},
                             files={'ddl':('schema.sql',DDL)})
        path='/v1/imports/'+res.json()['id']+'/sql'
        factory=httpx.AsyncClient
        for sql, status in [('SELECT id FROM owner',200),('SELECT id FROM missing',422),
                            ('DELETE FROM owner',422),('SELECT id FROM owner; DELETE FROM owner',422)]:
            with self.subTest(sql=sql):
                def respond(request):
                    messages=json.loads(request.content)['messages']
                    self.assertIn('只读查询',messages[0]['content'])
                    payload=json.loads(messages[1]['content'])
                    self.assertEqual(payload['database']['dialect'],'postgres')
                    self.assertEqual(len(payload['relations']),1)
                    return httpx.Response(200,json={'choices':[{'message':{'content':json.dumps({'sql':sql})}}]})
                with patch.dict('os.environ', {'OFFLINE_MODEL_URL':'http://test/v1','OFFLINE_MODEL_NAME':'fixture'}):
                    with patch('backend.query.httpx.AsyncClient', side_effect=lambda **kw: factory(
                            transport=httpx.MockTransport(respond), **kw)):
                        response=self.client.post(path,json={'question':'查询负责人'})
                self.assertEqual(response.status_code,status,response.text)
                if status==200:
                    self.assertEqual(response.json(),{'sql':sql})

    def test_offline_analysis_cancel_keeps_published_relations(self):
        import asyncio
        from concurrent.futures import ThreadPoolExecutor
        from threading import Event

        import httpx

        res=self.client.post('/v1/imports',data={'name':'取消分析','product':'postgresql','auto_confirm':'true'},
                             files={'ddl':('schema.sql',DDL)})
        path='/v1/imports/'+res.json()['id']
        before=self.client.get(path).json()['relations']
        started=Event()
        factory=httpx.AsyncClient

        async def respond(_):
            started.set()
            await asyncio.sleep(5)

        with patch.dict('os.environ', {'OFFLINE_MODEL_URL':'http://test/v1','OFFLINE_MODEL_NAME':'fixture'}):
            with patch('backend.model.httpx.AsyncClient', side_effect=lambda **kw: factory(
                    transport=httpx.MockTransport(respond), **kw)), ThreadPoolExecutor() as pool:
                future=pool.submit(self.client.post,path+'/analysis')
                self.assertTrue(started.wait(2))
                self.assertEqual(self.client.delete(path).status_code,409)
                self.assertEqual(self.client.post(path+'/analysis/cancel').status_code,202)
                self.assertEqual(future.result(2).status_code,202)
        state=self.client.get(path).json()
        self.assertIsNone(state['analysis'])
        self.assertEqual(state['relations'],before)

    def test_interrupted_analysis_restores_available_import_or_last_completed_run(self):
        from contextlib import closing

        response=self.client.post('/v1/imports',data={'name':'中断恢复','product':'postgresql','auto_confirm':'true'},
                                  files={'ddl':('schema.sql',DDL)})
        ident=response.json()['id']
        path='/v1/imports/'+ident
        before=self.client.get(path).json()

        with closing(module.connect()) as db:
            module.update(db,ident,'analysis=?',('分析中断，可重试',))
        module.recover()
        self.assertIsNone(self.client.get(path).json()['analysis'])

        with patch.dict('os.environ', {'OFFLINE_MODEL_URL':'http://test/v1','OFFLINE_MODEL_NAME':'fixture'}):
            with patch('backend.app.discover', return_value=[]):
                self.assertEqual(self.client.post(path+'/analysis').status_code,202)
        with closing(module.connect()) as db:
            module.update(db,ident,'analysis=?',('运行中',))
        module.recover()
        after=self.client.get(path).json()
        self.assertEqual(after['analysis'],'完成')
        self.assertEqual(after['tables'],before['tables'])
        self.assertEqual(after['relations'],before['relations'])

    def test_failed_structure_has_no_effective_directory(self):
        response=self.client.post('/v1/imports',data={'dialect':'mysql','name':'坏文件'},
                                  files={'ddl':('schema.sql','CREATE TABLE broken (x ')})
        path='/v1/imports/'+response.json()['id']
        row=self.client.get(path).json()
        self.assertEqual(row['status'],'failed')
        self.assertNotIn('tables',row)
        self.assertEqual(self.client.post(path+'/confirm').status_code,409)

    def test_ddl_only(self):
        response=self.client.post('/v1/imports',data={'dialect':'postgres','name':'仅 DDL'},
                                  files={'ddl':('schema.sql',DDL)})
        row=self.client.get('/v1/imports/'+response.json()['id']).json()
        self.assertEqual(row['status'],'preview',row)
        self.assertEqual(len(row['relations']),1)

    def test_product_compatibility_is_explicit_and_covers_vendor_exports(self):
        self.assertEqual(self.client.post('/v1/imports', data={'product':'oceanbase','name':'缺少模式'},
                         files={'ddl':('schema.sql',DDL)}).status_code, 422)
        fixture = [
            ('goldendb','','CREATE TABLE t (id BIGINT PRIMARY KEY) ENGINE=InnoDB;', 't'),
            ('vastbase','','CREATE TABLE public.t (id BIGINT PRIMARY KEY);', 'public.t'),
            ('kingbase','postgres','CREATE TABLE public.t (id BIGINT PRIMARY KEY);', 'public.t'),
            ('opengauss','','CREATE TABLE public.t (id BIGINT PRIMARY KEY);', 'public.t'),
            ('oceanbase','mysql','CREATE TABLE t (id BIGINT PRIMARY KEY);', 't'),
            ('oceanbase','oracle','ALTER SESSION SET CURRENT_SCHEMA = demo; CREATE TABLE t (id NUMBER PRIMARY KEY);', 'DEMO.T'),
            ('dameng','','CREATE TABLE "DEMO"."T" ("ID" NUMBER PRIMARY KEY);', 'DEMO.T'),
            ('oracle','','CREATE TABLE t (id NUMBER PRIMARY KEY);', 'T'),
            ('sqlserver','',"USE [demo]\nGO\nCREATE TABLE [dbo].[T] ([Id] INT, CONSTRAINT pk PRIMARY KEY CLUSTERED ([Id] ASC))\nGO\n", 'demo.dbo.T'),
        ]
        for product, mode, ddl, table in fixture:
            with self.subTest(product=product,mode=mode):
                reply = self.client.post('/v1/imports',data={'product':product,'compatibility_mode':mode,'name':product},
                                         files={'ddl':('schema.sql',ddl)})
                self.assertEqual(reply.status_code,202,reply.text)
                row = self.client.get('/v1/imports/'+reply.json()['id']).json()
                self.assertEqual(row['status'],'preview',row)
                self.assertEqual(row['config']['product'],product)
                self.assertIn(table,row['tables'])

    def test_history_activates_known_cardinalities_and_preserves_join_conditions(self):
        import json

        import httpx

        ddl = '''
CREATE TABLE a (id INT PRIMARY KEY, ref INT, code INT, kind INT);
CREATE TABLE b (id INT PRIMARY KEY, ref INT, code INT);
'''
        notes = '''
SELECT * FROM a x JOIN b y ON x.id=y.id

SELECT * FROM a x JOIN b y ON x.id=y.ref

SELECT * FROM a x JOIN b y ON x.ref=y.id AND x.kind=7

SELECT * FROM a x JOIN b y ON x.ref=y.ref

关联基数：多对多
SELECT * FROM a x JOIN b y ON x.code=y.code
'''
        reply = self.client.post('/v1/imports',data={'dialect':'postgres','name':'历史关系'},files=[
            ('ddl',('schema.sql',ddl)),('documents',('history.txt',notes))])
        path = '/v1/imports/'+reply.json()['id']
        self.assertEqual(self.client.get(path).json()['relations'],{})
        self.client.post(path+'/confirm')

        def respond(request):
            data = json.loads(json.loads(request.content)['messages'][1]['content'])
            if data['phase']=='document_outline':
                result = outline_result(data['blocks'])
            elif data['phase']=='document_extract':
                rows = []
                for source in data['blocks']:
                    text = source['text']
                    sql = text[text.index('SELECT'):]
                    item = {'kind':'history','quote':sql}
                    if '多对多' in text:
                        item.update(cardinality='多对多',citations=[{'block_id':source['block_id'],
                                                                    'quote':'关联基数：多对多'}])
                    rows.append({'block_id':source['block_id'],'items':[item]})
                result = {'blocks':rows}
            else:
                self.assertEqual(data['phase'],'keywords')
                result = {'keywords':[]}
            return httpx.Response(200,json={'choices':[{'message':{'content':json.dumps(result)}}]})

        factory = httpx.AsyncClient
        with patch.dict('os.environ',{'OFFLINE_MODEL_URL':'http://test/v1','OFFLINE_MODEL_NAME':'fixture'}), patch(
                'backend.model.httpx.AsyncClient',side_effect=lambda **kw:factory(
                    transport=httpx.MockTransport(respond),**kw)):
            self.assertEqual(self.client.post(path+'/analysis').status_code,202)
        row = self.client.get(path).json()
        relations = list(row['relations'].items())
        self.assertEqual({r['cardinality']:r['status'] for _,r in relations},
                         {'1:1':'active','1:N':'active','N:1':'active','N:N':'active','unknown':'pending'})
        self.assertTrue(any('kind' in r['condition'] and r['status']=='active' for _,r in relations))
        key, relation = next((key,r) for key,r in relations if r['cardinality']=='unknown')
        self.assertEqual(self.client.put(path+'/relations/'+key,json={**relation,'status':'active'}).status_code,422)
        self.assertEqual(self.client.put(path+'/relations/'+key,json={**relation,'status':'active','cardinality':'N:N'}).status_code,200)

    def test_workbook_and_word_keep_original_layout_without_header_based_publication(self):
        from docx import Document
        from openpyxl import Workbook

        book = Workbook()
        book.active.append(['表英文名', '关联关系'])
        book.active.append(['ticket', 'ticket.owner_id = owner.id AND ticket.category = 7'])
        workbook = BytesIO()
        book.save(workbook)
        document = Document()
        document.add_paragraph('关联关系：ticket.tenant = owner.tenant')
        word = BytesIO()
        document.save(word)
        response = self.client.post('/v1/imports', data={'dialect':'postgres','name':'多文件'}, files=[
            ('ddl',('schema.sql',DDL)), ('documents',('dictionary.xlsx',workbook.getvalue())),
            ('documents',('notes.docx',word.getvalue()))])
        ident = response.json()['id']
        row = self.client.get('/v1/imports/' + ident).json()
        self.assertEqual(row['status'], 'preview', row)
        declarations = [r for r in row['relations'].values() if r['origin'] == 'document']
        self.assertEqual(declarations,[])
        records = module.import_catalog(ident)['records']
        self.assertTrue(any(r.get('cells')==['ticket','ticket.owner_id = owner.id AND ticket.category = 7']
                            for r in records))

    def test_request_packing_covers_each_recalled_pair_once(self):
        from collections import Counter
        from itertools import combinations

        from backend.model import MAX_REQUEST_BYTES, MAX_TABLES_PER_REQUEST, plan_requests, request_payload, size

        tables = {f't{i:02}': {'name':f't{i:02}'} for i in range(24)}
        cards = {name:{'id':name, 'comment':'字段' * 500} for name in tables}
        names = list(tables)
        groups = [names[:18], names[5:22], names[10:], names[:18]]
        expected = {pair for group in groups for pair in combinations(sorted(group), 2)}
        jobs = plan_requests(groups, cards)
        self.assertEqual(Counter(pair for job in jobs for pair in job['pairs']),
                         Counter({pair:1 for pair in expected}))
        for job in jobs:
            self.assertLessEqual(len(job['tables']), MAX_TABLES_PER_REQUEST)
            self.assertLessEqual(size(request_payload(job, cards, tables, [], 'mysql', {})), MAX_REQUEST_BYTES)

    def test_model_analysis_classifies_proposals_without_restoring_disabled_relation(self):
        import httpx
        import json

        ddl = DDL + """
ALTER TABLE owner ADD UNIQUE (id);
ALTER TABLE ticket ADD COLUMN backup_owner INT;
COMMENT ON COLUMN ticket.backup_owner IS '备用负责人，owner.id';
"""
        response = self.client.post('/v1/imports', data={'dialect':'postgres','name':'模型用例'}, files={
            'ddl':('schema.sql',ddl)})
        path = '/v1/imports/' + response.json()['id']
        self.client.post(path + '/confirm')
        key, relation = next(iter(self.client.get(path).json()['relations'].items()))
        self.client.put(path+'/relations/'+key, json={**relation,'status':'disabled'})
        model_output = {'relations':[
            {'kind':'entity_reference', 'mapping':[['ticket.category','owner.id']], 'reason':'缺少说明'},
            {'kind':'entity_reference', 'mapping':[['ticket.backup_owner','owner.id']], 'reason':'注释指向目标列'},
            {'kind':'shared_entity', 'mapping':[['ticket.tenant','owner.tenant']], 'reason':'共同主体'},
            {'kind':'entity_reference', 'mapping':[['ticket.tenant','owner.tenant'],['ticket.owner_id','owner.id']], 'reason':'已有关系'},
        ]}
        def respond(request):
            body = json.loads(request.content)
            payload = json.loads(body['messages'][1]['content'])
            for heading in ('【角色】', '【任务】', '【输出格式】'):
                self.assertIn(heading, body['messages'][0]['content'])
            if payload['phase'] == 'keywords':
                # Only the source matches the keyword. Its explicit column comment still seeds this pair.
                result = {'keywords':['backup_owner']}
            elif payload['phase'] == 'compare':
                self.assertIn('【关联判定规则】', body['messages'][0]['content'])
                result = model_output
            else:
                self.assertEqual(payload['phase'], 'review')
                result = {'decisions':[{'id':item['id'],'outcome':'usable'} for item in payload['candidates']]}
            return httpx.Response(200, json={'choices':[{'message':{'content':json.dumps(result)}}]})
        factory = httpx.AsyncClient
        with patch.dict('os.environ', {'OFFLINE_MODEL_URL':'http://test/v1','OFFLINE_MODEL_NAME':'fixture'}):
            with patch('backend.model.httpx.AsyncClient', side_effect=lambda **kw: factory(
                    transport=httpx.MockTransport(respond), **kw)):
                result = self.client.post(path+'/analysis')
        self.assertEqual(result.status_code,202,result.text)
        row = self.client.get(path).json()
        self.assertEqual(row['analysis'],'完成',row)
        self.assertEqual(row['relations'][key]['status'],'disabled')
        self.assertEqual({r['pairs'][0][0]:r['status'] for r in row['relations'].values() if r['origin']=='model'},
                         {'category':'pending', 'backup_owner':'active'})
        self.assertIn('直接指向', next(r['review_reason'] for r in row['relations'].values() if r['status'] == 'pending'))
        with patch.dict('os.environ', {'OFFLINE_MODEL_URL':'http://test/v1','OFFLINE_MODEL_NAME':'fixture'}):
            with patch('backend.app.discover', side_effect=ValueError('模型失败')):
                self.client.post(path+'/analysis')
            self.assertEqual(self.client.get(path).json()['relations'], row['relations'])
            with patch('backend.app.discover', return_value=[]):
                self.client.post(path+'/analysis')
        current = self.client.get(path).json()
        self.assertEqual(current['analysis'], '完成')
        self.assertEqual(current['relations'], {k:r for k,r in row['relations'].items() if r['status'] != 'pending'})

    def test_model_deadline_and_no_delivery_have_separate_retry_policies(self):
        import asyncio

        import httpx

        from backend.model import analysis_progress, discover

        tables = parse_catalog(StringIO(DDL), 'postgres', '')['tables']
        factory = httpx.AsyncClient

        async def respond(_):
            await asyncio.sleep(1)

        for request, delivery, expected_kind, attempts in (
            ('0.01','1','request_deadline',[1,2,3]*3),
            ('1','0.01','no_delivery_deadline',[1,1,1]),
        ):
            with self.subTest(expected_kind=expected_kind):
                observed = []
                settings = {'OFFLINE_MODEL_URL':'http://test/v1','OFFLINE_MODEL_NAME':'fixture',
                            'LLM_TIMEOUT_SECONDS': request, 'RELATIONSHIP_NO_DELIVERY_SECONDS':delivery,
                            'LLM_REQUEST_RETRIES':'2'}
                with patch.dict('os.environ', settings), patch('backend.model.httpx.AsyncClient',
                        side_effect=lambda **kw: factory(transport=httpx.MockTransport(respond), **kw)):
                    with analysis_progress(lambda event, **data: observed.append((event, data))):
                        with self.assertRaises(ValueError):
                            list(discover(tables, [], 'postgres', {}))
                failures = [r for event,r in observed if event.endswith('model_failed')]
                self.assertEqual([r['timeout_kind'] for r in failures],[expected_kind]*len(attempts))
                self.assertEqual([r['http_attempt'] for r in failures],attempts)

    def test_keyword_recall_organizes_wide_hits_and_routes_groups_independently(self):
        import json
        from itertools import combinations

        import httpx

        from backend.model import analysis_progress, discover

        ddl = '\n'.join(f'CREATE TABLE catalog_{i:02} (id INT PRIMARY KEY, catalog_key INT, notes TEXT);'
                        for i in range(24))
        tables = parse_catalog(StringIO(ddl), 'postgres', '')['tables']
        ids = sorted(tables)
        observed, compared, events = [], [], {}

        def respond(request):
            body = json.loads(request.content)
            data = json.loads(body['messages'][1]['content'])
            phase = data['phase']
            observed.append(phase)
            if phase == 'keywords':
                self.assertLessEqual(len(data['tables']), 10)
                self.assertTrue(all(f['name'] != 'notes' for t in data['tables'] for f in t['columns']))
                result = {'keywords':['Cat Alog','CATALOG','id','does_not_exist','catalog_00']}
            elif phase == 'organization':
                self.assertEqual(len(data['tables']),24)
                result = {'groups':[{'table_ids':ids[:4]},{'table_ids':ids[2:6]},
                                    {'table_ids':[ids[0],'missing']}]}
            elif phase == 'compare':
                compared.extend(tuple(pair) for pair in data['comparison_pairs'])
                result = {'relations':[{'kind':'broken'}, {'kind':'entity_reference',
                           'mapping':[[f'{ids[0]}.catalog_key', f'{ids[1]}.id']], 'reason':'待核对'}]}
            else:
                self.assertEqual(phase,'review')
                result = {'decisions':[{'id':row['id'],'outcome':'unresolved','reason':'缺编号口径'}
                                       for row in data['candidates']]}
            return httpx.Response(200,json={'choices':[{'message':{'content':json.dumps(result)}}]})

        factory = httpx.AsyncClient
        with patch.dict('os.environ',{'OFFLINE_MODEL_URL':'http://test/v1','OFFLINE_MODEL_NAME':'fixture'}), patch(
                'backend.model.httpx.AsyncClient',side_effect=lambda **kw:factory(
                    transport=httpx.MockTransport(respond),**kw)):
            with analysis_progress(lambda event, **data:events.update({event:data})):
                relations = dict(discover(tables,[],'postgres',{}))
        self.assertEqual(observed.count('keywords'),3)
        self.assertEqual(observed.count('organization'),1)
        self.assertEqual(set(compared),set(combinations(ids[:4],2))|set(combinations(ids[2:6],2)))
        self.assertEqual(len(compared),len(set(compared)))
        self.assertEqual(events['offline.analysis.recall']['single_hits'],1)
        self.assertEqual(events['offline.analysis.organization_discarded']['discarded'],1)
        self.assertEqual([r['review_reason'] for r in relations.values()],['缺编号口径'])

    def test_large_group_reads_evidence_then_concludes_on_repeated_request(self):
        import json

        import httpx

        from backend.model import discover

        tables = parse_catalog(StringIO(DDL), 'postgres', '')['tables']
        ids = sorted(tables)
        phases = []
        proposal = {'kind':'entity_reference',
                    'mapping':[['ticket.owner_id','owner.id']], 'reason':'字段含义指向目标'}

        def respond(request):
            body = json.loads(request.content)
            data = json.loads(body['messages'][1]['content'])
            phases.append(data['phase'])
            if data['phase']=='keywords':
                result = {'keywords':['owner']}
            elif data['phase']=='agent':
                result = {'action':'read_tables','arguments':{'table_ids':ids}}
            elif data['phase']=='conclusion':
                self.assertEqual([x['action'] for x in data['recent']],['read_tables'])
                self.assertEqual({r['table_id'] for r in data['recent'][0]['result']['tables']},set(ids))
                result = {'relations':[proposal], 'gaps':[]}
            else:
                self.assertEqual(data['phase'],'review')
                result = {'decisions':[{'id':r['id'],'outcome':'usable'} for r in data['candidates']]}
            return httpx.Response(200,json={'choices':[{'message':{'content':json.dumps(result)}}]})

        factory = httpx.AsyncClient
        with patch.dict('os.environ',{'OFFLINE_MODEL_URL':'http://test/v1','OFFLINE_MODEL_NAME':'fixture',
                                      'RELATIONSHIP_AGENT_GROUP_THRESHOLD':'1'}), patch(
                'backend.model.httpx.AsyncClient',side_effect=lambda **kw:factory(
                    transport=httpx.MockTransport(respond),**kw)):
            relations = dict(discover(tables,[],'postgres',{}))
        self.assertEqual(phases,['keywords','agent','agent','conclusion','review'])
        self.assertEqual([r['pairs'] for r in relations.values()],[[['owner_id','id']]])

    def test_analysis_retry_reuses_completed_requests_from_durable_checkpoint(self):
        import json
        from contextlib import closing

        import httpx

        response = self.client.post('/v1/imports',data={'dialect':'postgres','name':'恢复用例'},files={
            'ddl':('schema.sql',DDL)})
        ident = response.json()['id']
        path = '/v1/imports/' + ident
        self.client.post(path+'/confirm')
        previous = self.client.get(path).json()['relations']
        calls = []

        def respond(request):
            body = json.loads(request.content)
            phase = json.loads(body['messages'][1]['content'])['phase']
            calls.append(phase)
            if phase == 'compare' and calls.count('compare') <= 3:
                return httpx.Response(500)
            result = {'keywords':['owner']} if phase=='keywords' else {'relations':[]}
            return httpx.Response(200,json={'choices':[{'message':{'content':json.dumps(result)}}]})

        factory = httpx.AsyncClient
        with patch.dict('os.environ',{'OFFLINE_MODEL_URL':'http://test/v1','OFFLINE_MODEL_NAME':'fixture',
                                      'LLM_REQUEST_RETRIES':'0'}), patch(
                'backend.model.httpx.AsyncClient',side_effect=lambda **kw:factory(
                    transport=httpx.MockTransport(respond),**kw)):
            self.client.post(path+'/analysis')
            with closing(module.connect()) as db:
                self.assertEqual(db.execute('SELECT COUNT(*) FROM analysis_checkpoints WHERE import_id=?',
                                            (ident,)).fetchone()[0],1)
            self.assertEqual(self.client.get(path).json()['relations'],previous)
            self.client.post(path+'/analysis')
        self.assertEqual(calls,['keywords']+['compare']*4)
        self.assertEqual(self.client.get(path).json()['analysis'],'完成')
        self.assertEqual(self.client.get(path).json()['relations'],previous)
        with closing(module.connect()) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM analysis_checkpoints WHERE import_id=?',
                                        (ident,)).fetchone()[0],0)

    def test_document_semantics_binds_custom_columns_and_plain_text_to_ddl(self):
        import json

        import httpx
        from openpyxl import Workbook

        ddl = """
CREATE TABLE component (id INT PRIMARY KEY);
COMMENT ON TABLE component IS '器件档案';
COMMENT ON COLUMN component.id IS '器件编号';
CREATE TABLE maintenance (component_ref INT);
COMMENT ON TABLE maintenance IS '保养流水';
COMMENT ON COLUMN maintenance.component_ref IS '对应器件';
CREATE TABLE inspection (component_code INT);
CREATE TABLE measurement (reference_code INT);
CREATE TABLE signal (reference_code INT);
"""
        book = Workbook()
        book.active.append(['资料组甲','按甲使用','台账乙','乙中位置'])
        book.active.append(['保养流水','对应器件','器件档案','器件编号'])
        stream = BytesIO()
        book.save(stream)
        statement = 'inspection 的 component_code 对应 component 的 id。'
        hint = 'measurement 和 signal 记录的是同一类采集业务，需要核对对象口径。'
        response = self.client.post('/v1/imports',data={'dialect':'postgres','name':'文档语义','auto_confirm':'true'},files=[
            ('ddl',('schema.sql',ddl)),('documents',('custom.xlsx',stream.getvalue())),
            ('documents',('guide.txt',statement+'\n\n'+hint))])
        path = '/v1/imports/' + response.json()['id']
        self.assertEqual(self.client.get(path).json()['relations'],{})
        calls = []

        def respond(request):
            data = json.loads(json.loads(request.content)['messages'][1]['content'])
            phase = data['phase']
            calls.append(phase)
            if phase=='document_outline':
                result = outline_result(data['blocks'])
            elif phase=='document_extract':
                rows = []
                for block in data['blocks']:
                    raw = block['text']
                    if '保养流水' in raw:
                        self.assertEqual(block['cells'],['保养流水','对应器件','器件档案','器件编号'])
                        items = [{'kind':'declaration','quote':raw,'source':'保养流水','target':'器件档案',
                                  'pairs':[['对应器件','器件编号']]}]
                    elif raw==statement:
                        items = [{'kind':'declaration','quote':raw,'source':'inspection','target':'component',
                                  'pairs':[['component_code','id']]},
                                 {'kind':'declaration','quote':'原文没有这段','source':'signal','target':'component',
                                  'pairs':[['reference_code','id']]}]
                    elif raw==hint:
                        items = [{'kind':'hint','quote':raw,'source':'measurement','target':'signal','summary':'采集业务对象'}]
                    else:
                        items = []
                    rows.append({'block_id':block['block_id'],'items':items})
                result = {'blocks':rows}
            elif phase=='document_bind':
                bindings = []
                for value in data['bindings']:
                    item = value['candidate']
                    if item['kind']=='hint':
                        bindings.append({'id':item['id'],'table_ids':['measurement','signal']})
                        continue
                    source,column = ('maintenance','component_ref') if item['source']=='保养流水' else (
                        'inspection','component_code')
                    self.assertTrue({source,'component'} <= {t['table_id'] for t in value['tables']})
                    bindings.append({'id':item['id'],'source':source,'target':'component','pairs':[[column,'id']]})
                result = {'bindings': bindings}
            elif phase=='keywords':
                result = {'keywords':[]}
            else:
                self.assertEqual(phase,'compare')
                self.assertEqual(data['comparison_pairs'],[['measurement','signal']])
                self.assertTrue(any(e.get('summary')=='采集业务对象' for e in data['evidence']))
                result = {'relations':[]}
            return httpx.Response(200,json={'choices':[{'message':{'content':json.dumps(result)}}]})

        factory = httpx.AsyncClient
        with patch.dict('os.environ',{'OFFLINE_MODEL_URL':'http://test/v1','OFFLINE_MODEL_NAME':'fixture'}), patch(
                'backend.model.httpx.AsyncClient',side_effect=lambda **kw:factory(
                    transport=httpx.MockTransport(respond),**kw)):
            self.assertEqual(self.client.post(path+'/analysis').status_code,202)
        row = self.client.get(path).json()
        self.assertEqual(row['analysis'],'完成')
        self.assertEqual({(r['source'],r['target'],r['status']) for r in row['relations'].values()},
                         {('maintenance','component','active'),('inspection','component','active')})
        self.assertEqual(row['document_analysis']['processed_blocks'],4)
        self.assertEqual(row['document_analysis']['issues'],1)
        directory = module.import_catalog(response.json()['id'])['document_directory']
        self.assertEqual({entry['file'] for entry in directory},{'custom.xlsx','guide.txt'})
        self.assertEqual(directory[0]['start'],'Sheet!第 1 行')
        self.assertIn('compare',calls)

    def test_history_where_filters_do_not_become_relation_conditions(self):
        from backend.documents import historical_mappings

        result = list(historical_mappings(
            'SELECT * FROM first p JOIN second q ON p.id=q.first_id AND q.kind=3 WHERE p.status=7',
            'postgres'))
        self.assertEqual(result,[([('first.id','second.first_id')],[('second.kind','3')])])

    def test_document_outline_carries_mid_file_anchors_and_links_later_passages(self):
        import json

        import httpx
        from backend.model import analysis_progress, discover

        tables = parse_catalog(StringIO(
            'CREATE TABLE inventory (component_ref INT); CREATE TABLE component (id INT PRIMARY KEY);'),
            'postgres','')['tables']
        records = [{'file':'guide.xlsx','section':'原始资料','locator':f'第 {i+1} 行','text':f'原始内容 {i}'}
                   for i in range(28)]
        records[5]['text'] = '位置甲 | 编码口径 | 归处'
        records[17]['text'] = 'inventory 的 component_ref 与后面的附记配套使用'
        records[20]['text'] = '这以下换另一种说明方式'
        records[25]['text'] = '附记：匹配 component 的 id。'
        progress = {}

        def respond(request):
            data = json.loads(json.loads(request.content)['messages'][1]['content'])
            phase = data['phase']
            if phase=='document_outline':
                blocks = data['blocks']
                if blocks[0]['block_id']=='b0':
                    result = {'sections':[
                        *outline_result(blocks[:5],title='编制过程',revisit=False)['sections'],
                        *outline_result(blocks[5:],title='对应方式',anchors=['b5'])['sections']]}
                elif blocks[0]['block_id']=='b12':
                    self.assertEqual([row['block_id'] for row in data['context']['anchors']],['b5'])
                    result = {'sections':[
                        *outline_result(blocks[:8],anchors=['b5'],continues=True)['sections'],
                        *outline_result(blocks[8:],title='另一份独立说明',anchors=['b20'],revisit=False)['sections']]}
                else:
                    result = outline_result(blocks,title='附记',anchors=['b25'],revisit=False)
            elif phase=='document_outline_links':
                self.assertEqual({row['id'] for row in data['sections']},{'b0','b5','b20','b24'})
                result = {'links':[
                    {'source_id':'b5','context_id':'b24','block_ids':['b25']},
                    {'source_id':'b5','context_id':'b24','block_ids':['b0']}]}
            elif phase=='document_extract':
                ids = [row['block_id'] for row in data['blocks']]
                self.assertEqual(data['outline']['id'],'b5')
                self.assertNotIn('b20',ids)
                rows = []
                for row in data['blocks']:
                    items = []
                    if row['block_id']=='b17':
                        self.assertEqual({entry['block_id'] for entry in data['context']},{'b5','b25'})
                        valid = {'kind':'declaration','source':'inventory.component_ref','target':'component.id','quote':row['text'],
                                 'pairs':[['component_ref','id']],
                                 'citations':[{'block_id':'b25','quote':records[25]['text']}]}
                        items = [valid,{**valid,'citations':[{'block_id':'b0','quote':records[0]['text']}]}]
                    rows.append({'block_id':row['block_id'],'items':items})
                result = {'blocks':rows}
            elif phase=='document_bind':
                result = {'bindings':[{'id':row['candidate']['id'],'source':'inventory','target':'component',
                                       'pairs':[['component_ref','id']]}
                                      for row in data['bindings']]}
            else:
                self.assertEqual(phase,'keywords')
                result = {'keywords':[]}
            return httpx.Response(200,json={'choices':[{'message':{'content':json.dumps(result)}}]})

        factory = httpx.AsyncClient
        with patch.dict('os.environ',{'OFFLINE_MODEL_URL':'http://test/v1','OFFLINE_MODEL_NAME':'fixture'}), patch(
                'backend.model.httpx.AsyncClient',side_effect=lambda **kw:factory(
                    transport=httpx.MockTransport(respond),**kw)):
            with analysis_progress(lambda event,**data:progress.update({event:data})):
                relations = dict(discover(tables,records,'postgres',{}))
        self.assertEqual([(r['source'],r['target'],r['status']) for r in relations.values()],
                         [('inventory','component','active')])
        report = progress['offline.analysis.documents_completed']
        self.assertEqual(report['summary']['directory_sections'],4)
        self.assertEqual(report['summary']['invalid_directory_links'],1)
        self.assertEqual(report['summary']['processed_blocks'],15)
        self.assertEqual(report['summary']['issues'],1)
        self.assertEqual(report['directory'][1]['references'],['b25'])

    def test_document_guard_must_match_the_original_qualified_predicate(self):
        from backend.document_analysis import document_relation

        tables = parse_catalog(StringIO(
            'CREATE TABLE a (ref INT, kind INT); CREATE TABLE b (id INT PRIMARY KEY);'),
            'postgres','')['tables']
        candidate = {'kind':'declaration','quote':'a.ref = b.id AND a.kind = 7','pairs':[['ref','id']],
                     'source':'a','target':'b','condition':'a.kind = 7','cardinality':'','location':'example'}
        binding = {'source':'a','target':'b','pairs':[['ref','id']],'condition':'source.kind = 7'}
        visible = {'a':{'ref','kind'},'b':{'id'}}
        relation = document_relation(candidate,binding,tables,'postgres',visible)
        self.assertEqual(relation['status'],'active')
        unproven = document_relation(candidate,{**binding,'condition':'source.kind = 8'},tables,'postgres',visible)
        self.assertEqual(unproven['status'],'pending')

    def test_document_batch_failure_resumes_without_reanalyzing_finished_blocks(self):
        import json
        from collections import Counter

        import httpx
        from backend.model import analysis_progress, discover

        tables = parse_catalog(StringIO(DDL),'postgres','')['tables']
        records = [{'file':f'{name}.txt','locator':'第 1 行','text':name} for name in ('first','second')]
        cache, calls = {}, Counter()

        def checkpoint(key, result=None):
            if result is not None:
                cache[key] = result
            return cache.get(key)

        def respond(request):
            data = json.loads(json.loads(request.content)['messages'][1]['content'])
            if data['phase']=='keywords':
                result = {'keywords':[]}
            elif data['phase']=='document_outline':
                result = outline_result(data['blocks'])
            else:
                block = data['blocks'][0]
                calls[block['text']] += 1
                if block['text']=='second' and calls['second']<=3:
                    result = {'blocks':[]}  # A partial payload cannot mark the block complete.
                else:
                    result = {'blocks':[{'block_id':block['block_id'],'items':[]}]}
            return httpx.Response(200,json={'choices':[{'message':{'content':json.dumps(result)}}]})

        factory = httpx.AsyncClient
        with patch.dict('os.environ',{'OFFLINE_MODEL_URL':'http://test/v1','OFFLINE_MODEL_NAME':'fixture'}), patch(
                'backend.model.httpx.AsyncClient',side_effect=lambda **kw:factory(
                    transport=httpx.MockTransport(respond),**kw)):
            with analysis_progress(lambda *_args,**_kw:None,checkpoint=checkpoint):
                with self.assertRaisesRegex(ValueError,'document_extract'):
                    list(discover(tables,records,'postgres',{}))
                self.assertEqual(list(discover(tables,records,'postgres',{})),[])
        self.assertEqual(calls,{'first':1,'second':4})

    def test_model_defaults_match_online_and_provider_options_can_be_omitted(self):
        from backend.model import model_options, positive_seconds

        with patch.dict('os.environ', {}, clear=True):
            self.assertEqual(positive_seconds('LLM_TIMEOUT_SECONDS'), 300)
            self.assertEqual(positive_seconds('RELATIONSHIP_NO_DELIVERY_SECONDS'), 300)
            self.assertEqual(model_options('json_object'),
                             {'response_format':{'type':'json_object'},'enable_thinking':False})
        with patch.dict('os.environ', {'RELATIONSHIP_THINKING_MODE':'omit','LLM_ENABLE_THINKING':'true'}):
            self.assertEqual(model_options('text'), {})

    def test_stream_delivery_deadline_stops_after_public_output_only(self):
        import asyncio
        import json

        import httpx

        from backend.model_transport import NoDeliveryTimeout, complete_json

        def frame(delta, finish=None):
            return ('data: '+json.dumps({'choices':[{'delta':delta,'finish_reason':finish}]})+'\n\n').encode()

        class Stream(httpx.AsyncByteStream):
            def __init__(self, public):
                self.public=public

            async def __aiter__(self):
                yield frame({'content':'{"relations":'} if self.public else {'reasoning_content':'thinking'})
                await asyncio.sleep(.08)
                yield frame({'content':'[]}'}, 'stop')
                yield b'data: [DONE]\n\n'

        async def consume(public, request_seconds):
            async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda request:httpx.Response(200,headers={'content-type':'text/event-stream'},stream=Stream(public))
            )) as client:
                return await complete_json(client,'http://test/chat/completions',{},request_seconds,.04)

        self.assertEqual(asyncio.run(consume(True, .5)),{'relations':[]})
        with self.assertRaises(NoDeliveryTimeout):
            asyncio.run(consume(False, .5))
        with self.assertRaises(TimeoutError) as result:
            asyncio.run(consume(True, .06))
        self.assertNotIsInstance(result.exception, NoDeliveryTimeout)

    def test_shared_mapping_excludes_only_unsubstantiated_sibling_edge(self):
        from backend.model import exclude_shared_entity

        ddl = "CREATE TABLE a (ref INT, direct INT); CREATE TABLE b (ref INT); CREATE TABLE hub (id INT PRIMARY KEY);"
        tables = parse_catalog(StringIO(ddl), 'postgres', '')['tables']
        def relation(source, target, pairs, status='pending', **fields):
            return dict(source=source, target=target, pairs=pairs, status=status, condition='', **fields)
        known = {key:relation(key, 'hub', [['ref','id']], 'active') for key in ('a','b')}
        proposals = {
            'shared':relation('a','b',[['ref','ref']]),
            'direct':relation('a','b',[['direct','ref']]),
            'record':relation('a','b',[['ref','ref']], relationship_kind='direct_record_reference'),
        }
        self.assertEqual(exclude_shared_entity(proposals, known, tables), {'shared'})

    def test_evidence_favors_pair_and_omits_already_parsed_sql(self):
        from backend.model import request_payload

        tables = {key:{'name':key} for key in ('a','b')}
        records = [
            {'file':'notes.sql', 'locator':'1', 'text':'SELECT * FROM a JOIN b'},
            {'file':'dictionary.xlsx', 'locator':'2', 'text':'a'},
            {'file':'dictionary.xlsx', 'locator':'3', 'text':'a 与 b'},
            {'file':'dictionary.xlsx', 'locator':'4', 'text':'table_a'},
        ]
        known = {'id':{'source':'a','target':'b','pairs':[['ref','id']],'condition':'',
                       'origin':'history','evidence':'notes.sql · 1'}}
        job = {'tables':set(tables),'pairs':[('a','b')]}
        payload = request_payload(job, tables, tables, records, 'mysql', known)
        self.assertEqual([e['location'] for e in payload['evidence']], ['dictionary.xlsx · 3','dictionary.xlsx · 2'])
        self.assertEqual(len(payload['known']), 1)
