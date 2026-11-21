import hashlib
import json
import re

import sqlglot
from sqlglot import exp


def signature(r):
    direct = (r['source'], r['target'], sorted(tuple(p) for p in r['pairs']))
    reverse = (r['target'], r['source'], sorted((b, a) for a, b in r['pairs']))
    return hashlib.sha256(json.dumps([min(direct, reverse), r.get('condition', '')], ensure_ascii=False).encode()).hexdigest()[:24]


def bind(mapping, tables):
    def find(ref):
        table, _, field = ref.rpartition('.')
        matches = [name for name in tables if name == table]
        if not matches:
            matches = [name for name in tables if name.casefold() == table.casefold() or name.rsplit('.',1)[-1].casefold() == table.casefold()]
        if len(matches) != 1:
            raise ValueError(f'无法唯一定位 {ref}')
        ident = matches[0]
        fields = [c['name'] for c in tables[ident]['columns']]
        fields = [field] if field in fields else [c for c in fields if c.casefold() == field.casefold()]
        if len(fields) != 1:
            raise ValueError(f'无法唯一定位 {ref}')
        return ident, fields[0]

    items = [(find(a), find(b)) for a, b in mapping]
    source, target = items[0][0][0], items[0][1][0]
    pairs = []
    for left, right in items:
        if (left[0], right[0]) == (target, source) and source != target:
            left, right = right, left
        if (left[0], right[0]) != (source, target):
            raise ValueError('一条关联应只包含两端表')
        pairs.append([left[1], right[1]])
    return source, target, pairs


def cardinality(tables, source, target, pairs):
    def unique(table, columns):
        return any(key and set(key) <= set(columns)
                   for key in [tables[table]['primary_key'], *tables[table]['unique']])
    left, right = zip(*pairs)
    return ('1' if unique(source, left) else 'N') + ':' + ('1' if unique(target, right) else 'N') if (
        unique(source, left) or unique(target, right)) else 'unknown'


def validate(r, tables, dialect):
    left, right = r['source'], r['target']
    if left not in tables or right not in tables:
        raise ValueError('请选择目录内的两端表')
    left_fields, right_fields = ({c['name'] for c in tables[t]['columns']} for t in (left, right))
    pairs = r['pairs']
    if not pairs or len({a for a, _ in pairs}) != len(pairs) or len({b for _, b in pairs}) != len(pairs):
        raise ValueError('字段映射不能为空或重复')
    if any(a not in left_fields or b not in right_fields for a, b in pairs):
        raise ValueError('字段映射引用了目录外的字段')
    condition = r.get('condition', '').strip()
    if condition:
        try:
            conditions = sqlglot.parse(condition, read=dialect)
        except sqlglot.errors.SqlglotError as exc:
            raise ValueError('附加条件语法不正确') from exc
        if len(conditions) != 1 or not isinstance(conditions[0], (exp.EQ, exp.And)):
            raise ValueError('附加条件仅支持等值条件及 AND')
        terms = list(conditions[0].flatten()) if isinstance(conditions[0], exp.And) else conditions
        for term in terms:
            if not isinstance(term, exp.EQ) or not isinstance(term.this, exp.Column) or not isinstance(term.expression, (exp.Literal, exp.Boolean, exp.Neg)):
                raise ValueError('附加条件请使用 source.字段 = 常量 或 target.字段 = 常量')
            col = term.this
            value = term.expression
            if isinstance(value, exp.Neg) and (not isinstance(value.this, exp.Literal) or value.this.is_string):
                raise ValueError('附加条件的负号只能用于数字')
            if col.args.get('db') or col.name not in {'source':left_fields, 'target':right_fields}.get(col.table, set()):
                raise ValueError('附加条件引用了不存在的字段')
        r['condition'] = conditions[0].sql(dialect=dialect)
    return r


def bind_document_mapping(mapping, tables, dialect):
    fields, guards = mapping
    left, right, pairs = bind(fields, tables)
    conditions = []
    for field, literal in guards:
        table, _, column = field.rpartition('.')
        matching = [(side,ident) for side,ident in [('source',left),('target',right)]
                    if table.casefold() in {ident.casefold(),ident.rsplit('.',1)[-1].casefold()}]
        if len(matching) != 1:
            raise ValueError(f'无法定位附加条件：{field}')
        side, ident = matching[0]
        columns = [c['name'] for c in tables[ident]['columns'] if c['name'].casefold()==column.casefold()]
        if len(columns)!=1:
            raise ValueError(f'无法定位附加条件：{field}')
        conditions.append(f"{exp.column(columns[0], table=side, quoted=True).sql(dialect=dialect)} = {literal}")
    return left, right, pairs, ' AND '.join(conditions)


def initial_relations(catalog, dialect):
    tables = catalog['tables']
    candidates = {}
    for r in catalog['relations']:
        try:
            candidates[signature(r)] = validate(r, tables, dialect)
        except ValueError as exc:
            catalog['warnings'].append(f"{r['evidence']}：{exc}")
    return candidates
