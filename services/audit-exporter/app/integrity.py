from __future__ import annotations

import hashlib
import json
from typing import Any


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")


def verify_event_chain(events: list[dict[str, Any]]) -> tuple[bool, int | None, str]:
    previous_hash = "0" * 64
    for expected_sequence, event in enumerate(events, start=1):
        body = {
            "run_id": event.get("run_id"),
            "sequence": event.get("sequence"),
            "event_type": event.get("event_type"),
            "actor": event.get("actor"),
            "payload": event.get("payload"),
            "trace_id": event.get("trace_id"),
        }
        digest = hashlib.sha256(f"{previous_hash}:".encode() + canonical_json(body)).hexdigest()
        if (
            event.get("sequence") != expected_sequence
            or event.get("previous_hash") != previous_hash
            or event.get("event_hash") != digest
        ):
            return False, expected_sequence, previous_hash
        previous_hash = digest
    return bool(events), None, previous_hash
