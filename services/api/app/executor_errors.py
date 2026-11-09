"""Typed Executor boundary. Unknown/unstructured errors never imply capability loss."""
import httpx


KINDS={'auth','permission','syntax','schema','connect','transient','timeout','unsupported_sql','capability','unknown'}
MESSAGES={'auth':'数据库认证失败','permission':'数据库操作无权执行','syntax':'数据库SQL语法错误',
          'schema':'数据库对象或字段映射错误','connect':'Executor或数据库连接暂不可用',
          'transient':'数据库暂时繁忙','timeout':'数据库操作超时','unsupported_sql':'数据库不支持当前SQL特性',
          'capability':'查询成本估算能力不可用','unknown':'Executor返回未识别的操作错误'}


class ExecutorOperationError(RuntimeError):
    def __init__(self,kind='unknown',code=None,diagnostic=None):
        self.kind=kind if kind in KINDS else 'unknown'
        self.code=code if isinstance(code,(int,str)) and len(str(code))<=12 else None
        self.diagnostic={k:str(v)[:1000] for k,v in (diagnostic or {}).items()
                         if k in {'message','column','table','position','datatype'}}
        self.retryable=self.kind in {'connect','transient','timeout'}
        super().__init__(MESSAGES[self.kind]+(f'（错误码 {self.code}）' if self.code is not None else ''))

    def public(self):return {'error_type':self.kind,'db_error_code':self.code,'retryable':self.retryable}


def check_response(response,operation):
    if response.is_success:return
    try:
        payload=response.json()
        detail=payload.get('detail',payload) if isinstance(payload,dict) else {}
        if not isinstance(detail,dict):detail={}
    except ValueError:detail={}
    # Capability must explicitly refer to estimate; HTTP 404/5xx is insufficient.
    kind=detail.get('error_type','unknown')
    if kind=='capability' and (operation!='estimate' or detail.get('operation')!='estimate'):
        kind='unknown'
    raise ExecutorOperationError(kind,detail.get('db_error_code'),detail.get('diagnostic'))


def transport_error(exc):
    return ExecutorOperationError('timeout' if isinstance(exc,httpx.TimeoutException) else 'connect')


async def request_executor(client,url,*,operation,**kwargs):
    try:response=await client.post(url,**kwargs)
    except httpx.RequestError as exc:raise transport_error(exc) from exc
    check_response(response,operation)
    return response
