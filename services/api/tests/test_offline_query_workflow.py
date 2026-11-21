import asyncio
import json
from unittest.mock import AsyncMock

from semora_contracts import EvidenceRef, OntologySnapshotRef, RunStatus
from app import knowledge_retrieval, offline_domains, prepared_context
from app.control_state import restore_run, run_payload
from test_sql_native import native_harness


def test_offline_query_uses_common_knowledge_alignment_and_sql_record(monkeypatch):
    source_id = 'a' * 32
    prepare = prepared_context.prepare_context
    w, session, execute, estimate, model, runner = native_harness(monkeypatch,questions=[{
        'id':'period','field':'period','question':'统计哪个时间段？','reason':'时间范围影响统计结果',
    }],program={'summary':'列出所需编号','steps':[{'name':'q','purpose':'列出记录编号',
        'data_source_id':source_id,'sql':'SELECT id FROM items','output_columns':['id']}]})
    session.requirement_form={'objective':'列出记录编号','offline_source_id':source_id}
    w.snapshot_loader=AsyncMock(return_value=(OntologySnapshotRef(domain='test',version='1',content_hash='hash',
        artifact_version='1',knowledge_base_ids=['book']),{'domain':'test','knowledgeBaseIds':['book'],
            'offlineSourceIds':[source_id]}))
    catalog={'status':'ready','name':'导入目录','config':{'product':'mysql','product_label':'MySQL','version':'8.0','dialect':'mysql'},
        'tables':{'items':{'id':'items','name':'items','namespace':'','comment':'记录','primary_key':['id'],'unique':[],
                           'columns':[{'name':'id','type':'INT','nullable':False}]}},'relations':{}}
    monkeypatch.setattr(offline_domains,'offline_request',AsyncMock(return_value=catalog))
    monkeypatch.setattr(prepared_context,'prepare_context',prepare)
    book=prepared_context.knowledge_service
    search=AsyncMock(return_value=[{'slug':'definition','title':'统计定义'}])
    monkeypatch.setattr(book,'_wiki_search',search)
    monkeypatch.setattr(book,'_wiki_page',AsyncMock(return_value={'slug':'definition','title':'统计定义','content':'范围由需求确认'}))
    monkeypatch.setattr(book,'_page_evidence',lambda *_: EvidenceRef(knowledge_base_id='book',document_id='definition',
        chunk_id='one',locator='definition',revision='1',title='统计定义',summary='范围由需求确认'))
    program=model.complete_json.return_value

    async def respond(prompt, body):
        if prompt==knowledge_retrieval.KEYWORD_PROMPT:
            return {'knowledge_keywords':['记录']}
        return program

    model.complete_json.side_effect=respond

    async def scenario():
        run=await w.start(session.id,'列出记录编号',domain='test')
        assert run.status==RunStatus.CLARIFYING,run.error
        assert run.evidence[0].title=='统计定义'
        await w.align(run,'业务用户',{'period':'本季度'})
        return restore_run(run_payload(run))

    run=asyncio.run(scenario())
    assert run.status==RunStatus.COMPLETED,run.error
    assert run.offline_source_id==source_id
    assert run.question=='列出记录编号'
    assert run.requirement.confirmed_facts[0].value=='本季度'
    assert run.alignment_rounds and search.await_count
    assert run.result is None and not run.execution_receipts
    assert run.sql.dialect=='mysql' and 'SELECT id FROM items' in run.sql.physical_sql
    assert run.sql.compilation['validation']['method']=='offline_schema'
    assert run.delivery.kind=='sql'
    assert {'knowledge.prepared','alignment.required','sql.ready','sql.delivery.completed'} <= {e.event for e in run.events}
    author=[json.loads(call.args[1]) for call in model.complete_json.await_args_list
            if 'execution_scope' in call.args[1]][-1]
    assert author['execution_scope']['source_targets'][0]['dialect']=='mysql'
    execute.assert_not_awaited()
    estimate.assert_not_awaited()
