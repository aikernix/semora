import asyncio
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException

from services.executor.app.errors import database_error
from app.executor_errors import ExecutorOperationError,check_response,request_executor
from app.cost_estimation import estimate_query
from app.gate import review_sql


@pytest.mark.parametrize('code,kind',[(1064,'syntax'),(1054,'schema'),(1045,'auth'),(1142,'permission'),(2003,'connect'),(1235,'unsupported_sql')])
def test_mysql_native_codes_are_classified_without_leaking_driver_message(code,kind):
    error=database_error(Exception(code,'PRIVATE_PASSWORD and SQL literal'),'estimate')
    assert error.detail['error_type']==kind and error.detail['db_error_code']==code
    assert 'PRIVATE' not in str(error.detail)


def test_psycopg3_sqlstate_and_unknown_error_are_preserved_safely():
    class PgError(Exception):sqlstate='42703'
    assert database_error(PgError('SECRET'),'estimate').detail['error_type']=='schema'
    assert database_error(TypeError('SECRET'),'estimate').detail['error_type']=='unknown'


@pytest.mark.parametrize('detail,expected',[
    ({'error_type':'syntax','db_error_code':1064},'syntax'),
    ('MySQL failure','unknown'),
    ({'error_type':'capability'},'unknown'),
    ({'error_type':'capability','operation':'estimate'},'capability')])
def test_http_502_does_not_imply_transient_or_estimate_capability(detail,expected):
    response=httpx.Response(502,json={'detail':detail})
    with pytest.raises(ExecutorOperationError) as caught:check_response(response,'estimate')
    assert caught.value.kind==expected


def test_shared_estimate_policy_retries_once_and_never_retries_auth_or_sql():
    async def scenario():
        for kind,count in [('connect',2),('timeout',2),('auth',1),('permission',1),('syntax',1),('unknown',1)]:
            calls=[];events=[]
            async def estimate(*args):calls.append(True);raise ExecutorOperationError(kind)
            workflow=SimpleNamespace(estimate=estimate,emit=lambda *args:events.append(args))
            run=SimpleNamespace(agent_memory={},delivery_warnings=[],status='gating')
            artifact=SimpleNamespace(physical_sql='SELECT 1 LIMIT 1',parameters={},data_source_id='one')
            with pytest.raises(ExecutorOperationError):await estimate_query(workflow,run,artifact)
            assert len(calls)==count and run.agent_memory['execution_failure'][0]['error_type']==kind
    asyncio.run(scenario())


def test_explicit_estimate_capability_loss_keeps_unknown_and_honors_approval_policy():
    async def scenario():
        async def estimate(*args):raise ExecutorOperationError('capability')
        workflow=SimpleNamespace(estimate=estimate,emit=lambda *args:None)
        run=SimpleNamespace(agent_memory={},delivery_warnings=[],status='gating')
        artifact=SimpleNamespace(physical_sql='SELECT 1 LIMIT 1',parameters={},data_source_id='one')
        result=await estimate_query(workflow,run,artifact)
        assert result['estimated_rows'] is None and run.delivery_warnings
        for mode,approval in [('disabled',False),('risk_based',True),('all',True)]:
            gate=review_sql(artifact.physical_sql,require_organization_scope=False,estimated_rows=None,estimate_unavailable=True,approval_mode=mode)
            assert gate.allowed and gate.requires_approval==approval and gate.risk_level=='medium'
            assert next(c.message for c in gate.checks if c.name=='estimated_result_size')=='成本估算不可用，未做行数预判'
    asyncio.run(scenario())


def test_transport_error_is_typed_without_retrying_query_implicitly():
    async def scenario():
        calls=[]
        async def handler(request):calls.append(True);raise httpx.ConnectError('PRIVATE',request=request)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(ExecutorOperationError) as caught:
                await request_executor(client,'http://executor/internal/v1/query',operation='query',json={})
        assert caught.value.kind=='connect' and len(calls)==1 and 'PRIVATE' not in str(caught.value)
    asyncio.run(scenario())
