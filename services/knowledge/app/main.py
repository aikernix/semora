from __future__ import annotations

from contextlib import asynccontextmanager
from hashlib import sha256
import json
import mimetypes
import os
from pathlib import Path
from typing import Any
from urllib.parse import quote
from uuid import UUID, uuid4

from fastapi import FastAPI, File, Header, HTTPException, Query, Response, UploadFile
from psycopg import connect
from psycopg.rows import dict_row
from pydantic import BaseModel, Field
from semora_telemetry import configure_telemetry

from .embeddings import embedding_provider
from .object_store import ObjectStoreError, object_store
from .ocr import ocr_provider
from .parsers import IMAGE_SUFFIXES, chunk_blocks, parse_document


DATABASE_URL = os.getenv("KNOWLEDGE_DATABASE_URL", "postgresql://semora:semora@localhost:5432/semora_business")
DATA_DIR = Path(os.getenv("KNOWLEDGE_DATA_DIR", "/data/uploads"))
SEED_DIR = Path(os.getenv("KNOWLEDGE_SEED_DIR", "/app/seed"))
INTERNAL_TOKEN = os.getenv("INTERNAL_TOKEN", "semora-dev-internal")
MAX_UPLOAD_BYTES = int(os.getenv("KNOWLEDGE_UPLOAD_MAX_BYTES", str(25 * 1024 * 1024)))
ALLOWED_SUFFIXES = {
    ".pdf", ".docx", ".pptx", ".xlsx", ".xlsm", ".txt", ".md", ".csv", ".html", ".htm",
    *IMAGE_SUFFIXES,
}
DEMO_KB_ID = "11111111-1111-4111-8111-111111111111"


def db(*, autocommit: bool = True):
    return connect(DATABASE_URL, autocommit=autocommit, row_factory=dict_row)


def vector_literal(vector: list[float]) -> str:
    return "[" + ",".join(f"{value:.8f}" for value in vector) + "]"


def require_internal_token(x_semora_internal_token: str | None) -> None:
    if os.getenv("REQUIRE_INTERNAL_TOKEN", "true").lower() == "true" and x_semora_internal_token != INTERNAL_TOKEN:
        raise HTTPException(401, "invalid internal token")


def initialize_database() -> None:
    object_store.ensure_ready()
    ocr_provider.ensure_ready()
    dimensions = embedding_provider.dimensions
    if dimensions < 64 or dimensions > 4096:
        raise RuntimeError("EMBEDDING_DIMENSIONS must be between 64 and 4096")
    statements = [
        "CREATE EXTENSION IF NOT EXISTS vector",
        "CREATE EXTENSION IF NOT EXISTS pg_trgm",
        "CREATE SCHEMA IF NOT EXISTS knowledge_core",
        """CREATE TABLE IF NOT EXISTS knowledge_core.knowledge_bases (
            id UUID PRIMARY KEY, name TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(), updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )""",
        """CREATE TABLE IF NOT EXISTS knowledge_core.documents (
            id UUID PRIMARY KEY, knowledge_base_id UUID NOT NULL REFERENCES knowledge_core.knowledge_bases(id),
            title TEXT NOT NULL, file_name TEXT NOT NULL, file_type TEXT NOT NULL,
            source_path TEXT NOT NULL, content_hash CHAR(64) NOT NULL,
            parse_status TEXT NOT NULL DEFAULT 'pending', enable_status TEXT NOT NULL DEFAULT 'disabled',
            chunk_count INTEGER NOT NULL DEFAULT 0, error_message TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(), processed_at TIMESTAMPTZ
        )""",
        f"""CREATE TABLE IF NOT EXISTS knowledge_core.chunks (
            id UUID PRIMARY KEY, knowledge_base_id UUID NOT NULL REFERENCES knowledge_core.knowledge_bases(id),
            document_id UUID NOT NULL REFERENCES knowledge_core.documents(id) ON DELETE CASCADE,
            chunk_index INTEGER NOT NULL, locator TEXT NOT NULL, title TEXT NOT NULL,
            content TEXT NOT NULL, embedding vector({dimensions}) NOT NULL, metadata JSONB NOT NULL DEFAULT '{{}}',
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(), UNIQUE(document_id, chunk_index)
        )""",
        """CREATE TABLE IF NOT EXISTS knowledge_core.ingestion_jobs (
            id UUID PRIMARY KEY, document_id UUID NOT NULL UNIQUE
                REFERENCES knowledge_core.documents(id) ON DELETE CASCADE,
            state TEXT NOT NULL DEFAULT 'queued'
                CHECK (state IN ('queued','processing','retry_wait','completed','failed')),
            attempts INTEGER NOT NULL DEFAULT 0, max_attempts INTEGER NOT NULL DEFAULT 3,
            available_at TIMESTAMPTZ NOT NULL DEFAULT now(), locked_by TEXT,
            lease_expires_at TIMESTAMPTZ, last_error TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(), started_at TIMESTAMPTZ,
            finished_at TIMESTAMPTZ, updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )""",
        "CREATE INDEX IF NOT EXISTS chunks_kb_idx ON knowledge_core.chunks(knowledge_base_id)",
        "CREATE INDEX IF NOT EXISTS chunks_content_trgm_idx ON knowledge_core.chunks USING gin(content gin_trgm_ops)",
        "CREATE INDEX IF NOT EXISTS chunks_embedding_hnsw_idx ON knowledge_core.chunks USING hnsw (embedding vector_cosine_ops)",
        "CREATE INDEX IF NOT EXISTS ingestion_jobs_claim_idx ON knowledge_core.ingestion_jobs(state,available_at,created_at)",
    ]
    with db() as connection, connection.cursor() as cursor:
        for statement in statements:
            cursor.execute(statement)
        cursor.execute(
            """INSERT INTO knowledge_core.knowledge_bases(id,name,description)
            VALUES(%s,'零售信贷制度与口径','Semora Knowledge Core 内置零售信贷演示知识库')
            ON CONFLICT(id) DO NOTHING""",
            (DEMO_KB_ID,),
        )


def create_ingestion_job(cursor: Any, document_id: str) -> None:
    cursor.execute(
        """INSERT INTO knowledge_core.ingestion_jobs(id,document_id)
        VALUES(%s,%s) ON CONFLICT(document_id) DO NOTHING""",
        (str(uuid4()), document_id),
    )


def ingest_document(document_id: str, *, job_id: str | None = None, worker_id: str | None = None) -> int:
    """Parse and atomically replace a document's searchable index.

    Job lifecycle and retry decisions are owned by the durable worker. A failed
    transaction cannot expose partially written chunks to search.
    """
    with db() as connection, connection.cursor() as cursor:
        cursor.execute("SELECT * FROM knowledge_core.documents WHERE id=%s", (document_id,))
        document = cursor.fetchone()
    if not document:
        raise ValueError("document not found")
    with object_store.materialize(document["source_path"]) as source_path:
        blocks = parse_document(source_path)
    chunks = chunk_blocks(blocks)
    if not chunks:
        raise ValueError("文档未解析出有效文本")
    vectors = embedding_provider.embed([chunk.text for chunk in chunks])
    if len(vectors) != len(chunks):
        raise ValueError("embedding count does not match chunk count")
    with db(autocommit=False) as connection, connection.cursor() as cursor:
        if job_id is not None or worker_id is not None:
            if not job_id or not worker_id:
                raise ValueError("job_id and worker_id must be provided together")
            cursor.execute(
                """SELECT state,locked_by,lease_expires_at > now() AS lease_valid
                FROM knowledge_core.ingestion_jobs WHERE id=%s FOR UPDATE""",
                (job_id,),
            )
            lease = cursor.fetchone()
            if not lease or lease["state"] != "processing" or lease["locked_by"] != worker_id or not lease["lease_valid"]:
                raise RuntimeError("ingestion job lease was lost before index commit")
        cursor.execute("DELETE FROM knowledge_core.chunks WHERE document_id=%s", (document_id,))
        for index, (chunk, vector) in enumerate(zip(chunks, vectors, strict=True)):
            cursor.execute(
                """INSERT INTO knowledge_core.chunks(
                id,knowledge_base_id,document_id,chunk_index,locator,title,content,embedding,metadata)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s::vector,%s::jsonb)""",
                (
                    str(uuid4()), document["knowledge_base_id"], document_id, index, chunk.locator,
                    document["title"], chunk.text, vector_literal(vector),
                    json.dumps({
                        "file_name": document["file_name"],
                        "revision": document["content_hash"][:12],
                        "extraction": "ocr" if "OCR" in chunk.locator else "native",
                    }),
                ),
            )
        cursor.execute(
            """UPDATE knowledge_core.documents SET parse_status='completed',enable_status='enabled',
            chunk_count=%s,error_message=NULL,processed_at=now() WHERE id=%s""",
            (len(chunks), document_id),
        )
    return len(chunks)


def seed_documents() -> None:
    object_store.ensure_ready()
    if not SEED_DIR.exists():
        return
    with db() as connection, connection.cursor() as cursor:
        cursor.execute("SELECT count(*) AS count FROM knowledge_core.documents WHERE knowledge_base_id=%s", (DEMO_KB_ID,))
        if cursor.fetchone()["count"]:
            return
    for source in sorted(SEED_DIR.glob("*.md")):
        document_id = str(uuid4())
        content_hash = sha256(source.read_bytes()).hexdigest()
        source_uri = object_store.put_file(document_id, source, "text/markdown")
        try:
            with db(autocommit=False) as connection, connection.cursor() as cursor:
                cursor.execute(
                    """INSERT INTO knowledge_core.documents(
                    id,knowledge_base_id,title,file_name,file_type,source_path,content_hash)
                    VALUES(%s,%s,%s,%s,%s,%s,%s)""",
                    (document_id, DEMO_KB_ID, source.stem, source.name, "md", source_uri, content_hash),
                )
                create_ingestion_job(cursor, document_id)
        except Exception:
            object_store.delete(source_uri)
            raise


@asynccontextmanager
async def lifespan(_: FastAPI):
    ocr_provider.ensure_ready()
    with db() as connection, connection.cursor() as cursor:
        cursor.execute("SELECT 1 FROM knowledge_core.ingestion_jobs LIMIT 1")
    yield


app = FastAPI(title="Semora Knowledge Core", version="0.1.0", lifespan=lifespan)
configure_telemetry(app, "semora-knowledge")


class KnowledgeBaseCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=2000)


class SearchInput(BaseModel):
    query: str = Field(min_length=1, max_length=2000)
    knowledge_base_ids: list[str] = Field(default_factory=list)
    top_k: int = Field(default=8, ge=1, le=50)


@app.get("/healthz")
def healthz() -> dict[str, Any]:
    try:
        object_store.verify_security()
    except ObjectStoreError as exc:
        raise HTTPException(503, str(exc)) from exc
    with db() as connection, connection.cursor() as cursor:
        cursor.execute("SELECT state,count(*) AS count FROM knowledge_core.ingestion_jobs GROUP BY state")
        queue = {row["state"]: row["count"] for row in cursor.fetchall()}
    return {
        "status": "ok", "service": "semora-knowledge", "embedding_mode": embedding_provider.mode,
        "object_store": object_store.mode,
        "object_store_bucket": object_store.bucket if object_store.mode == "s3" else None,
        "object_store_security": object_store.security.public_status() if object_store.mode == "s3" else None,
        "ocr": ocr_provider.status(),
        "ingestion_queue": queue,
    }


@app.get("/internal/v1/knowledge-bases")
def list_knowledge_bases(x_semora_internal_token: str | None = Header(default=None)) -> list[dict[str, Any]]:
    require_internal_token(x_semora_internal_token)
    with db() as connection, connection.cursor() as cursor:
        cursor.execute(
            """SELECT kb.id,kb.name,kb.description,kb.created_at,
            count(DISTINCT d.id) AS knowledge_count,coalesce(sum(d.chunk_count),0) AS chunk_count,
            bool_or(d.parse_status IN ('pending','processing')) AS is_processing
            FROM knowledge_core.knowledge_bases kb LEFT JOIN knowledge_core.documents d ON d.knowledge_base_id=kb.id
            GROUP BY kb.id ORDER BY kb.created_at"""
        )
        return [dict(row) for row in cursor.fetchall()]


@app.post("/internal/v1/knowledge-bases", status_code=201)
def create_knowledge_base(body: KnowledgeBaseCreate, x_semora_internal_token: str | None = Header(default=None)) -> dict[str, Any]:
    require_internal_token(x_semora_internal_token)
    kb_id = str(uuid4())
    with db() as connection, connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO knowledge_core.knowledge_bases(id,name,description) VALUES(%s,%s,%s) RETURNING *",
            (kb_id, body.name, body.description),
        )
        return dict(cursor.fetchone())


@app.get("/internal/v1/knowledge-bases/{kb_id}/documents")
def list_documents(
    kb_id: UUID,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    x_semora_internal_token: str | None = Header(default=None),
) -> dict[str, Any]:
    require_internal_token(x_semora_internal_token)
    with db() as connection, connection.cursor() as cursor:
        cursor.execute("SELECT count(*) AS count FROM knowledge_core.documents WHERE knowledge_base_id=%s", (str(kb_id),))
        total = cursor.fetchone()["count"]
        cursor.execute(
            """SELECT d.id,d.knowledge_base_id,d.title,d.file_name,d.file_type,d.parse_status,d.enable_status,
            d.chunk_count,d.error_message,d.created_at,d.processed_at,j.state AS job_state,
            j.attempts,j.max_attempts,j.last_error AS job_error,j.updated_at AS job_updated_at
            FROM knowledge_core.documents d LEFT JOIN knowledge_core.ingestion_jobs j ON j.document_id=d.id
            WHERE d.knowledge_base_id=%s ORDER BY d.created_at DESC LIMIT %s OFFSET %s""",
            (str(kb_id), page_size, (page - 1) * page_size),
        )
        return {"documents": [dict(row) for row in cursor.fetchall()], "total": total, "page": page, "page_size": page_size}


@app.get("/internal/v1/documents/{document_id}")
def get_document(document_id: UUID, x_semora_internal_token: str | None = Header(default=None)) -> dict[str, Any]:
    require_internal_token(x_semora_internal_token)
    with db() as connection, connection.cursor() as cursor:
        cursor.execute(
            """SELECT id,knowledge_base_id,title,file_name,file_type,content_hash,parse_status,
            enable_status,chunk_count,error_message,created_at,processed_at
            FROM knowledge_core.documents WHERE id=%s""",
            (str(document_id),),
        )
        document = cursor.fetchone()
        if not document:
            raise HTTPException(404, "document not found")
        return dict(document)


@app.get("/internal/v1/documents/{document_id}/content")
def download_document(document_id: UUID, x_semora_internal_token: str | None = Header(default=None)) -> Response:
    require_internal_token(x_semora_internal_token)
    with db() as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT file_name,source_path,content_hash FROM knowledge_core.documents WHERE id=%s",
            (str(document_id),),
        )
        document = cursor.fetchone()
    if not document:
        raise HTTPException(404, "document not found")
    try:
        content = object_store.read_bytes(document["source_path"])
    except ObjectStoreError as exc:
        raise HTTPException(503, str(exc)) from exc
    file_name = Path(document["file_name"]).name
    media_type = mimetypes.guess_type(file_name)[0] or "application/octet-stream"
    return Response(
        content=content,
        media_type=media_type,
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(file_name)}",
            "X-Semora-Filename": file_name,
            "X-Semora-Content-Hash": document["content_hash"],
        },
    )


@app.get("/internal/v1/documents/{document_id}/chunks")
def get_document_chunks(document_id: UUID, x_semora_internal_token: str | None = Header(default=None)) -> dict[str, Any]:
    require_internal_token(x_semora_internal_token)
    with db() as connection, connection.cursor() as cursor:
        cursor.execute(
            """SELECT id,knowledge_base_id,title,file_name,file_type,content_hash,parse_status,
            enable_status,chunk_count,created_at,processed_at FROM knowledge_core.documents WHERE id=%s""",
            (str(document_id),),
        )
        document = cursor.fetchone()
        if not document:
            raise HTTPException(404, "document not found")
        if document["parse_status"] != "completed":
            raise HTTPException(409, f"document is not ready: {document['parse_status']}")
        cursor.execute(
            """SELECT id,chunk_index,locator,title,content,metadata,created_at
            FROM knowledge_core.chunks WHERE document_id=%s ORDER BY chunk_index""",
            (str(document_id),),
        )
        return {"document": dict(document), "chunks": [dict(row) for row in cursor.fetchall()]}


@app.post("/internal/v1/knowledge-bases/{kb_id}/documents", status_code=202)
async def upload_document(
    kb_id: UUID,
    file: UploadFile = File(...),
    x_semora_internal_token: str | None = Header(default=None),
) -> dict[str, Any]:
    require_internal_token(x_semora_internal_token)
    filename = Path(file.filename or "").name
    suffix = Path(filename).suffix.lower()
    if not filename or suffix not in ALLOWED_SUFFIXES:
        raise HTTPException(400, f"不支持的文档类型；支持：{', '.join(sorted(ALLOWED_SUFFIXES))}")
    content = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, f"文档超过 {MAX_UPLOAD_BYTES // 1024 // 1024} MB 限制")
    with db() as connection, connection.cursor() as cursor:
        cursor.execute("SELECT 1 FROM knowledge_core.knowledge_bases WHERE id=%s", (str(kb_id),))
        if not cursor.fetchone():
            raise HTTPException(404, "knowledge base not found")
    document_id = str(uuid4())
    digest = sha256(content).hexdigest()
    source_uri = object_store.put_bytes(document_id, filename, content, file.content_type)
    try:
        with db(autocommit=False) as connection, connection.cursor() as cursor:
            cursor.execute(
                """INSERT INTO knowledge_core.documents(
                id,knowledge_base_id,title,file_name,file_type,source_path,content_hash)
                VALUES(%s,%s,%s,%s,%s,%s,%s)""",
                (document_id, str(kb_id), Path(filename).stem, filename, suffix.lstrip("."), source_uri, digest),
            )
            create_ingestion_job(cursor, document_id)
    except Exception:
        object_store.delete(source_uri)
        raise
    return {
        "id": document_id, "parse_status": "pending", "job_state": "queued",
        "file_name": filename, "content_hash": digest,
    }


@app.post("/internal/v1/documents/{document_id}/retry", status_code=202)
def retry_document(document_id: UUID, x_semora_internal_token: str | None = Header(default=None)) -> dict[str, Any]:
    require_internal_token(x_semora_internal_token)
    with db(autocommit=False) as connection, connection.cursor() as cursor:
        cursor.execute(
            """SELECT j.state FROM knowledge_core.ingestion_jobs j
            WHERE j.document_id=%s FOR UPDATE""",
            (str(document_id),),
        )
        job = cursor.fetchone()
        if not job:
            raise HTTPException(404, "ingestion job not found")
        if job["state"] not in {"failed", "completed"}:
            raise HTTPException(409, f"document ingestion is already {job['state']}")
        cursor.execute(
            """UPDATE knowledge_core.ingestion_jobs SET state='queued',attempts=0,available_at=now(),
            locked_by=NULL,lease_expires_at=NULL,last_error=NULL,started_at=NULL,finished_at=NULL,updated_at=now()
            WHERE document_id=%s RETURNING id,state,attempts,max_attempts""",
            (str(document_id),),
        )
        result = dict(cursor.fetchone())
        cursor.execute(
            """UPDATE knowledge_core.documents SET parse_status='pending',enable_status='disabled',
            error_message=NULL,processed_at=NULL WHERE id=%s""",
            (str(document_id),),
        )
    return {"document_id": str(document_id), **result}


@app.post("/internal/v1/search")
def search(body: SearchInput, x_semora_internal_token: str | None = Header(default=None)) -> dict[str, Any]:
    require_internal_token(x_semora_internal_token)
    kb_ids = body.knowledge_base_ids or [DEMO_KB_ID]
    for value in kb_ids:
        try:
            UUID(value)
        except ValueError as exc:
            raise HTTPException(400, f"invalid knowledge base id: {value}") from exc
    query_vector = vector_literal(embedding_provider.embed([body.query])[0])
    fetch_count = min(body.top_k * 4, 100)
    with db() as connection, connection.cursor() as cursor:
        cursor.execute(
            """SELECT c.id,c.knowledge_base_id,c.document_id,c.chunk_index,c.locator,c.title,c.content,c.metadata,
            1-(c.embedding <=> %s::vector) AS channel_score FROM knowledge_core.chunks c
            JOIN knowledge_core.documents d ON d.id=c.document_id
            WHERE c.knowledge_base_id=ANY(%s::uuid[]) AND d.parse_status='completed' AND d.enable_status='enabled'
            ORDER BY c.embedding <=> %s::vector LIMIT %s""",
            (query_vector, kb_ids, query_vector, fetch_count),
        )
        vector_rows = [dict(row) for row in cursor.fetchall()]
        cursor.execute(
            """SELECT c.id,c.knowledge_base_id,c.document_id,c.chunk_index,c.locator,c.title,c.content,c.metadata,
            greatest(similarity(c.content,%s),similarity(c.title,%s),
                CASE WHEN c.content ILIKE %s THEN 1.0 ELSE 0.0 END) AS channel_score
            FROM knowledge_core.chunks c JOIN knowledge_core.documents d ON d.id=c.document_id
            WHERE c.knowledge_base_id=ANY(%s::uuid[]) AND d.parse_status='completed' AND d.enable_status='enabled'
            ORDER BY channel_score DESC LIMIT %s""",
            (body.query, body.query, f"%{body.query}%", kb_ids, fetch_count),
        )
        keyword_rows = [dict(row) for row in cursor.fetchall()]
    fused: dict[str, dict[str, Any]] = {}
    for channel, rows in (("vector", vector_rows), ("keyword", keyword_rows)):
        for rank, row in enumerate(rows, start=1):
            key = str(row["id"])
            item = fused.setdefault(key, {**row, "score": 0.0, "match_type": []})
            item["score"] += 1.0 / (60 + rank)
            item["match_type"].append(channel)
    ranked = sorted(fused.values(), key=lambda item: item["score"], reverse=True)[:body.top_k]
    for item in ranked:
        item["id"] = str(item["id"])
        item["knowledge_base_id"] = str(item["knowledge_base_id"])
        item["knowledge_id"] = str(item.pop("document_id"))
        item.pop("channel_score", None)
    return {"results": ranked, "embedding_mode": embedding_provider.mode, "fusion": "rrf"}
