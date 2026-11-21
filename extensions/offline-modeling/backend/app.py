import json
import logging
import os
import secrets
import shutil
import sqlite3
import httpx
import sqlglot
from collections import Counter
from time import perf_counter
from contextlib import closing
from pathlib import Path
from uuid import uuid4

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from typing import Literal

from .ddl import parse_catalog
from .documents import blocks
from .model import discover
from .products import DIALECTS, PRODUCTS, resolve_product
from .query import generate_sql
from .relations import initial_relations, signature, validate

ROOT = Path(os.getenv('OFFLINE_DATA_DIR', './offline-data'))
ROOT.mkdir(parents=True, exist_ok=True)
MAX_BYTES = 64 * 1024 * 1024
app = FastAPI(title='Semora 离线建模', docs_url=None, redoc_url=None)
FRONTEND = Path(__file__).resolve().parents[1] / 'frontend'
logger = logging.getLogger('uvicorn.error')


def log_stage(event, ident, started, **counts):
    logger.info(json.dumps({'event': event, 'import_id': ident,
                            'duration_ms': round((perf_counter()-started)*1000),
                            **counts}, ensure_ascii=False))


@app.middleware('http')
async def access(request, call_next):
    # Bind the standalone service to loopback. For a shared deployment use a trusted gateway.
    if request.method not in {'GET', 'HEAD', 'OPTIONS'} and request.headers.get('sec-fetch-site') == 'cross-site':
        return JSONResponse({'detail':'请从离线建模页面提交操作'}, 403)
    password = os.getenv('OFFLINE_ACCESS_PASSWORD', '')
    if not password and os.getenv('OFFLINE_REQUIRE_ACCESS_PASSWORD', 'false') == 'true':
        return JSONResponse({'detail':'离线建模尚未配置访问密码'}, 503)
    if password:
        import base64
        actual = request.headers.get('Authorization', '').encode()
        expected = b'Basic ' + base64.b64encode(('offline:' + password).encode())
        if not secrets.compare_digest(actual, expected):
            return JSONResponse({'detail':'请登录离线建模'}, 401, {'WWW-Authenticate':'Basic realm="Offline Modeling"'})
    return await call_next(request)


def connect():
    db = sqlite3.connect(ROOT / 'offline.db', timeout=15)
    db.row_factory = sqlite3.Row
    db.execute('''CREATE TABLE IF NOT EXISTS imports (
        id TEXT PRIMARY KEY, name TEXT NOT NULL, status TEXT NOT NULL, config TEXT NOT NULL,
        payload TEXT, error TEXT, analysis TEXT, revision INTEGER NOT NULL DEFAULT 0
    )''')
    db.execute('''CREATE TABLE IF NOT EXISTS analysis_checkpoints (
        import_id TEXT NOT NULL, work_key TEXT NOT NULL, payload TEXT NOT NULL,
        PRIMARY KEY (import_id, work_key)
    )''')
    return db


def get(db, ident):
    row = db.execute('SELECT * FROM imports WHERE id=?', (ident,)).fetchone()
    if row is None:
        raise HTTPException(404, '离线数据源不存在')
    return row


def update(db, ident, sql, args):
    with db:
        return db.execute(f'UPDATE imports SET {sql} WHERE id=?', (*args, ident)).rowcount


def parse_import(ident):
    started = perf_counter()
    stage = 'ddl'
    with closing(connect()) as db:
        cfg = json.loads(get(db, ident)['config'])
        directory = ROOT / ident
        try:
            with (directory / 'ddl').open(encoding='utf-8-sig') as stream:
                catalog = parse_catalog(stream, cfg['dialect'], cfg['namespace'])
            log_stage('offline.ddl.completed', ident, started, dialect=cfg['dialect'],
                      bytes=(directory/'ddl').stat().st_size, tables=len(catalog['tables']),
                      columns=sum(len(t['columns']) for t in catalog['tables'].values()),
                      declared=len(catalog['relations']), warnings=len(catalog['warnings']))
            stage = 'documents'
            records, chars = [], 0
            for i, file in enumerate(cfg['files']):
                file_started, before = perf_counter(), len(records)
                for block in blocks(directory / f'doc-{i}', file):
                    chars += len(block['text'])
                    if chars > 10_000_000:
                        raise ValueError('本次导入的有效文档文本超过处理上限')
                    records.append({**block,'file_index':i+1})
                log_stage('offline.document.completed', ident, file_started, file_index=i+1,
                          format=Path(file).suffix.lower(), blocks=len(records)-before)
            stage, binding_started = 'binding', perf_counter()
            relations = initial_relations(catalog, cfg['dialect'])
            payload = {'tables':catalog['tables'], 'relations':relations,'records':records,'warnings':catalog['warnings'],
                       'ddl_relations':catalog['relations']}
            status = 'ready' if cfg.get('auto_confirm') else 'preview'
            update(db,ident,'status=?,payload=?,error=?',(status,json.dumps(payload,ensure_ascii=False),None))
            log_stage('offline.binding.completed', ident, binding_started, blocks=len(records),
                      origins=dict(Counter(r['origin'] for r in relations.values())),
                      statuses=dict(Counter(r['status'] for r in relations.values())), warnings=len(catalog['warnings']))
            log_stage('offline.import.completed', ident, started)
        except Exception as exc:
            message = str(exc)[:1200] if isinstance(exc, ValueError) else f'文件解析失败：{type(exc).__name__}'
            update(db,ident,'status=?,error=?',('failed',message))
            log_stage('offline.import.failed', ident, started, stage=stage, error_type=type(exc).__name__)


@app.get('/')
def home():
    return FileResponse(FRONTEND / 'index.html')


app.mount('/assets', StaticFiles(directory=FRONTEND), name='assets')


@app.get('/v1/products')
def products():
    return [{'id':key, 'label':label, 'modes':[{'id':mode,'label':DIALECTS[mode]} for mode in modes]}
            for key, (label,modes) in PRODUCTS.items()]


@app.get('/v1/imports')
def list_imports(summary: bool = False):
    with closing(connect()) as db:
        sql = """SELECT id,name,status,error,analysis,
                    json_extract(config,'$.product') AS product,
                    json_extract(config,'$.product_label') AS product_label,
                    json_extract(config,'$.version') AS version,
                    json_extract(config,'$.ddl') AS ddl"""
        if summary:
            sql += """, (SELECT count(*) FROM json_each(payload,'$.tables')) AS object_count,
                        (SELECT count(*) FROM json_each(payload,'$.relations')
                            WHERE json_extract(value,'$.status')='active') AS relation_count"""
        return [dict(row) for row in db.execute(sql + ' FROM imports ORDER BY rowid DESC')]


@app.post('/v1/imports', status_code=202)
def create_import(tasks: BackgroundTasks, name: str = Form(), dialect: str = Form(''),
                  product: str = Form(''), compatibility_mode: str = Form(''), version: str = Form(''), namespace: str = Form(''), ddl: UploadFile = File(),
                  documents: list[UploadFile] = File(default=[]), auto_confirm: bool = Form(False)):
    try:
        product, dialect = resolve_product(product, compatibility_mode, dialect)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    name, version, namespace = name.strip(), version.strip(), namespace.strip()
    if not name or len(name) > 120 or len(version) > 80 or len(namespace) > 128:
        raise HTTPException(422, '请检查数据源名称、版本与默认命名空间')
    files = [Path(f.filename or '').name for f in documents]
    if Path(ddl.filename or '').suffix.lower() not in {'.sql','.ddl'} or len(files) > 20 or any(
            Path(f).suffix.lower() not in {'.sql','.txt','.xlsx','.docx'} for f in files):
        raise HTTPException(422, '请上传一个 SQL/DDL 文件和最多 20 份支持的文档')
    ident = uuid4().hex
    directory = ROOT / ident
    directory.mkdir()
    total = 0
    try:
        for target, upload in [('ddl',ddl), *((f'doc-{i}',file) for i,file in enumerate(documents))]:
            with (directory / target).open('wb') as out:
                while chunk := upload.file.read(65536):
                    total += len(chunk)
                    if total > MAX_BYTES:
                        raise HTTPException(413, '本次上传总大小超过 64 MiB')
                    out.write(chunk)
        config = {'product':product,'product_label':PRODUCTS[product][0],'dialect':dialect,'version':version,'namespace':namespace,'files':files,'ddl':Path(ddl.filename or '').name,
                  'auto_confirm':auto_confirm}
        with closing(connect()) as db, db:
            db.execute('INSERT INTO imports(id,name,status,config) VALUES(?,?,?,?)',
                       (ident,name,'importing',json.dumps(config,ensure_ascii=False)))
    except Exception:
        shutil.rmtree(directory)
        raise
    tasks.add_task(parse_import, ident)
    return {'id':ident}


@app.get('/v1/imports/{ident}')
def details(ident: str):
    with closing(connect()) as db:
        row = dict(get(db, ident))
    payload = json.loads(row.pop('payload') or '{}')
    payload.pop('records', None)
    payload.pop('ddl_relations', None)
    payload.pop('analysis_completed', None)
    payload.pop('document_directory', None)
    return {**row, 'config':json.loads(row['config']), **payload}


def import_catalog(ident):
    with closing(connect()) as db:
        row = get(db, ident)
        return json.loads(row['payload'] or '{}')


def table_summary(table):
    return {'id':table['id'], 'name':table['name'], 'schema':table['namespace'], 'kind':table['kind'],
            'description':table['comment'], 'column_count':len(table['columns']),
            'primary_keys':[{'columns':table['primary_key']}] if table['primary_key'] else []}


@app.get('/v1/imports/{ident}/tables')
def list_tables(ident: str, query: str = '', page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    needle = query.strip().casefold()
    tables = [table for table in import_catalog(ident).get('tables', {}).values()
              if needle in (table['id'] + ' ' + table['comment']).casefold()]
    start = (page-1)*page_size
    return {'items':[table_summary(table) for table in tables[start:start+page_size]], 'total':len(tables)}


@app.get('/v1/imports/{ident}/tables/detail')
def table_details(ident: str, table_id: str):
    catalog = import_catalog(ident)
    table = catalog.get('tables', {}).get(table_id)
    if table is None:
        raise HTTPException(404, '表不存在')
    return {**table_summary(table), 'columns':table['columns'], 'indexes':table['indexes'],
            'unique_constraints':[{'columns':fields} for fields in table['unique']],
            'foreign_keys':[{'target_table':r['target'], 'column_pairs':r['pairs']}
                            for r in catalog.get('ddl_relations', list(catalog.get('relations', {}).values()))
                            if r['origin']=='ddl' and r['source']==table_id]}


@app.get('/v1/imports/{ident}/relationships')
def list_relationships(ident: str, status: Literal['all','accepted','pending','disabled'] = 'all',
                       page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    counts = dict.fromkeys(('all','accepted','pending','disabled'),0)
    items = []
    for key, relation in import_catalog(ident).get('relations', {}).items():
        state = {'active':'accepted','rejected':'disabled'}.get(relation['status'],relation['status'])
        counts['all'] += 1
        counts[state] += 1
        if status == 'all' or status == state:
            left, right = zip(*relation['pairs'])
            items.append({'id':key, 'source_table_id':relation['source'], 'target_table_id':relation['target'],
                          'source_columns':left, 'target_columns':right,
                          'key_pairs':[{'source':a,'target':b} for a,b in relation['pairs']],
                          'status':state, 'cardinality':relation['cardinality'], 'condition':relation['condition'],
                          'decision_reason':relation.get('review_reason') if state == 'pending' else None})
    start = (page-1)*page_size
    return {'items':items[start:start+page_size], 'total':counts[status], 'counts':counts}


class SQLRequest(BaseModel):
    question: str = Field(min_length=1,max_length=1500)


@app.post('/v1/imports/{ident}/sql')
async def draft_sql(ident: str, body: SQLRequest):
    with closing(connect()) as db:
        row=get(db,ident)
        if row['status']!='ready':
            raise HTTPException(409,'请先完成数据源导入')
        config=json.loads(row['config'])
        catalog=json.loads(row['payload'])
    try:
        sql=await generate_sql(body.question,config,catalog)
    except (TimeoutError,httpx.TimeoutException) as exc:
        raise HTTPException(504,'生成 SQL 超时，请重试') from exc
    except (httpx.HTTPError, KeyError, IndexError, TypeError) as exc:
        raise HTTPException(502,'模型未返回可用的 SQL，请重试') from exc
    except (ValueError,sqlglot.errors.SqlglotError) as exc:
        raise HTTPException(422,str(exc) if isinstance(exc,ValueError) else '生成的 SQL 无法解析') from exc
    return {'sql':sql}


@app.delete('/v1/imports/{ident}', status_code=204)
def delete_import(ident: str):
    with closing(connect()) as db:
        directory = ROOT / get(db, ident)['id']
        staged = ROOT / f'.deleted-{directory.name}'
        moved = False
        try:
            with db:
                db.execute('BEGIN IMMEDIATE')
                row = get(db, ident)
                if row['status'] == 'importing' or row['analysis'] in {'运行中','取消中'}:
                    raise HTTPException(409, '请等待导入完成，或先取消关联分析')
                if directory.exists():
                    directory.rename(staged)
                    moved = True
                db.execute('DELETE FROM analysis_checkpoints WHERE import_id=?', (ident,))
                db.execute('DELETE FROM imports WHERE id=?', (ident,))
        except Exception:
            if moved:
                staged.rename(directory)
            raise
    if staged.exists():
        try:
            shutil.rmtree(staged)
        except OSError:
            logger.warning('离线导入 %s 的文件将在下次启动时清理', ident)


@app.post('/v1/imports/{ident}/confirm')
def confirm(ident: str):
    with closing(connect()) as db, db:
        db.execute('BEGIN IMMEDIATE')
        row = get(db, ident)
        if row['status'] not in {'preview','ready'}:
            raise HTTPException(409, '请等待导入预览完成')
        db.execute("UPDATE imports SET status='ready' WHERE id=?", (ident,))
    return {'id':ident,'status':'ready'}


@app.post('/v1/imports/{ident}/retry', status_code=202)
def retry_import(ident: str, tasks: BackgroundTasks):
    with closing(connect()) as db, db:
        if not db.execute("UPDATE imports SET status='importing',error=NULL WHERE id=? AND status='failed'", (ident,)).rowcount:
            get(db, ident)
            raise HTTPException(409, '仅失败的导入可用原文件重试')
    tasks.add_task(parse_import, ident)
    return {'id':ident}


class RelationInput(BaseModel):
    source: str
    target: str
    pairs: list[tuple[str,str]] = Field(min_length=1,max_length=16)
    condition: str = Field(default='',max_length=2000)
    cardinality: Literal['1:1','N:1','1:N','N:N','unknown'] = 'unknown'
    status: Literal['active','pending','disabled','rejected'] = 'active'


@app.post('/v1/imports/{ident}/relations')
@app.put('/v1/imports/{ident}/relations/{relation_id}')
def save_relation(ident: str, body: RelationInput, relation_id: str | None = None):
    with closing(connect()) as db, db:
        db.execute('BEGIN IMMEDIATE')
        row = get(db, ident)
        if row['status'] != 'ready':
            raise HTTPException(409, '请先确认导入')
        payload = json.loads(row['payload'])
        values = payload['relations']
        if relation_id is not None and relation_id not in values:
            raise HTTPException(404, '关联不存在')
        try:
            relation = validate(body.model_dump(),payload['tables'],json.loads(row['config'])['dialect'])
        except ValueError as exc:
            raise HTTPException(422,str(exc)) from exc
        previous = values.get(relation_id)
        if relation['status'] == 'active' and relation['cardinality'] == 'unknown' and previous and previous.get('status') in {'pending','rejected'}:
            raise HTTPException(422, '请先编辑关系并明确基数，再确认生效')
        key = signature(relation)
        if key != relation_id and key in values:
            raise HTTPException(409,'已有相同字段映射和条件')
        if previous:
            values.pop(relation_id)
            history = [*previous.get('history', []), {k:v for k,v in previous.items() if k != 'history'}]
        else:
            history = []
        values[key] = {**relation, 'origin':'manual', 'evidence':previous['evidence'] if previous else '人工配置',
                       'history':history}
        db.execute('UPDATE imports SET payload=?,revision=revision+1 WHERE id=?',
                   (json.dumps(payload,ensure_ascii=False),ident))
    return {'id':key, **values[key]}


def available_analysis(row, interrupted):
    if row['status'] != 'ready' or not row['payload']:
        return interrupted
    payload = json.loads(row['payload'])
    relations = payload.get('relations', {})
    if not payload.get('tables') and not relations:
        return interrupted
    # Older imports have no completion marker. Published model relations also prove a completed run.
    if payload.get('analysis_completed') or any(
        item.get('origin') == 'model'
        for relation in relations.values()
        for item in (relation, *relation.get('history', []))
    ):
        return '完成'
    return None


def analyze(ident):
    started = perf_counter()
    with closing(connect()) as db:
        row = get(db, ident)
        payload = json.loads(row['payload'])
        try:
            from .model import AnalysisCancelled, analysis_progress
            def cancelled():
                return get(db, ident)['analysis'] == '取消中'

            def checkpoint(key, result=None):
                if result is None:
                    saved = db.execute('SELECT payload FROM analysis_checkpoints WHERE import_id=? AND work_key=?',
                                       (ident, key)).fetchone()
                    return json.loads(saved['payload']) if saved else None
                with db:
                    db.execute('INSERT OR REPLACE INTO analysis_checkpoints VALUES (?, ?, ?)',
                               (ident, key, json.dumps(result, ensure_ascii=False)))

            if cancelled():
                raise AnalysisCancelled()
            previous_pending = {key: relation for key, relation in payload['relations'].items()
                                if relation['origin'] == 'model' and relation['status'] == 'pending'}
            known = {key: relation for key, relation in payload['relations'].items() if key not in previous_pending}
            document_summary, document_directory = None, None

            def report(event, **counts):
                nonlocal document_summary, document_directory
                if event == 'offline.analysis.documents_completed':
                    document_summary = counts['summary']
                    document_directory = counts['directory']
                    counts = {key:value for key,value in document_summary.items() if key!='gaps'}
                log_stage(event, ident, started, **counts)

            with analysis_progress(report, cancelled, checkpoint):
                proposals = dict(discover(payload['tables'], payload['records'], json.loads(row['config'])['dialect'],
                                          known, declared=payload.get('ddl_relations')))
            with db:
                db.execute('BEGIN IMMEDIATE')
                current = get(db,ident)
                if current['analysis'] == '取消中':
                    raise AnalysisCancelled()
                latest = json.loads(current['payload'])
                suppressed = {signature(h) for r in latest['relations'].values() for h in r.get('history', [])}
                for key, relation in previous_pending.items():
                    if latest['relations'].get(key) == relation:
                        latest['relations'].pop(key)
                for key, r in proposals.items():
                    if key not in suppressed:
                        latest['relations'].setdefault(key,r)
                latest['analysis_completed'] = True
                if document_summary is not None:
                    latest['document_analysis'] = document_summary
                    latest['document_directory'] = document_directory
                db.execute('UPDATE imports SET payload=?,analysis=?,revision=revision+1 WHERE id=?',
                           (json.dumps(latest,ensure_ascii=False),'完成',ident))
                db.execute('DELETE FROM analysis_checkpoints WHERE import_id=?', (ident,))
            log_stage('offline.analysis.completed', ident, started, proposals=len(proposals))
        except AnalysisCancelled:
            update(db,ident,'analysis=?',(available_analysis(get(db, ident),'已取消'),))
            log_stage('offline.analysis.cancelled', ident, started)
        except Exception as exc:
            message = str(exc)[:500] if isinstance(exc,ValueError) else '分析失败，请重试'
            with db:
                db.execute('BEGIN IMMEDIATE')
                current = get(db,ident)
                if current['analysis'] == '取消中':
                    message = available_analysis(current,'已取消')
                db.execute('UPDATE imports SET analysis=? WHERE id=?',(message,ident))
            log_stage('offline.analysis.failed', ident, started, error_type=type(exc).__name__)


@app.post('/v1/imports/{ident}/analysis', status_code=202)
def start_analysis(ident: str, tasks: BackgroundTasks):
    if not os.getenv('OFFLINE_MODEL_URL') or not os.getenv('OFFLINE_MODEL_NAME'):
        raise HTTPException(422, '请先配置离线扩展的模型地址和模型名称')
    with closing(connect()) as db, db:
        db.execute('BEGIN IMMEDIATE')
        row = get(db,ident)
        if row['status'] != 'ready' or row['analysis'] in {'运行中','取消中'}:
            raise HTTPException(409, '请确认导入并等待现有分析完成')
        db.execute("UPDATE imports SET analysis='运行中' WHERE id=?", (ident,))
    tasks.add_task(analyze,ident)
    return {'id':ident}


@app.post('/v1/imports/{ident}/analysis/cancel', status_code=202)
def cancel_analysis(ident: str):
    with closing(connect()) as db, db:
        if not db.execute("UPDATE imports SET analysis='取消中' WHERE id=? AND analysis='运行中'", (ident,)).rowcount:
            get(db, ident)
            raise HTTPException(409, '当前没有可取消的关联分析')
    return {'id':ident}


@app.on_event('startup')
def recover():
    # This extension is deployed as one worker. Re-run interrupted imports from sealed files.
    with closing(connect()) as db, db:
        for staged in ROOT.glob('.deleted-*'):
            ident = staged.name.removeprefix('.deleted-')
            if db.execute('SELECT 1 FROM imports WHERE id=?', (ident,)).fetchone():
                staged.rename(ROOT / ident)
            else:
                shutil.rmtree(staged)
        pending = [row['id'] for row in db.execute("SELECT id FROM imports WHERE status='importing'")]
        interrupted = db.execute("""SELECT id,status,payload,analysis FROM imports
                          WHERE analysis IN ('运行中','分析中断，可重试','取消中','已取消')""").fetchall()
        for row in interrupted:
            fallback = '已取消' if row['analysis'] in {'取消中','已取消'} else '分析中断，可重试'
            db.execute('UPDATE imports SET analysis=? WHERE id=?',(available_analysis(row,fallback),row['id']))
    for ident in pending:
        parse_import(ident)
