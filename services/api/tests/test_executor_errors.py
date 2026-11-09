import asyncio

import httpx
import pytest

from services.executor.app.errors import database_error
from app.executor_errors import ExecutorOperationError,check_response,request_executor


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






def test_transport_error_is_typed_without_retrying_query_implicitly():
    async def scenario():
        calls=[]
        async def handler(request):calls.append(True);raise httpx.ConnectError('PRIVATE',request=request)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(ExecutorOperationError) as caught:
                await request_executor(client,'http://executor/internal/v1/query',operation='query',json={})
        assert caught.value.kind=='connect' and len(calls)==1 and 'PRIVATE' not in str(caught.value)
    asyncio.run(scenario())
