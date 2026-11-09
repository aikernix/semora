"""Sanitized database diagnostics; native codes drive policy, never message text."""
from fastapi import HTTPException
import re


def database_error(exc, operation):
    args=getattr(exc,'args',())
    code=getattr(exc,'sqlstate',None) or (args[0] if args and isinstance(args[0],int) else None)
    kind='unknown'
    if code in {1045,'28P01','28000'}:kind='auth'
    elif code in {1044,1142,1143,1227,'42501'}:kind='permission'
    elif code in {1064,1055,1111,1247,1582,'42601','42803'}:kind='syntax'
    elif code in {1054,1146,1305,'42703','42P01','42702','42883'}:kind='schema'
    elif code in {2002,2003,2006,2013,2055} or isinstance(code,str) and code.startswith('08'):kind='connect'
    elif code in {1205,1213,'40001','40P01'}:kind='transient'
    elif code in {3024,1969,'57014'}:kind='timeout'
    elif code in {1235,'0A000','42804','42846','42P18'}:kind='unsupported_sql'
    messages={'auth':'数据库认证失败，请核对账号及来源授权','permission':'数据库拒绝当前操作权限',
              'syntax':'数据库SQL语法检查未通过','schema':'数据库对象或字段映射检查未通过',
              'connect':'数据库连接暂不可用','transient':'数据库暂时繁忙或事务冲突',
              'timeout':'数据库操作超过时间限制','unsupported_sql':'数据库不支持当前SQL特性',
              'unknown':'数据库操作失败，需要查看诊断信息'}
    # Return only curated structural diagnostics, never raw SQL/values/DSNs.
    diagnostic={}
    if kind in {'syntax','schema','unsupported_sql'}:
        diag=getattr(exc,'diag',None)
        for attr,key in [('column_name','column'),('table_name','table'),('statement_position','position'),('datatype_name','datatype')]:
            value=getattr(diag,attr,None)
            if value and re.fullmatch(r'[\w. ]{1,200}',str(value)):diagnostic[key]=str(value)
        primary=getattr(diag,'message_primary',None)
        if primary:
            primary=re.sub(r"'(?:[^']|'')*'", "'<value>'",str(primary))
            primary=re.sub(r'\b\w+://\S+', '<connection>', primary)
            diagnostic['message']=primary[:1000]
        elif code in {1054,1146,1305,1055} and len(args)>1:
            # MySQL quotes identifiers with single quotes in these messages.
            names=re.findall(r"'([A-Za-z_][A-Za-z0-9_.$]{0,199})'",str(args[1]))
            if names:diagnostic['message']='Referenced identifiers: '+', '.join(names[:4])
        elif code == 1247:
            diagnostic['message']='An aggregate output alias cannot be referenced inside another ORDER BY expression; use the aggregate expression itself.'
        elif code == 1064 and len(args)>1 and str(args[1]).startswith('You have an error in your SQL syntax;'):
            # Retain only known SQL tokens from the failing fragment, no SQL
            # literals or arbitrary driver text (which may contain credentials).
            message=str(args[1])
            near=re.search(r"\bnear '(.*)' at line (\d+)\s*$",message,re.S)
            if near:
                tokens=re.findall(r'\b(?:ROW_NUMBER|OVER|PARTITION|WITH|RECURSIVE|LATERAL|FILTER|NULLS|FIRST|LAST|CASE|CAST|ORDER|GROUP|BY)\b',near[1],re.I)
                diagnostic={'position_line':near[2], 'sql_tokens':list(dict.fromkeys(t.upper() for t in tokens))[:12]}
    return HTTPException(502,detail={'error_type':kind,'db_error_code':code,
        'operation':operation,'message':messages[kind],**({'diagnostic':diagnostic} if diagnostic else {})})
