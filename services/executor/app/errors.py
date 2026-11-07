"""Sanitized database diagnostics; native codes drive policy, never message text."""
from fastapi import HTTPException


def database_error(exc, operation):
    args=getattr(exc,'args',())
    code=getattr(exc,'sqlstate',None) or (args[0] if args and isinstance(args[0],int) else None)
    kind='unknown'
    if code in {1045,'28P01','28000'}:kind='auth'
    elif code in {1044,1142,1143,1227,'42501'}:kind='permission'
    elif code in {1064,'42601'}:kind='syntax'
    elif code in {1054,1146,'42703','42P01','42702'}:kind='schema'
    elif code in {2002,2003,2006,2013,2055} or isinstance(code,str) and code.startswith('08'):kind='connect'
    elif code in {1205,1213,'40001','40P01'}:kind='transient'
    elif code in {3024,1969,'57014'}:kind='timeout'
    elif code in {1235,'0A000'}:kind='unsupported_sql'
    messages={'auth':'数据库认证失败，请核对账号及来源授权','permission':'数据库拒绝当前操作权限',
              'syntax':'数据库SQL语法检查未通过','schema':'数据库对象或字段映射检查未通过',
              'connect':'数据库连接暂不可用','transient':'数据库暂时繁忙或事务冲突',
              'timeout':'数据库操作超过时间限制','unsupported_sql':'数据库不支持当前SQL特性',
              'unknown':'数据库操作失败，需要查看诊断信息'}
    # No DSN, raw query, bound values or driver exception text leaves Executor.
    return HTTPException(502,detail={'error_type':kind,'db_error_code':code,
        'operation':operation,'message':messages[kind]})
