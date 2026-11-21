"""Read SQL dumps without executing them. Only complete statements reach the parser."""
import re
from collections.abc import Iterator
from typing import TextIO

import sqlglot
from sqlglot import exp

MAX_STATEMENT = 2 * 1024 * 1024


def statements(stream: TextIO, dialect: str, chunk_size: int = 65536) -> Iterator[tuple[int, str]]:
    buffer, offset, eof = '', 0, False
    line, first, delimiter = 1, 1, ';'
    quote = None
    parts = []
    seen = False
    size, comment_depth, line_start = 0, 0, True
    while True:
        if len(buffer) - offset < 256 and not eof:
            more = stream.read(chunk_size)
            buffer, offset = buffer[offset:] + more, 0
            eof = not more
            if not eof and len(buffer) < 256:
                continue
        if offset >= len(buffer):
            break

        def consume(n):
            nonlocal offset, line, line_start
            value = buffer[offset:offset+n]
            offset += n
            line += value.count('\n')
            if value:
                line_start = value.endswith('\n')
            return value

        tail = buffer[offset:offset+256]
        char = tail[0]
        start_line = line
        if quote is None and line_start:
            separator = (r'[ \t]*GO(?:[ \t]+1)?[ \t]*(?:\r?\n|$)' if dialect == 'tsql'
                         else r'[ \t]*/[ \t]*(?:\r?\n|$)' if dialect == 'oracle' else None)
            if separator and (match := re.match(separator, tail, re.I)):
                consume(len(match[0]))
                if seen:
                    yield first, ''.join(parts).strip()
                parts, seen, size = [], False, 0
                continue
        if quote == 'line':
            consume(1)
            if char == '\n':
                quote = None
                parts.append(' ')
            continue
        if quote == 'comment':
            if tail.startswith('*/'):
                consume(2)
                comment_depth -= 1
                if not comment_depth:
                    quote = None
                    if seen:
                        parts.append(' ')
            elif dialect == 'postgres' and tail.startswith('/*'):
                consume(2)
                comment_depth += 1
            else:
                consume(1)
            continue
        if quote is not None:
            if char == '\\' and (dialect == 'mysql' or quote == "E'") and quote in {"'", '"', "E'"}:
                token = consume(2)
            elif tail.startswith(quote[-1] if quote == "E'" else quote):
                end = "'" if quote == "E'" else quote
                token = consume(len(end))
                if len(end) == 1 and buffer[offset:offset+1] == end:
                    token += consume(1)
                else:
                    quote = None
            else:
                token = consume(1)
        else:
            directive = re.match(r'DELIMITER[ \t]+([^\s]{1,32})[ \t]*(?:\r?\n|$)', tail, re.I)
            if dialect == 'mysql' and not seen and directive:
                delimiter = directive[1]
                consume(len(directive[0]))
                continue
            if (tail.startswith('--') and (dialect != 'mysql' or len(tail) == 2 or tail[2].isspace())) or (dialect == 'mysql' and char == '#'):
                quote = 'line'
                consume(2 if char == '-' else 1)
                continue
            if tail.startswith('/*'):
                if dialect == 'mysql' and re.match(r'/\*!\d*\s*(?:CREATE|ALTER|DROP|RENAME)\b', tail, re.I):
                    raise ValueError(f'第 {line} 行存在版本条件内的结构定义，请导出为普通 DDL')
                quote = 'comment'
                comment_depth = 1
                consume(2)
                continue
            if tail.startswith(delimiter):
                consume(len(delimiter))
                if seen:
                    yield first, ''.join(parts).strip()
                parts, seen, size = [], False, 0
                continue
            dollar = re.match(r'\$(?:[A-Za-z_][\w]{0,100})?\$', tail) if dialect == 'postgres' else None
            if dollar:
                quote = dollar[0]
                token = consume(len(quote))
            elif dialect == 'oracle' and len(tail) >= 3 and tail[:2].lower() == "q'":
                quote = {'[':']','{':'}','(':')','<':'>'}.get(tail[2],tail[2]) + "'"
                token = consume(3)
            elif dialect == 'tsql' and char == '[':
                quote = ']'
                token = consume(1)
            elif dialect == 'postgres' and tail[:2].lower() == "e'" and (not parts or not parts[-1][-1:].isalnum()):
                quote = "E'"
                token = consume(2)
            else:
                if char in {'"', "'", '`'}:
                    quote = char
                token = consume(1)
        if not seen:
            if not token.strip():
                continue
            seen, first = True, start_line
        parts.append(token)
        size += len(token)
        if size > MAX_STATEMENT:
            raise ValueError(f'第 {first} 行的 SQL 语句超过单条解析上限')
    if quote not in {None, 'line'}:
        raise ValueError(f'第 {first} 行存在未闭合的引号或注释')
    if seen:
        yield first, ''.join(parts).strip()


def parse_catalog(stream, dialect, namespace=''):
    tables = {}
    foreign = []
    warnings = []
    catalog = ''
    if dialect == 'tsql':
        namespace = namespace or 'dbo'

    def name(node):
        value = node.name
        if node.args.get('quoted'):
            return value
        return value.lower() if dialect == 'postgres' else value.upper() if dialect == 'oracle' else value

    def table_id(node, default=None):
        if not isinstance(node, exp.Table):
            raise ValueError('无法定位物理表')
        parts = [name(x) for x in node.parts]
        prefix = namespace if default is None else default
        if len(parts) == 1 and prefix:
            parts.insert(0, prefix)
        if dialect == 'tsql' and catalog and len(parts) == 2 and '.' not in parts[0]:
            parts.insert(0, catalog)
        return '.'.join(parts)

    def fields(nodes):
        result = []
        for node in nodes or []:
            if isinstance(node, exp.Ordered):
                node = node.this
            if isinstance(node, exp.Column):
                node = node.this
            if not isinstance(node, exp.Identifier):
                raise ValueError('键字段包含无法确认的表达式')
            result.append(name(node))
        return result

    def reference(table, columns, node, line, trusted=True):
        target = node.this
        target_id = table_id(target.this if isinstance(target, exp.Schema) else target, table['namespace'])
        foreign.append((table['id'], columns, target_id, fields(target.expressions) if isinstance(target, exp.Schema) else [], line, trusted))

    def apply(table, node, line, trusted=True):
        if isinstance(node, exp.ColumnDef):
            column = {'name': name(node.this), 'type': node.kind.sql(dialect=dialect), 'nullable': True, 'comment': ''}
            if any(c['name'] == column['name'] for c in table['columns']):
                raise ValueError(f"字段重复：{column['name']}")
            table['columns'].append(column)
            for wrapper in node.constraints:
                kind = wrapper.kind
                if isinstance(kind, exp.PrimaryKeyColumnConstraint):
                    table['primary_key'].append(column['name'])
                    column['nullable'] = False
                elif isinstance(kind, exp.NotNullColumnConstraint):
                    column['nullable'] = False
                elif isinstance(kind, exp.UniqueColumnConstraint):
                    table['unique'].append([column['name']])
                elif isinstance(kind, exp.CommentColumnConstraint):
                    column['comment'] = kind.this.this
                elif isinstance(kind, exp.Reference):
                    reference(table, [column['name']], kind, line, trusted)
        elif isinstance(node, (exp.Constraint, exp.AddConstraint)):
            if (len(node.expressions) == 2 and isinstance(node.expressions[0], exp.PrimaryKeyColumnConstraint)
                    and type(node.expressions[1]).__name__ in {'ClusteredColumnConstraint','NonClusteredColumnConstraint'}):
                table['primary_key'] = fields(node.expressions[1].this)
                return
            for member in node.expressions:
                apply(table, member, line, trusted)
        elif isinstance(node, exp.PrimaryKey):
            table['primary_key'] = fields(node.expressions)
        elif isinstance(node, exp.UniqueColumnConstraint):
            table['unique'].append(fields(node.this.expressions))
        elif isinstance(node, exp.ForeignKey):
            reference(table, fields(node.expressions), node.args['reference'], line, trusted)
        elif isinstance(node, exp.CheckColumnConstraint):
            table.setdefault('checks', []).append(node.sql(dialect=dialect))
        elif isinstance(node, exp.IndexColumnConstraint):
            try:
                table['indexes'].append({'name': node.name, 'fields': fields(node.expressions)})
            except ValueError:
                warnings.append(f'第 {line} 行：表达式索引仅保留原定义')
                table['indexes'].append({'name': node.name, 'definition': node.sql(dialect=dialect)})
        else:
            raise ValueError(f'未支持的表定义：{type(node).__name__}')

    for line, sql in statements(stream, dialect):
        prefix = sql.split(None, 1)[0].upper()
        if prefix not in {'CREATE', 'ALTER', 'COMMENT', 'USE', 'SET', 'DROP', 'RENAME', 'TRUNCATE'}:
            continue
        try:
            if dialect == 'oracle' and (schema := re.fullmatch(r'ALTER\s+SESSION\s+SET\s+CURRENT_SCHEMA\s*=\s*("[^"]+"|\w+)',sql,re.I)):
                namespace = schema[1][1:-1] if schema[1].startswith('"') else schema[1].upper()
                continue
            trusted_constraint = True
            if dialect == 'tsql' and re.match(r'ALTER\s+TABLE\b',sql,re.I):
                match = re.match(r'(ALTER\s+TABLE\s+.+?)\s+WITH\s+(NO)?CHECK\s+(ADD\s+CONSTRAINT\b.*)',sql,re.I|re.S)
                if match:
                    sql = f'{match[1]} {match[3]}'
                    trusted_constraint = match[2] is None
                elif re.fullmatch(r'ALTER\s+TABLE\s+.+?\s+CHECK\s+CONSTRAINT\s+(?:\[[^\]]+\]|\w+)',sql,re.I|re.S):
                    warnings.append(f'第 {line} 行：CHECK CONSTRAINT 不改变离线验证状态')
                    continue
            search_path = re.fullmatch(r'SET\s+search_path\s*(?:TO|=)\s*("?\w+"?)(?:\s*,.*)?', sql, re.I | re.S)
            if search_path and dialect == 'postgres':
                namespace = search_path[1].strip('"')
                continue
            if prefix in {'DROP', 'RENAME', 'TRUNCATE'}:
                # A dump often includes DROP IF EXISTS immediately before the first CREATE.
                if re.match(r'DROP\s+(TABLE|VIEW)\s+IF\s+EXISTS\b', sql, re.I):
                    drop = sqlglot.parse_one(sql, read=dialect)
                    if table_id(drop.this) not in tables:
                        continue
                raise ValueError('包含无法安全还原的删除或重命名')
            if prefix == 'SET':
                continue
            node = sqlglot.parse_one(sql, read=dialect)
            kind = str(node.args.get('kind', '')).upper()
            if isinstance(node, exp.Use):
                if dialect == 'tsql':
                    catalog = name(node.this.this)
                else:
                    namespace = name(node.this.this)
            elif isinstance(node, exp.Create) and kind in {'TABLE', 'VIEW'}:
                schema = node.this
                target = schema.this if isinstance(schema, exp.Schema) else schema
                ident = table_id(target)
                if ident in tables:
                    raise ValueError(f'表重复定义：{ident}')
                table = {'id': ident, 'name': name(target.this), 'namespace': ident.rpartition('.')[0],
                         'kind': kind, 'line': line, 'columns': [], 'primary_key': [], 'unique': [],
                         'indexes': [], 'comment': ''}
                tables[ident] = table
                if kind == 'VIEW':
                    table['definition'] = node.expression.sql(dialect=dialect)
                    for output in node.expression.selects:
                        if output.output_name and not output.is_star:
                            table['columns'].append({'name': output.output_name, 'type': None, 'nullable': None, 'comment': ''})
                    warnings.append(f'第 {line} 行：视图 {ident} 的类型及星号展开未探测')
                elif not isinstance(schema, exp.Schema) or node.expression is not None:
                    raise ValueError('CREATE TABLE 必须包含可解析的字段定义')
                else:
                    for field in schema.expressions:
                        apply(table, field, line)
                    properties = node.args.get('properties')
                    if properties:
                        for prop in properties.find_all(exp.SchemaCommentProperty):
                            table['comment'] = prop.this.this
            elif isinstance(node, exp.Alter) and kind == 'TABLE':
                target = table_id(node.this)
                for action in node.args.get('actions', []):
                    apply(tables[target], action, trusted=trusted_constraint and not node.args.get('not_valid'), line=line)
            elif isinstance(node, exp.Create) and kind == 'INDEX':
                index = node.this
                table = tables[table_id(index.args['table'])]
                entry = {'name': index.name, 'definition': node.sql(dialect=dialect)}
                params = index.args.get('params')
                try:
                    entry['fields'] = fields(params.args.get('columns', []))
                except ValueError:
                    warnings.append(f'第 {line} 行：表达式索引仅保留原定义')
                table['indexes'].append(entry)
                if node.args.get('unique') and not params.args.get('where') and entry.get('fields'):
                    table['unique'].append(entry['fields'])
            elif isinstance(node, exp.Comment) and kind in {'TABLE', 'COLUMN'}:
                parts = node.this.parts
                target_parts = parts[:-1] if kind == 'COLUMN' else parts
                target = exp.Table(this=target_parts[-1], db=target_parts[-2] if len(target_parts)>1 else None,
                                   catalog=target_parts[-3] if len(target_parts)>2 else None)
                table = tables[table_id(target)]
                where = next(c for c in table['columns'] if c['name'] == name(parts[-1])) if kind == 'COLUMN' else table
                where['comment'] = node.expression.this if isinstance(node.expression, exp.Literal) else ''
            elif re.match(r'(CREATE\s+(?:OR\s+REPLACE\s+)?(?:TABLE|VIEW|INDEX|UNIQUE\s+INDEX)|ALTER\s+TABLE|COMMENT\s+ON\s+(?:TABLE|COLUMN))\b', sql, re.I):
                raise ValueError('未支持的结构语句')
            else:
                warnings.append(f'第 {line} 行：跳过其他对象或会话语句')
        except (sqlglot.errors.SqlglotError, ValueError, KeyError, AttributeError, StopIteration) as exc:
            raise ValueError(f'第 {line} 行：{exc}') from exc
    if not any(t['kind'] == 'TABLE' and t['columns'] for t in tables.values()):
        raise ValueError('没有解析出带字段的物理表')
    for table in tables.values():
        names = {c['name'] for c in table['columns']}
        for key in [table['primary_key'], *table['unique']]:
            if not set(key) <= names:
                raise ValueError(f"表 {table['id']} 的约束引用了不存在的字段")
        for col in table['columns']:
            if col['name'] in table['primary_key']:
                col['nullable'] = False
    relations = []
    for left, cols, right, target, line, trusted in foreign:
        target = target or tables.get(right, {}).get('primary_key', [])
        source_names = {c['name'] for c in tables[left]['columns']}
        target_names = {c['name'] for c in tables.get(right, {}).get('columns', [])}
        if not cols or len(cols) != len(target) or not set(cols) <= source_names or not set(target) <= target_names:
            warnings.append(f'第 {line} 行：未解析外键 {left} → {right}')
            continue
        left_unique = any(key and set(key) <= set(cols) for key in
                          [tables[left]['primary_key'], *tables[left]['unique']])
        right_unique = any(key and set(key) <= set(target) for key in
                           [tables[right]['primary_key'], *tables[right]['unique']])
        cardinality = ('1' if left_unique else 'N') + ':' + ('1' if right_unique else 'N')
        relations.append({'source': left, 'target': right, 'pairs': list(zip(cols, target)), 'condition': '',
                          'cardinality': cardinality if left_unique or right_unique else 'unknown',
                          'origin': 'ddl', 'status': 'active' if trusted else 'pending',
                          'evidence': f'DDL 第 {line} 行' + ('' if trusted else ' · NOT VALID')})
    return {'tables': tables, 'relations': relations, 'warnings': warnings}
