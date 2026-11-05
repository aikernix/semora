from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any

import boto3
import psycopg
from botocore.config import Config
from botocore.exceptions import ClientError
from confluent_kafka import Producer
from fastapi import FastAPI, Header, HTTPException
from psycopg.rows import dict_row
from semora_telemetry import configure_telemetry
from semora_s3_security import S3SecurityError, S3SecurityPolicy

from .integrity import canonical_json, verify_event_chain


LOGGER = logging.getLogger("semora.audit_exporter")
DATABASE_URL = os.environ["AUDIT_EXPORTER_DATABASE_URL"]
BUCKET = os.getenv("AUDIT_ARCHIVE_S3_BUCKET", "semora-audit")
WORM_MODE = os.getenv("AUDIT_ARCHIVE_WORM_MODE", "disabled").lower()
RETENTION_YEARS = int(os.getenv("AUDIT_ARCHIVE_RETENTION_YEARS", "10"))
POLL_SECONDS = float(os.getenv("AUDIT_ARCHIVE_POLL_SECONDS", "5"))
INTERNAL_TOKEN = os.getenv("INTERNAL_TOKEN", "semora-dev-internal")
WORKER_STATE: dict[str, Any] = {
    "last_success": None,
    "last_error": None,
}

if WORM_MODE not in {"disabled", "governance", "compliance"}:
    raise RuntimeError("AUDIT_ARCHIVE_WORM_MODE must be disabled, governance or compliance")
if not 1 <= RETENTION_YEARS <= 100:
    raise RuntimeError("AUDIT_ARCHIVE_RETENTION_YEARS must be between 1 and 100")

S3_SECURITY = S3SecurityPolicy.from_env(
    "AUDIT_ARCHIVE_S3",
    default_require_versioning=WORM_MODE != "disabled",
)

s3 = boto3.client(
    "s3",
    endpoint_url=os.getenv("AUDIT_ARCHIVE_S3_ENDPOINT") or None,
    aws_access_key_id=os.getenv("AUDIT_ARCHIVE_S3_ACCESS_KEY") or None,
    aws_secret_access_key=os.getenv("AUDIT_ARCHIVE_S3_SECRET_KEY") or None,
    region_name=os.getenv("AUDIT_ARCHIVE_S3_REGION", "us-east-1"),
    config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
)


def require_internal_token(value: str | None) -> None:
    if value != INTERNAL_TOKEN:
        raise HTTPException(401, "invalid internal token")


def verify_bucket_security() -> None:
    s3.head_bucket(Bucket=BUCKET)
    if WORM_MODE != "disabled":
        try:
            configuration = s3.get_object_lock_configuration(Bucket=BUCKET)
            enabled = configuration.get("ObjectLockConfiguration", {}).get("ObjectLockEnabled")
            if enabled != "Enabled":
                raise RuntimeError("audit archive bucket does not have Object Lock enabled")
        except ClientError as exc:
            raise RuntimeError("cannot verify audit archive Object Lock configuration") from exc
    try:
        S3_SECURITY.verify_bucket(s3, BUCKET)
    except (ClientError, S3SecurityError) as exc:
        raise RuntimeError(f"cannot verify audit archive bucket security: {exc}") from exc


def ensure_bucket() -> None:
    try:
        s3.head_bucket(Bucket=BUCKET)
    except ClientError as exc:
        status = int(exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0))
        code = str(exc.response.get("Error", {}).get("Code", ""))
        if status == 403:
            raise RuntimeError("audit archive bucket access denied") from exc
        if status != 404 and code not in {"404", "NoSuchBucket", "NotFound"}:
            raise RuntimeError(f"cannot inspect audit archive bucket: {code or status}") from exc
        kwargs: dict[str, Any] = {"Bucket": BUCKET}
        if WORM_MODE != "disabled":
            kwargs["ObjectLockEnabledForBucket"] = True
        s3.create_bucket(**kwargs)
    verify_bucket_security()


def build_archive(run: dict[str, Any], events: list[dict[str, Any]]) -> tuple[bytes, dict[str, Any]]:
    valid, invalid_sequence, terminal_hash = verify_event_chain(events)
    if not valid:
        raise RuntimeError(f"audit chain invalid at sequence {invalid_sequence}")
    manifest = {
        "schema": "semora.audit.archive/v1",
        "run_id": run["id"],
        "run_status": run["status"],
        "closed_at": run["updated_at"],
        "event_count": len(events),
        "terminal_event_hash": terminal_hash,
        "trace_ids": sorted({str(item["trace_id"]) for item in events}),
        "hash_algorithm": "sha256-chain",
    }
    return canonical_json({"manifest": manifest, "events": events}), manifest


def put_immutable(key: str, content: bytes, retention_until: datetime | None) -> None:
    # Object Lock or versioning can be disabled after startup. Validate at the
    # point of write so a drifted bucket never receives a nominally compliant
    # audit archive.
    verify_bucket_security()
    digest = hashlib.sha256(content).digest()
    kwargs: dict[str, Any] = {
        "Bucket": BUCKET,
        "Key": key,
        "Body": content,
        "ContentType": "application/json",
        "ChecksumSHA256": base64.b64encode(digest).decode(),
        "IfNoneMatch": "*",
        "Metadata": {"sha256": digest.hex(), "semora-format": "audit-archive-v1"},
    }
    if retention_until is not None:
        kwargs["ObjectLockMode"] = WORM_MODE.upper()
        kwargs["ObjectLockRetainUntilDate"] = retention_until
    kwargs.update(S3_SECURITY.put_options())
    try:
        s3.put_object(**kwargs)
        S3_SECURITY.verify_object(s3.head_object(Bucket=BUCKET, Key=key))
    except ClientError as exc:
        status = int(exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0))
        code = str(exc.response.get("Error", {}).get("Code", ""))
        if status not in {409, 412} and code not in {"PreconditionFailed", "ConditionalRequestConflict"}:
            raise
        response = s3.get_object(Bucket=BUCKET, Key=key)
        S3_SECURITY.verify_object(response)
        existing = response["Body"].read()
        if not hashlib.sha256(existing).digest() == digest:
            raise RuntimeError("immutable audit archive key collision") from exc


def publish_manifest(manifest: dict[str, Any], object_uri: str, content_hash: str) -> dict[str, Any]:
    bootstrap = os.getenv("AUDIT_KAFKA_BOOTSTRAP_SERVERS", "").strip()
    topic = os.getenv("AUDIT_KAFKA_TOPIC", "semora.audit.archived")
    if not bootstrap:
        return {"topic": None, "partition": None, "offset": None}
    config: dict[str, Any] = {
        "bootstrap.servers": bootstrap,
        "enable.idempotence": True,
        "acks": "all",
        "client.id": "semora-audit-exporter",
    }
    for env_name, key in (
        ("AUDIT_KAFKA_SECURITY_PROTOCOL", "security.protocol"),
        ("AUDIT_KAFKA_SASL_MECHANISM", "sasl.mechanism"),
        ("AUDIT_KAFKA_SASL_USERNAME", "sasl.username"),
        ("AUDIT_KAFKA_SASL_PASSWORD", "sasl.password"),
    ):
        if os.getenv(env_name):
            config[key] = os.environ[env_name]
    result: dict[str, Any] = {}
    error: list[str] = []

    def delivered(err, message) -> None:
        if err is not None:
            error.append(str(err))
        else:
            result.update(topic=message.topic(), partition=message.partition(), offset=message.offset())

    message = canonical_json({**manifest, "object_uri": object_uri, "content_hash": content_hash})
    producer = Producer(config)
    producer.produce(topic, key=str(manifest["run_id"]), value=message, on_delivery=delivered)
    remaining = producer.flush(float(os.getenv("AUDIT_KAFKA_FLUSH_TIMEOUT_SECONDS", "10")))
    if remaining or error:
        raise RuntimeError(f"Kafka archive manifest delivery failed: {error[0] if error else 'timeout'}")
    return result


def export_run(run_id: str) -> bool:
    with psycopg.connect(DATABASE_URL, autocommit=False, row_factory=dict_row) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_try_advisory_xact_lock(hashtextextended(%s, 1)) AS locked", (run_id,))
            if not cursor.fetchone()["locked"]:
                return False
            cursor.execute("SELECT 1 FROM semora_meta.audit_archives WHERE run_id=%s", (run_id,))
            if cursor.fetchone():
                return False
            cursor.execute(
                "SELECT id,status,updated_at FROM semora_meta.runs WHERE id=%s AND status IN ('completed','failed')",
                (run_id,),
            )
            run = cursor.fetchone()
            if not run:
                return False
            cursor.execute(
                "SELECT payload FROM semora_meta.audit_events WHERE run_id=%s ORDER BY sequence",
                (run_id,),
            )
            events = [row["payload"] for row in cursor.fetchall()]
            content, manifest = build_archive(run, events)
            content_hash = hashlib.sha256(content).hexdigest()
            closed_at = run["updated_at"].astimezone(timezone.utc)
            key = f"audit/{closed_at:%Y/%m/%d}/{run_id}/{content_hash}.json"
            retention_until = None
            if WORM_MODE != "disabled":
                retention_until = datetime.now(timezone.utc) + timedelta(days=365 * RETENTION_YEARS)
            put_immutable(key, content, retention_until)
            object_uri = f"s3://{BUCKET}/{key}"
            kafka = publish_manifest(manifest, object_uri, content_hash)
            cursor.execute(
                """INSERT INTO semora_meta.audit_archives(
                    run_id,event_count,terminal_event_hash,content_hash,object_uri,retention_until,
                    kafka_topic,kafka_partition,kafka_offset
                ) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (
                    run_id,
                    manifest["event_count"],
                    manifest["terminal_event_hash"],
                    content_hash,
                    object_uri,
                    retention_until,
                    kafka.get("topic"),
                    kafka.get("partition"),
                    kafka.get("offset"),
                ),
            )
        connection.commit()
    return True


def export_pending(limit: int = 20) -> int:
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as connection, connection.cursor() as cursor:
        cursor.execute(
            """SELECT r.id FROM semora_meta.runs r
            LEFT JOIN semora_meta.audit_archives a ON a.run_id=r.id
            WHERE r.status IN ('completed','failed') AND a.run_id IS NULL
            ORDER BY r.updated_at LIMIT %s""",
            (limit,),
        )
        run_ids = [row["id"] for row in cursor.fetchall()]
    exported = 0
    for run_id in run_ids:
        try:
            exported += int(export_run(run_id))
        except Exception:
            LOGGER.exception("audit archive export failed for run_id=%s", run_id)
    return exported


def archive_record(run_id: str) -> dict[str, Any] | None:
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT * FROM semora_meta.audit_archives WHERE run_id=%s", (run_id,))
        row = cursor.fetchone()
        return dict(row) if row else None


def verify_archive(run_id: str) -> dict[str, Any]:
    record = archive_record(run_id)
    if not record:
        return {"run_id": run_id, "archived": False, "valid": False}
    bucket, key = record["object_uri"].removeprefix("s3://").split("/", 1)
    response = s3.get_object(Bucket=bucket, Key=key)
    S3_SECURITY.verify_object(response)
    content = response["Body"].read()
    content_hash = hashlib.sha256(content).hexdigest()
    document = json.loads(content)
    events = document.get("events", [])
    chain_valid, invalid_sequence, terminal_hash = verify_event_chain(events)
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT count(*) AS count, max(sequence) AS max_sequence FROM semora_meta.audit_events WHERE run_id=%s",
            (run_id,),
        )
        current = cursor.fetchone()
        cursor.execute(
            "SELECT event_hash FROM semora_meta.audit_events WHERE run_id=%s ORDER BY sequence DESC LIMIT 1",
            (run_id,),
        )
        last = cursor.fetchone()
    valid = bool(
        chain_valid
        and content_hash == record["content_hash"]
        and len(events) == record["event_count"] == current["count"]
        and terminal_hash == record["terminal_event_hash"] == (last or {}).get("event_hash")
    )
    return {
        "run_id": run_id,
        "archived": True,
        "valid": valid,
        "invalid_sequence": invalid_sequence,
        "event_count": record["event_count"],
        "terminal_event_hash": record["terminal_event_hash"],
        "content_hash": record["content_hash"],
        "object_uri": record["object_uri"],
        "retention_until": record["retention_until"],
        "exported_at": record["exported_at"],
        "kafka": {
            "topic": record["kafka_topic"],
            "partition": record["kafka_partition"],
            "offset": record["kafka_offset"],
        },
        "encryption": S3_SECURITY.public_status(),
    }


async def worker_loop(stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            await asyncio.to_thread(export_pending)
            WORKER_STATE["last_success"] = datetime.now(timezone.utc)
            WORKER_STATE["last_error"] = None
        except Exception as exc:
            WORKER_STATE["last_error"] = f"{type(exc).__name__}: {exc}"
            LOGGER.exception("audit archive scan failed")
        try:
            await asyncio.wait_for(stop.wait(), timeout=POLL_SECONDS)
        except TimeoutError:
            pass


@asynccontextmanager
async def lifespan(_: FastAPI):
    await asyncio.to_thread(ensure_bucket)
    await asyncio.to_thread(export_pending)
    WORKER_STATE["last_success"] = datetime.now(timezone.utc)
    WORKER_STATE["last_error"] = None
    stop = asyncio.Event()
    worker = asyncio.create_task(worker_loop(stop), name="audit-archive-exporter")
    try:
        yield
    finally:
        stop.set()
        await worker


app = FastAPI(title="Semora Audit Exporter", version="0.1.0", lifespan=lifespan)
configure_telemetry(app, "semora-audit-exporter")


@app.get("/healthz")
def healthz() -> dict[str, Any]:
    with psycopg.connect(DATABASE_URL) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT 1")
    try:
        verify_bucket_security()
    except (ClientError, RuntimeError) as exc:
        raise HTTPException(503, f"audit archive bucket security check failed: {exc}") from exc
    last_success = WORKER_STATE["last_success"]
    last_error = WORKER_STATE["last_error"]
    stale_after = max(60.0, POLL_SECONDS * 5)
    if last_success is None or (datetime.now(timezone.utc) - last_success).total_seconds() > stale_after:
        raise HTTPException(503, "audit archive worker has not completed a recent scan")
    if last_error:
        raise HTTPException(503, f"audit archive worker failed: {last_error}")
    return {
        "status": "ok",
        "service": "semora-audit-exporter",
        "worm_mode": WORM_MODE,
        "object_store_security": S3_SECURITY.public_status(),
        "last_successful_scan": last_success.isoformat(),
    }


@app.post("/internal/v1/archives/run-once")
def run_once(x_semora_internal_token: str | None = Header(default=None)) -> dict[str, int]:
    require_internal_token(x_semora_internal_token)
    return {"exported": export_pending()}


@app.get("/internal/v1/archives/{run_id}")
def get_archive(run_id: str, x_semora_internal_token: str | None = Header(default=None)) -> dict[str, Any]:
    require_internal_token(x_semora_internal_token)
    record = archive_record(run_id)
    if not record:
        raise HTTPException(404, "audit archive not found")
    return record


@app.get("/internal/v1/archives/{run_id}/verify")
def verify(run_id: str, x_semora_internal_token: str | None = Header(default=None)) -> dict[str, Any]:
    require_internal_token(x_semora_internal_token)
    try:
        return verify_archive(run_id)
    except (ClientError, S3SecurityError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(409, f"audit archive verification failed: {type(exc).__name__}") from exc
