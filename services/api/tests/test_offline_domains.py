import unittest
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import database_routes, main, offline_domains


class OfflineDomainTests(unittest.TestCase):
    def setUp(self):
        app = FastAPI()
        app.include_router(offline_domains.router)
        app.include_router(database_routes.router)
        self.client = TestClient(app)
        self.identifier = 'a' * 32
        self.domain = '/api/v1/ontology/configurations/example'

    def test_offline_binding_requires_a_ready_import(self):
        saved = {'offlineSourceIds':[self.identifier]}
        bind = AsyncMock(return_value=saved)
        with patch.object(offline_domains,'ready_offline_sources',AsyncMock(return_value=[])), patch.object(
                offline_domains.ontology_service,'bind_offline_source',bind):
            self.assertEqual(self.client.post(self.domain+'/offline-sources/'+self.identifier).status_code,404)
            bind.assert_not_awaited()
        with patch.object(offline_domains,'ready_offline_sources',AsyncMock(return_value=[{'id':self.identifier}])), patch.object(
                offline_domains.ontology_service,'bind_offline_source',bind):
            result = self.client.post(self.domain+'/offline-sources/'+self.identifier)
            self.assertEqual(result.json(),saved)
            bind.assert_awaited_once_with('example',self.identifier)

    def test_sql_and_cross_source_use_different_bindings(self):
        draft={'offlineSourceIds':[self.identifier],'sourcePackageRefs':[{'dataSourceId':'online'}]}
        model=AsyncMock(return_value={'sql':'SELECT * FROM items'})
        with patch.object(offline_domains.ontology_service,'get_draft',AsyncMock(return_value={'offlineSourceIds':[self.identifier]})), patch.object(
                offline_domains.ontology_service,'get_configuration',AsyncMock(return_value=draft)), patch.object(
                offline_domains,'offline_request',model):
            result=self.client.post(self.domain+'/cross-source-analysis',
                                    json={'left_source_id':'online','right_source_id':self.identifier})
            self.assertEqual(result.status_code,422)
            self.assertIn('不能参与跨库关联',result.json()['detail'])
            path='/api/v1/domains/example/offline-sql'
            self.assertEqual(self.client.post(path,json={'source_id':'b'*32,'question':'统计记录'}).status_code,404)
            self.assertEqual(self.client.post(path,json={'source_id':self.identifier,'question':'统计记录'}).status_code,200)
            model.assert_awaited_once_with('POST',f'/v1/imports/{self.identifier}/sql',timeout=650,json={'question':'统计记录'})

    def test_bound_source_requires_unbinding_before_delete(self):
        with patch.object(offline_domains.ontology_service,'list_drafts',AsyncMock(return_value=[
                {'offlineSourceIds':[self.identifier]}])), patch.object(offline_domains,'offline_request',AsyncMock()) as remote:
            self.assertEqual(self.client.delete('/api/v1/ontology/offline-sources/'+self.identifier).status_code,409)
            remote.assert_not_awaited()

    def test_picker_enriches_existing_domains_only(self):
        drafts=[{'domain':'offline','status':'unconfigured','offlineSourceIds':[self.identifier]},
                {'domain':'empty','status':'unconfigured'}]
        imports=[{'id':self.identifier,'name':'已绑定目录'},
                 {'id':'b'*32,'name':'独立目录'}]
        with patch.object(main.ontology_service,'list_drafts',AsyncMock(return_value=drafts)), patch.object(
                main,'ready_offline_sources',AsyncMock(return_value=imports)):
            rows=TestClient(main.app).get('/api/v1/domains').json()
        self.assertEqual([row['domain'] for row in rows],['offline','empty'])
        self.assertEqual(rows[0]['status'],'configured')
        self.assertEqual(rows[0]['sourceMode'],'offline')
        self.assertEqual(rows[1]['status'],'unconfigured')

    def test_query_scope_comes_from_domain_and_rejects_legacy_conflict(self):
        import asyncio
        from fastapi import HTTPException

        body=main.SessionCreate(domain='example',requirement_form={'objective':'统计记录'})
        with patch.object(main.ontology_service,'get_draft',AsyncMock(return_value={'offlineSourceIds':[self.identifier]})):
            asyncio.run(main.validate_query_scope(body))
        self.assertEqual(body.requirement_form['offline_source_id'],self.identifier)
        with patch.object(main.ontology_service,'get_draft',AsyncMock(return_value={
                'offlineSourceIds':[self.identifier],'sourcePackageRefs':[{'dataSourceId':'online'}]})):
            with self.assertRaises(HTTPException) as result:
                asyncio.run(main.validate_query_scope(body))
        self.assertEqual(result.exception.status_code,409)
