"""Temporary JSONB roundtrip against local Semora metadata only; no persistent rows."""
import json,os
from urllib.parse import urlsplit
import psycopg
from semora_contracts import QueryRun,ReviewCase,ReviewDecision,ExecutionReceipt
from app.control_state import run_payload,restore_run

url=os.environ['METADATA_DATABASE_URL']
if urlsplit(url).hostname not in {'postgres','localhost','127.0.0.1'}:
    raise RuntimeError('This probe is restricted to local Semora metadata')
run=QueryRun(session_id='synthetic',domain='generic',question='temporary metadata serialization probe')
run.review_case=ReviewCase(plan_hash='synthetic-plan',binding='synthetic-binding',input_hash='synthetic-input',
    review=ReviewDecision(decision='approved',rationale='synthetic contract only; no query execution'))
run.execution_binding='synthetic-execution'
run.agent_memory['model_evidence_refs']=[{'Ksynthetic':{'document_id':'synthetic-document','revision':'v1'}}]
run.agent_memory['task_contracts']=[{'task_id':'synthetic-task','context_hash':'synthetic-context'}]
run.execution_receipts={'step':ExecutionReceipt(step='step',row_count=0,output_hash='0'*64)}
with psycopg.connect(url,autocommit=False) as connection:
    try:
        with connection.cursor() as cursor:
            cursor.execute('CREATE TEMP TABLE semora_control_roundtrip(payload jsonb) ON COMMIT DROP')
            cursor.execute('INSERT INTO semora_control_roundtrip VALUES(%s::jsonb)',(json.dumps(run_payload(run)),))
            cursor.execute('SELECT payload FROM semora_control_roundtrip')
            restored=restore_run(cursor.fetchone()[0])
        assert restored.agent_memory==run.agent_memory
        assert restored.review_case==run.review_case
        assert restored.execution_receipts==run.execution_receipts
        assert restored.execution_binding==run.execution_binding
        assert not {'review_case','execution_binding','execution_receipts'} & restored.model_dump().keys()
    finally:
        connection.rollback()
print(json.dumps({'passed':True,'database':'local metadata temporary JSONB table','committed':False,'business_database_accessed':False}))
