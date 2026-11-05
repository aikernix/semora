from app.audit import AuditLedger
from semora_contracts import AuditEvent


def test_hash_chain_detects_tampering():
    ledger = AuditLedger()
    ledger.append("run-1", "trace-1", "plan.ready", "analyst", {"metric": "loan_balance"})
    ledger.append("run-1", "trace-1", "gate.completed", "sql_gate", {"allowed": True})
    assert ledger.verify("run-1") == (True, None)

    ledger._events["run-1"][0].payload["metric"] = "tampered"
    valid, sequence = ledger.verify("run-1")
    assert valid is False
    assert sequence == 1


def test_authoritative_store_allocates_and_loads_events():
    persisted: list[AuditEvent] = []

    def sink(run_id: str, trace_id: str, event_type: str, actor: str, payload: dict) -> AuditEvent:
        previous_hash = persisted[-1].event_hash if persisted else "0" * 64
        body = {
            "run_id": run_id,
            "sequence": len(persisted) + 1,
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
        persisted.append(event)
        return event

    ledger = AuditLedger(sink=sink, loader=lambda run_id: [event for event in persisted if event.run_id == run_id])
    first = ledger.append("run-db", "trace-db", "run.started", "owner", {"question": "余额"})
    second = ledger.append("run-db", "trace-db", "plan.ready", "analyst", {"metric": "loan_balance"})

    assert first.sequence == 1
    assert second.sequence == 2
    assert second.previous_hash == first.event_hash
    assert ledger.verify("run-db") == (True, None)

