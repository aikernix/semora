from __future__ import annotations

import os
import signal
import socket
import sys
from threading import Event, Thread
import time
from typing import Any
from uuid import uuid4

from .main import db, ingest_document
from .ocr import ocr_provider


POLL_SECONDS = float(os.getenv("KNOWLEDGE_WORKER_POLL_SECONDS", "1"))
LEASE_SECONDS = int(os.getenv("KNOWLEDGE_JOB_LEASE_SECONDS", "900"))
WORKER_ID = os.getenv("KNOWLEDGE_WORKER_ID", f"{socket.gethostname()}-{os.getpid()}-{uuid4().hex[:8]}")
STOP = Event()


def claim_job() -> dict[str, Any] | None:
    with db(autocommit=False) as connection, connection.cursor() as cursor:
        cursor.execute(
            """WITH candidate AS (
                SELECT id FROM knowledge_core.ingestion_jobs
                WHERE (state IN ('queued','retry_wait') AND available_at <= now())
                   OR (state='processing' AND lease_expires_at < now())
                ORDER BY available_at,created_at
                FOR UPDATE SKIP LOCKED LIMIT 1
            )
            UPDATE knowledge_core.ingestion_jobs j
            SET state='processing',attempts=j.attempts+1,locked_by=%s,
                lease_expires_at=now()+(%s * interval '1 second'),
                started_at=coalesce(j.started_at,now()),finished_at=NULL,updated_at=now()
            FROM candidate WHERE j.id=candidate.id
            RETURNING j.*""",
            (WORKER_ID, LEASE_SECONDS),
        )
        job = cursor.fetchone()
        if job:
            cursor.execute(
                """UPDATE knowledge_core.documents SET parse_status='processing',enable_status='disabled',
                error_message=NULL WHERE id=%s""",
                (job["document_id"],),
            )
        return dict(job) if job else None


def renew_lease(job_id: str) -> bool:
    with db() as connection, connection.cursor() as cursor:
        cursor.execute(
            """UPDATE knowledge_core.ingestion_jobs
            SET lease_expires_at=now()+(%s * interval '1 second'),updated_at=now()
            WHERE id=%s AND state='processing' AND locked_by=%s""",
            (LEASE_SECONDS, job_id, WORKER_ID),
        )
        return cursor.rowcount == 1


def heartbeat(job_id: str, done: Event) -> None:
    interval = max(1.0, LEASE_SECONDS / 3)
    while not done.wait(interval):
        if not renew_lease(job_id):
            return


def complete_job(job: dict[str, Any]) -> None:
    with db() as connection, connection.cursor() as cursor:
        cursor.execute(
            """UPDATE knowledge_core.ingestion_jobs SET state='completed',locked_by=NULL,
            lease_expires_at=NULL,last_error=NULL,finished_at=now(),updated_at=now()
            WHERE id=%s AND state='processing' AND locked_by=%s""",
            (job["id"], WORKER_ID),
        )
        if cursor.rowcount != 1:
            raise RuntimeError("ingestion job lease was lost before completion")


def fail_job(job: dict[str, Any], exc: Exception) -> None:
    error = f"{type(exc).__name__}: {exc}"[:1000]
    attempts = int(job["attempts"])
    max_attempts = int(job["max_attempts"])
    terminal = attempts >= max_attempts
    delay_seconds = min(300, 5 * (2 ** max(0, attempts - 1)))
    with db(autocommit=False) as connection, connection.cursor() as cursor:
        cursor.execute(
            """UPDATE knowledge_core.ingestion_jobs
            SET state=%s,available_at=CASE WHEN %s THEN available_at ELSE now()+(%s * interval '1 second') END,
                locked_by=NULL,lease_expires_at=NULL,last_error=%s,
                finished_at=CASE WHEN %s THEN now() ELSE NULL END,updated_at=now()
            WHERE id=%s AND state='processing' AND locked_by=%s""",
            ("failed" if terminal else "retry_wait", terminal, delay_seconds, error, terminal, job["id"], WORKER_ID),
        )
        if cursor.rowcount != 1:
            return
        cursor.execute(
            """UPDATE knowledge_core.documents SET parse_status=%s,enable_status='disabled',
            error_message=%s,processed_at=CASE WHEN %s THEN now() ELSE NULL END WHERE id=%s""",
            ("failed" if terminal else "pending", error, terminal, job["document_id"]),
        )


def process_one() -> bool:
    job = claim_job()
    if not job:
        return False
    done = Event()
    lease_thread = Thread(target=heartbeat, args=(str(job["id"]), done), daemon=True)
    lease_thread.start()
    try:
        ingest_document(str(job["document_id"]), job_id=str(job["id"]), worker_id=WORKER_ID)
        complete_job(job)
    except Exception as exc:
        fail_job(job, exc)
    finally:
        done.set()
        lease_thread.join(timeout=2)
    return True


def healthcheck() -> int:
    try:
        ocr_provider.ensure_ready()
        with db() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT 1 FROM knowledge_core.ingestion_jobs LIMIT 1")
        return 0
    except Exception:
        return 1


def run() -> None:
    ocr_provider.ensure_ready()
    signal.signal(signal.SIGTERM, lambda *_: STOP.set())
    signal.signal(signal.SIGINT, lambda *_: STOP.set())
    while not STOP.is_set():
        if not process_one():
            STOP.wait(POLL_SECONDS)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "healthcheck":
        raise SystemExit(healthcheck())
    run()
