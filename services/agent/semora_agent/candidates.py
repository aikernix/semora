"""Bound candidate storage. Copies cross every boundary; it grants no execution right."""
from __future__ import annotations
from copy import deepcopy
from dataclasses import dataclass
from uuid import uuid4
from .task_contracts import ArtifactBinding,content_hash


class CandidateUnavailable(ValueError):
    pass


@dataclass(frozen=True)
class CandidateRecord:
    id: str
    binding: ArtifactBinding
    payload: dict
    payload_hash: str


class CandidateRegistry:
    def __init__(self):
        self._entries: dict[str,CandidateRecord]={}
        self._invalid: CandidateRecord | None=None
        self._failures: dict[str,int]={}

    def clear(self):
        self._entries.clear()
        self._invalid=None
        self._failures.clear()

    def register(self, payload: dict, binding: ArtifactBinding) -> str:
        identity=str(uuid4())
        value=deepcopy(payload)
        self._entries[identity]=CandidateRecord(identity,binding,value,content_hash(value))
        return identity

    def valid(self, identity: str | None, binding: ArtifactBinding) -> bool:
        record=self._entries.get(identity)
        return bool(record and record.binding==binding and record.payload_hash==content_hash(record.payload))

    def valid_ids(self, binding: ArtifactBinding) -> list[str]:
        return [identity for identity in self._entries if self.valid(identity,binding)]

    def resolve(self, identity: str, binding: ArtifactBinding) -> dict:
        if not self.valid(identity,binding):
            raise CandidateUnavailable('candidate_id不存在或已失效；请在当前需求、权限和本体下重新校验')
        return deepcopy(self._entries[identity].payload)

    def replace_validated(self, identity: str, payload: dict, binding: ArtifactBinding):
        """Internal compiler completion only; cannot revive a stale candidate."""
        self.resolve(identity,binding)
        value=deepcopy(payload)
        self._entries[identity]=CandidateRecord(identity,binding,value,content_hash(value))

    def set_invalid(self, payload: dict, binding: ArtifactBinding, identity: str | None=None) -> str:
        identity=identity or str(uuid4())
        value=deepcopy(payload)
        self._invalid=CandidateRecord(identity,binding,value,content_hash(value))
        return identity

    def invalid(self, binding: ArtifactBinding) -> dict | None:
        record=self._invalid
        if not record or record.binding!=binding or record.payload_hash!=content_hash(record.payload):
            return None
        return {'id':record.id,'analysis':deepcopy(record.payload),'binding':record.binding.fingerprint}

    def clear_invalid(self):
        self._invalid=None

    def record_failure(self, signature: str) -> int:
        self._failures[signature]=self._failures.get(signature,0)+1
        return self._failures[signature]

    def snapshot(self) -> dict:
        return {k:{'analysis':deepcopy(v.payload),'binding':v.binding.fingerprint} for k,v in self._entries.items()}
