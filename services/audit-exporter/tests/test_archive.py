from __future__ import annotations

import hashlib
import importlib.util
import os
import unittest
from pathlib import Path

os.environ.setdefault("AUDIT_EXPORTER_DATABASE_URL", "postgresql://unused")
os.environ.setdefault("AUDIT_ARCHIVE_S3_ACCESS_KEY", "test")
os.environ.setdefault("AUDIT_ARCHIVE_S3_SECRET_KEY", "test")

module_path = Path(__file__).resolve().parents[1] / "app" / "integrity.py"
spec = importlib.util.spec_from_file_location("semora_audit_integrity", module_path)
assert spec and spec.loader
integrity = importlib.util.module_from_spec(spec)
spec.loader.exec_module(integrity)
canonical_json = integrity.canonical_json
verify_event_chain = integrity.verify_event_chain


def event(sequence: int, previous_hash: str) -> dict:
    body = {
        "run_id": "run-1",
        "sequence": sequence,
        "event_type": "run.started" if sequence == 1 else "answer.completed",
        "actor": "conversation_owner",
        "payload": {"value": sequence},
        "trace_id": "trace-1",
    }
    event_hash = hashlib.sha256(f"{previous_hash}:".encode() + canonical_json(body)).hexdigest()
    return {**body, "previous_hash": previous_hash, "event_hash": event_hash}


class AuditArchiveTest(unittest.TestCase):
    def test_valid_chain_returns_terminal_hash(self) -> None:
        first = event(1, "0" * 64)
        second = event(2, first["event_hash"])
        valid, invalid_sequence, terminal_hash = verify_event_chain([first, second])
        self.assertTrue(valid)
        self.assertIsNone(invalid_sequence)
        self.assertEqual(terminal_hash, second["event_hash"])

    def test_payload_tampering_is_detected(self) -> None:
        first = event(1, "0" * 64)
        second = event(2, first["event_hash"])
        second["payload"]["value"] = 999
        valid, invalid_sequence, _ = verify_event_chain([first, second])
        self.assertFalse(valid)
        self.assertEqual(invalid_sequence, 2)

    def test_sequence_gap_is_detected(self) -> None:
        first = event(1, "0" * 64)
        third = event(3, first["event_hash"])
        valid, invalid_sequence, _ = verify_event_chain([first, third])
        self.assertFalse(valid)
        self.assertEqual(invalid_sequence, 2)


if __name__ == "__main__":
    unittest.main()
