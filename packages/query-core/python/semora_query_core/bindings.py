"""Render typed bind nodes; SQL syntax and driver parameter style are separate."""
import re

from sqlglot import exp


def render_bound_expression(tree, parameters, *, dialect='postgres', paramstyle='pyformat'):
    """Never replace SQL text or interpolate values. Current drivers use pyformat.

    New database adapters must explicitly select a supported parameter style;
    adding a SQL dialect alone does not imply driver/database compatibility.
    """
    if paramstyle not in {'pyformat', 'named'}:
        raise ValueError(f'未适配的数据库参数风格：{paramstyle}')

    def bind(node):
        if not isinstance(node, exp.Placeholder):
            return node
        name = node.name
        if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', name or '') or name not in parameters:
            raise ValueError('SQL参数引用无效或未绑定')
        # Var emits only our validated placeholder name, never a parameter value.
        return exp.Var(this=f'%({name})s' if paramstyle == 'pyformat' else f':{name}')

    return tree.transform(bind, copy=True).sql(dialect=dialect)
