from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import os
from uuid import uuid4

import psycopg
from semora_contracts import ApprovalDecision, QueryRun

from .audit import AuditLedger


@dataclass
class Session:
    id: str
    title: str
    created_at: datetime
    domain: str = "retail_credit"
    owner_subject: str | None = None
    run_ids: list[str] = field(default_factory=list)
    requirement_form: dict[str, str] = field(default_factory=dict)


class Store:
    def __init__(self) -> None:
        self._dsn = os.getenv("METADATA_DATABASE_URL")
        seed_id = "demo-session"
        self.sessions: dict[str, Session] = {}
        if not self._dsn or os.getenv("SEMORA_DEMO_SEED", "false").lower() == "true":
            self.sessions[seed_id] = Session(seed_id, "零售信贷经营分析", datetime.now(timezone.utc), "retail_credit", None)
        self.runs: dict[str, QueryRun] = {}
        self.approvals: dict[str, ApprovalDecision] = {}
        self.audit = AuditLedger(
            self._persist_audit if self._dsn else None,
            self._load_audit if self._dsn else None,
        )
        if self._dsn:
            self._hydrate()

    def _connect(self):
        return psycopg.connect(self._dsn, autocommit=True)

    def _hydrate(self) -> None:
        try:
            with self._connect() as connection, connection.cursor() as cursor:
                cursor.execute(
                    "SELECT id, title, created_at, domain, owner_subject, requirement_form FROM semora_meta.sessions ORDER BY created_at"
                )
                for row in cursor.fetchall():
                    self.sessions[row[0]] = Session(row[0], row[1], row[2], row[3], row[4])
                    self.sessions[row[0]].requirement_form = row[5] or {}
                cursor.execute("SELECT payload FROM semora_meta.runs ORDER BY created_at")
                for (payload,) in cursor.fetchall():
                    run = QueryRun.model_validate(payload)
                    self.runs[run.id] = run
                    self.sessions.setdefault(
                        run.session_id,
                        Session(
                            run.session_id,
                            run.question[:24],
                            run.created_at,
                            run.domain,
                            run.authorization.subject if run.authorization else None,
                        ),
                    ).run_ids.append(run.id)
                    if run.approval:
                        self.approvals[run.approval.id] = run.approval
        except Exception as exc:
            raise RuntimeError(f"metadata database initialization failed: {exc}") from exc

    def _persist_session(self, session: Session) -> None:
        if not self._dsn:
            return
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """INSERT INTO semora_meta.sessions(id,title,created_at,domain,owner_subject,requirement_form)
                VALUES(%s,%s,%s,%s,%s,%s::jsonb)
                ON CONFLICT(id) DO UPDATE SET title=excluded.title,domain=excluded.domain,
                owner_subject=coalesce(semora_meta.sessions.owner_subject,excluded.owner_subject),
                requirement_form=excluded.requirement_form""",
                (session.id, session.title, session.created_at, session.domain, session.owner_subject, json.dumps(session.requirement_form,ensure_ascii=False)),
            )

    def _persist_audit(
        self,
        run_id: str,
        trace_id: str,
        event_type: str,
        actor: str,
        payload: dict,
    ):
        from semora_contracts import AuditEvent

        with psycopg.connect(self._dsn, autocommit=False) as connection, connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (run_id,))
            cursor.execute(
                """SELECT sequence,event_hash FROM semora_meta.audit_events
                WHERE run_id=%s ORDER BY sequence DESC LIMIT 1""",
                (run_id,),
            )
            previous = cursor.fetchone()
            sequence = int(previous[0]) + 1 if previous else 1
            previous_hash = str(previous[1]) if previous else "0" * 64
            body = {
                "run_id": run_id,
                "sequence": sequence,
                "event_type": event_type,
                "actor": actor,
                "payload": payload,
                "trace_id": trace_id,
            }
            event = AuditEvent(
                **body,
                previous_hash=previous_hash,
                event_hash=AuditLedger._hash(previous_hash, body),
            )
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
            connection.commit()
            return event

    def _load_audit(self, run_id: str):
        from semora_contracts import AuditEvent

        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT payload FROM semora_meta.audit_events WHERE run_id=%s ORDER BY sequence",
                (run_id,),
            )
            return [AuditEvent.model_validate(row[0]) for row in cursor.fetchall()]

    def create_session(
        self,
        title: str = "新查询",
        domain: str = "retail_credit",
        owner_subject: str | None = None,
    ) -> Session:
        session = Session(str(uuid4()), title, datetime.now(timezone.utc), domain, owner_subject)
        self.sessions[session.id] = session
        self._persist_session(session)
        return session

    def add_run(self, run: QueryRun) -> None:
        self.runs[run.id] = run
        self.sessions.setdefault(
            run.session_id,
            Session(
                run.session_id,
                run.question[:24],
                datetime.now(timezone.utc),
                run.domain,
                run.authorization.subject if run.authorization else None,
            ),
        ).run_ids.append(run.id)
        self._persist_session(self.sessions[run.session_id])
        self.save_run(run)
        if run.approval:
            self.approvals[run.approval.id] = run.approval

    def save_run(self, run: QueryRun) -> None:
        if not self._dsn:
            return
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """INSERT INTO semora_meta.runs(id,session_id,status,payload,created_at,updated_at)
                VALUES(%s,%s,%s,%s::jsonb,%s,now())
                ON CONFLICT(id) DO UPDATE SET status=excluded.status,payload=excluded.payload,updated_at=now()""",
                (run.id, run.session_id, run.status.value, json.dumps({**run.model_dump(mode='json'), 'agent_memory': run.agent_memory,
                    'source_results': {key: value.model_dump(mode='json') for key, value in run.source_results.items()}}, ensure_ascii=False), run.created_at),
            )

    def list_audit_runs(self, search: str = "", status: str = "", limit: int = 50) -> list[dict]:
        search = search.strip()
        status = status.strip()
        if self._dsn:
            pattern = f"%{search}%"
            with self._connect() as connection, connection.cursor() as cursor:
                cursor.execute(
                    """SELECT r.id,r.session_id,r.status,r.payload->>'trace_id',
                        coalesce(r.payload->>'domain',''),coalesce(r.payload->>'question',''),
                        r.payload #>> '{authorization,subject}',
                        r.payload #>> '{authorization,username}',
                        r.created_at,r.updated_at,count(e.id),a.run_id IS NOT NULL
                    FROM semora_meta.runs r
                    LEFT JOIN semora_meta.audit_events e ON e.run_id=r.id
                    LEFT JOIN semora_meta.audit_archives a ON a.run_id=r.id
                    WHERE (%s='' OR r.id ILIKE %s OR r.payload->>'trace_id' ILIKE %s
                        OR r.payload->>'question' ILIKE %s)
                      AND (%s='' OR r.status=%s)
                    GROUP BY r.id,a.run_id
                    ORDER BY r.updated_at DESC
                    LIMIT %s""",
                    (search, pattern, pattern, pattern, status, status, limit),
                )
                return [
                    {
                        "id": row[0],
                        "session_id": row[1],
                        "status": row[2],
                        "trace_id": row[3],
                        "domain": row[4],
                        "question": row[5],
                        "owner_subject": row[6],
                        "owner_username": row[7],
                        "created_at": row[8],
                        "updated_at": row[9],
                        "event_count": row[10],
                        "archived": row[11],
                    }
                    for row in cursor.fetchall()
                ]

        values: list[dict] = []
        search_lower = search.lower()
        for run in sorted(self.runs.values(), key=lambda item: item.created_at, reverse=True):
            if status and run.status.value != status:
                continue
            if search_lower and search_lower not in " ".join(
                (run.id, run.trace_id, run.question)
            ).lower():
                continue
            values.append(
                {
                    "id": run.id,
                    "session_id": run.session_id,
                    "status": run.status.value,
                    "trace_id": run.trace_id,
                    "domain": run.domain,
                    "question": run.question,
                    "owner_subject": run.authorization.subject if run.authorization else None,
                    "owner_username": run.authorization.username if run.authorization else None,
                    "created_at": run.created_at,
                    "updated_at": run.created_at,
                    "event_count": len(self.audit.list(run.id)),
                    "archived": False,
                }
            )
            if len(values) >= limit:
                break
        return values


store = Store()

