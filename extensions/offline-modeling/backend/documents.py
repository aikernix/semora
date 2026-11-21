"""Document extraction is independent of the DDL catalog."""
import re
from pathlib import Path
from zipfile import ZipFile

import sqlglot
from sqlglot import exp

MAX_BLOCKS = 20000
MAX_BLOCK_CHARS = 16000
MAX_EXPANDED = 96 * 1024 * 1024
WORD = r'[^\W\d][\w$]*'
REF = rf'{WORD}(?:\.{WORD})+'
EQUALITY = re.compile(rf'({REF})\s*=\s*({REF})', re.UNICODE)
CARDINALITY = {'一对一':'1:1', '一对多':'1:N', '多对一':'N:1', '多对多':'N:N'}


def blocks(path: Path, filename: str):
    suffix = Path(filename).suffix.lower()
    if suffix in {'.docx', '.xlsx'}:
        with ZipFile(path) as bundle:
            if sum(x.file_size for x in bundle.infolist()) > MAX_EXPANDED:
                raise ValueError(f'{filename} 解压后超过大小限制')
    count = 0
    for locator, value, layout in _regions(path, suffix):
        text = value.strip()
        if not text:
            continue
        # Preserve the original region identity for oversized records.
        for start in range(0, len(text), MAX_BLOCK_CHARS):
            count += 1
            if count > MAX_BLOCKS:
                raise ValueError(f'{filename} 文档区域数超限')
            yield {'file': filename, 'locator': locator, 'part': start // MAX_BLOCK_CHARS + 1,
                   'text': text[start:start+MAX_BLOCK_CHARS],
                   **(layout if len(text) <= MAX_BLOCK_CHARS else {'section':layout.get('section','')})}


def _regions(path, suffix):
    if suffix == '.xlsx':
        from openpyxl import load_workbook
        with path.open('rb') as stream:
            book = load_workbook(stream, read_only=True, data_only=True, keep_links=False)
            try:
                for sheet in book:
                    for i, row in enumerate(sheet.iter_rows(values_only=True), 1):
                        values = ['' if v is None else str(v) for v in row]
                        yield f'{sheet.title}!第 {i} 行', ' | '.join(values).strip(' |'), {
                            'section':sheet.title,'cells':values}
            finally:
                book.close()
    elif suffix == '.docx':
        from docx import Document
        from docx.table import Table
        from docx.text.paragraph import Paragraph
        with path.open('rb') as stream:
            doc = Document(stream)
            title = ''
            for i, item in enumerate(doc.element.body, 1):
                if item.tag.endswith('}p'):
                    paragraph = Paragraph(item, doc)
                    if (paragraph.style.name or '').casefold().startswith(('heading','title','标题')):
                        title = paragraph.text
                    yield f'段落 {i}', paragraph.text, {'section':title}
                elif item.tag.endswith('}tbl'):
                    for j, row in enumerate(Table(item, doc).rows, 1):
                        cells = [c.text for c in row.cells]
                        yield f'表格 {i} 第 {j} 行', ' | '.join(cells).strip(' |'), {
                            'section':f'{title} · 表格 {i}','cells':cells}
    elif suffix in {'.txt', '.sql'}:
        with path.open(encoding='utf-8-sig') as stream:
            start, lines, size = 1, [], 0
            for i, line in enumerate(stream, 1):
                if not line.strip() or size + len(line) > MAX_BLOCK_CHARS:
                    if lines:
                        yield f'第 {start}–{i-1} 行', ''.join(lines), {}
                    lines, start, size = [], i, 0
                if line.strip():
                    if not lines:
                        start = i
                    lines.append(line)
                    size += len(line)
            if lines:
                yield f'第 {start}–{i} 行', ''.join(lines), {}
    else:
        raise ValueError(f'不支持的资料格式：{suffix}')


def declared_mappings(text):
    """Recognize explicit conjunctions; never extract half of a condition."""
    terms = re.split(r"\s+AND\s+|\s*且\s*", text.strip().rstrip(';'), flags=re.I)
    pairs, guards = [], []
    literal = r"(?:'[^']*'|-?\d+(?:\.\d+)?|true|false)"
    for term in terms:
        if match := EQUALITY.fullmatch(term.strip()):
            pairs.append(match.groups())
        elif match := re.fullmatch(rf'({REF})\s*=\s*({literal})', term.strip(), re.I):
            guards.append(match.groups())
        else:
            return None
    if not pairs:
        return None
    return pairs, guards


def historical_mappings(sql, dialect):
    """Retain complete physical ON clauses, including local constant guards."""
    try:
        parsed = sqlglot.parse(sql, read=dialect)
    except sqlglot.errors.SqlglotError:
        return
    for tree in parsed:
        if not isinstance(tree, exp.Select) or tree.find(exp.CTE) or tree.find(exp.Subquery):
            continue
        root = tree.args.get('from')
        if root is None or not isinstance(root.this, exp.Table):
            continue
        aliases = {root.this.alias_or_name: '.'.join(x.name for x in root.this.parts)}
        for join in tree.args.get('joins', []):
            if not isinstance(join.this, exp.Table):
                continue
            alias = join.this.alias_or_name
            aliases[alias] = '.'.join(x.name for x in join.this.parts)
            on = join.args.get('on')
            if on is None:
                continue
            terms = list(on.flatten()) if isinstance(on, exp.And) else [on]
            mapping, guards = [], []
            for term in terms:
                if not isinstance(term, exp.EQ):
                    break
                a, b = term.this, term.expression
                if all(isinstance(x, exp.Column) and x.table in aliases for x in (a,b)):
                    if alias not in {a.table,b.table} or a.table == b.table or aliases[a.table] == aliases[b.table]:
                        break
                    ordered = (b,a) if a.table == alias else (a,b)
                    mapping.append(tuple(f'{aliases[x.table]}.{x.name}' for x in ordered))
                else:
                    if isinstance(b, exp.Column):
                        a,b = b,a
                    if not isinstance(a, exp.Column) or a.table not in aliases or not isinstance(b,(exp.Literal,exp.Boolean,exp.Neg)):
                        break
                    guards.append((f'{aliases[a.table]}.{a.name}',b.sql(dialect=dialect)))
            else:
                if mapping:
                    # Preserve endpoint equality restrictions from WHERE as well
                    # as ON, including legacy composite JOIN keys.
                    where = tree.args.get('where')
                    if where:
                        conditions = list(where.this.flatten()) if isinstance(where.this, exp.And) else [where.this]
                        endpoints = {ref.rpartition('.')[0] for ref in mapping[0]}
                        for term in conditions:
                            if not isinstance(term, exp.EQ):
                                continue
                            a,b = term.this,term.expression
                            if all(isinstance(x,exp.Column) and x.table in aliases for x in (a,b)):
                                if a.table != b.table and {aliases[a.table],aliases[b.table]} == endpoints:
                                    ordered = (b,a) if a.table == alias else (a,b)
                                    pair = tuple(f'{aliases[x.table]}.{x.name}' for x in ordered)
                                    if pair not in mapping:
                                        mapping.append(pair)
                                continue
                    yield mapping, list(dict.fromkeys(guards))
