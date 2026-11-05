from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from collections.abc import Callable
from threading import RLock

from semora_contracts import AuditEvent


class AuditLedger:
    """Append-only per-run hash chain with an optional authoritative store."""

    def __init__(
        self,
        sink: Callable[[str, str, str, str, dict], AuditEvent] | None = None,
        loader: Callable[[str], list[AuditEvent]] | None = None,
    ) -> None:
        self._events: dict[str, list[AuditEvent]] = defaultdict(list)
        self._sink = sink
        self._loader = loader
        self._lock = RLock()

    @staticmethod
    def _hash(previous_hash: str, body: dict) -> str:
        canonical = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(f"{previous_hash}:{canonical}".encode()).hexdigest()

    def append(self, run_id: str, trace_id: str, event_type: str, actor: str, payload: dict) -> AuditEvent:
        if self._sink:
            return self._sink(run_id, trace_id, event_type, actor, payload)
        with self._lock:
            events = self._events[run_id]
            previous_hash = events[-1].event_hash if events else "0" * 64
            sequence = len(events) + 1
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
                event_hash=self._hash(previous_hash, body),
            )
            events.append(event)
            return event

    def list(self, run_id: str) -> list[AuditEvent]:
        if self._loader:
            return self._loader(run_id)
        with self._lock:
            return list(self._events.get(run_id, []))

    def verify(self, run_id: str) -> tuple[bool, int | None]:
        previous_hash = "0" * 64
        for event in self.list(run_id):
            body = {
                "run_id": event.run_id,
                "sequence": event.sequence,
                "event_type": event.event_type,
                "actor": event.actor,
                "payload": event.payload,
                "trace_id": event.trace_id,
            }
            if event.previous_hash != previous_hash or event.event_hash != self._hash(previous_hash, body):
                return False, event.sequence
            previous_hash = event.event_hash
        return True, None

