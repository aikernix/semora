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
                         {'ddl':'active','document':'active','history':'pending'})
        self.assertEqual(self.client.post(path+'/confirm').status_code,200)
        self.assertEqual(self.client.post(path+'/confirm').status_code,200)
        self.assertEqual(self.client.put(path,json={'tables':{}}).status_code,405)
        key, relation = next((key,r) for key,r in before['relations'].items() if r['origin']=='document')
        relation['status']='disabled'
        saved=self.client.put(path+'/relations/'+key,json=relation)
        self.assertEqual(saved.status_code,200,saved.text)
        self.assertEqual(saved.json()['status'],'disabled')
        self.assertEqual(saved.json()['history'][0]['origin'],'document')
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
        self.assertEqual(relationships['total'],2)
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
        row = self.client.get(path).json()
        relations = list(row['relations'].items())
        self.assertEqual({r['cardinality']:r['status'] for _,r in relations},
                         {'1:1':'active','1:N':'active','N:1':'active','N:N':'active','unknown':'pending'})
        self.assertTrue(any('kind' in r['condition'] and r['status']=='active' for _,r in relations))
        self.client.post(path+'/confirm')
        key, relation = next((key,r) for key,r in relations if r['cardinality']=='unknown')
        self.assertEqual(self.client.put(path+'/relations/'+key,json={**relation,'status':'active'}).status_code,422)
        self.assertEqual(self.client.put(path+'/relations/'+key,json={**relation,'status':'active','cardinality':'N:N'}).status_code,200)

    def test_workbook_and_word_declarations_preserve_document_guard(self):
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
        row = self.client.get('/v1/imports/' + response.json()['id']).json()
        self.assertEqual(row['status'], 'preview', row)
        declarations = [r for r in row['relations'].values() if r['origin'] == 'document']
        self.assertEqual(len(declarations), 2)
        self.assertTrue(all(r['status'] == 'active' for r in declarations))
        self.assertTrue(any('category' in r['condition'] and 'dictionary.xlsx' in r['evidence'] for r in declarations))

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
            self.assertIn('ticket', body['messages'][1]['content'])
            for heading in ('【角色】', '【任务】', '【输入】', '【关联判定规则】', '【输出格式】'):
                self.assertIn(heading, body['messages'][0]['content'])
            return httpx.Response(200, json={'choices':[{'message':{'content':json.dumps(model_output)}}]})
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
            ('0.01','1','request_deadline',3),
            ('1','0.01','no_delivery_deadline',1),
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
                self.assertEqual([r['timeout_kind'] for event,r in observed if event.endswith('model_failed')],
                                 [expected_kind] * attempts)

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
