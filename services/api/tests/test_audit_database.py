from __future__ import annotations

import json
import os
from uuid import uuid4

import psycopg
import pytest

from app.audit import AuditLedger
from semora_contracts import AuditEvent


TEST_DSN = os.getenv("TEST_METADATA_DATABASE_URL")


@pytest.mark.skipif(not TEST_DSN, reason="TEST_METADATA_DATABASE_URL is not configured")
def test_database_enforces_contiguous_append_and_rejects_mutation():
    run_id = f"audit-test-{uuid4()}"
    body = {
        "run_id": run_id,
        "sequence": 1,
        "event_type": "test.started",
        "actor": "pytest",
        "payload": {"safe": True},
        "trace_id": str(uuid4()),
    }
    previous_hash = "0" * 64
    event = AuditEvent(
        **body,
        previous_hash=previous_hash,
        event_hash=AuditLedger._hash(previous_hash, body),
    )

    with psycopg.connect(TEST_DSN, autocommit=False) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """INSERT INTO semora_meta.audit_events(
                    id,run_id,sequence,event_type,actor,trace_id,previous_hash,event_hash,payload,created_at
                ) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s)""",
                (
                    event.id,
                    event.run_id,
                    event.sequence,
                    event.event_type,
                    event.actor,
                    event.trace_id,
                    event.previous_hash,
                    event.event_hash,
                    event.model_dump_json(),
                    event.created_at,
                ),
            )
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                cursor.execute(
                    "UPDATE semora_meta.audit_events SET actor='tampered' WHERE id=%s",
                    (event.id,),
                )
        connection.rollback()
