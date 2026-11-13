"""Persistent data-source onboarding and bounded relationship discovery."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import psycopg

from .datasource_table_storage import tables_digest
from .mdl_export import column_type
from .relationship_evidence import recall_field_pairs, finalize_relationships

GENERATOR_VERSION = "source-ontology-v3"
PROFILE_VERSION = "indexed-directional-v1"
TERMINAL = {"completed", "completed_with_pending", "failed", "cancelled"}
GENERIC_NAMES = {"id", "code", "name", "type", "status", "date"}


def _now() -> datetime:
    return datetime.now(UTC)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _identifier(value: str, prefix: str = "x") -> str:
    value = re.sub(r"[^A-Za-z0-9_]", "_", value or "").strip("_")
    if not value or value[0].isdigit():
        value = prefix + "_" + value
    return value[:80] + "_" + hashlib.sha256(value.encode()).hexdigest()[:8]


def _words(*values: Any) -> set[str]:
    text = " ".join(str(value or "") for value in values).lower()
    return {word for word in re.split(r"[^a-z0-9\u4e00-\u9fff]+", text) if len(word) > 1}


def _keys(table: dict[str, Any], unique_only: bool = False) -> list[list[str]]:
    result = [list(item.get("columns") or []) for item in table.get("primary_keys") or []]
    result += [list(item.get("columns") or []) for item in table.get("unique_constraints") or []]
    if not unique_only:
        result += [list(item.get("columns") or []) for item in table.get("indexes") or []]
    return [item for item in result if item]


def _indexed(table: dict[str, Any], columns: list[str], *, unique: bool = False) -> bool:
    wanted = [str(value).lower() for value in columns]
    return any(
        [str(value).lower() for value in (key if unique else key[: len(wanted)])] == wanted
        for key in _keys(table, unique_only=unique)
    )


def _physical_table(table: dict[str, Any]) -> str:
    return ".".join(str(table.get(key)) for key in ("catalog", "schema", "name") if table.get(key))


def _ontology_type(column: dict[str, Any]) -> str:
    value = column_type(column) or "varchar"
    if value.startswith("decimal"):
        return "decimal"
    if value in {"integer", "smallint", "bigint"}:
        return "integer"
    if value in {"real", "double"}:
        return "decimal"
    if value in {"date", "timestamp", "timestamptz", "time"}:
        return value
    if value == "boolean":
        return "boolean"
    return "string"


def package_objects(
    datasource_id: str, tables: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], dict[str, dict[str, str]]]:
    objects = []
    mapping = {}
    source_prefix = _identifier(datasource_id, "ds")[:18]
    for table in tables:
        table_id = str(table.get("id") or _physical_table(table))
        object_name = _identifier(f"{source_prefix}_{table_id}", "object")
        columns = {
            str(column.get("name")): column
            for column in table.get("columns") or []
            if column.get("name")
        }
        properties = []
        field_map = {}
        for column_name, column in columns.items():
            property_name = _identifier(column_name, "field")
            field_map[column_name] = property_name
            properties.append(
                {
                    "name": property_name,
                    "label": column.get("description") or column.get("comment") or column_name,
                    "description": column.get("description") or column.get("comment") or "",
                    "column": column_name,
                    "type": _ontology_type(column),
                    "sensitive": False,
                    "nullable": bool(column.get("nullable", True)),
                }
            )
        primary = next(iter(table.get("primary_keys") or []), {}).get("columns") or []
        primary_property = field_map.get(primary[0], "") if len(primary) == 1 else ""
        objects.append(
            {
                "name": object_name,
                "label": table.get("description") or table.get("name") or table_id,
                "description": table.get("description") or "",
                "dataSourceId": datasource_id,
                "table": ".".join(
                    str(table.get(key)) for key in ("schema", "name") if table.get(key)
                )
                or str(table.get("name")),
                "primaryKey": primary_property,
                "properties": properties,
                "metadata": {
                    "sourceTableId": table_id,
                    "sourcePackageId": datasource_id,
                    "objectKind": table.get("kind") or "TABLE",
                },
            }
        )
        mapping[table_id] = {"object": object_name, **field_map}
    return objects, mapping


def _candidate(
    datasource_a: str,
    datasource_b: str,
    source: dict[str, Any],
    source_columns: list[str],
    target: dict[str, Any],
    target_columns: list[str],
    scope: str,
    evidence: list[dict[str, Any]],
    confidence: float,
    grade: str = "medium",
    status: str = "pending",
    domain: str | None = None,
) -> dict[str, Any]:
    value = {
        "source_table": source["id"],
        "columns": source_columns,
        "target_table": target["id"],
        "target_columns": target_columns,
    }
    candidate_id = hashlib.sha256(
        _json([datasource_a, datasource_b, domain or "", value]).encode()
    ).hexdigest()[:24]
    return {
        "id": candidate_id,
        "scope": scope,
        "source_datasource_id": datasource_a,
        "target_datasource_id": datasource_b,
        "source_table_id": source["id"],
        "target_table_id": target["id"],
        "source_columns": source_columns,
        "target_columns": target_columns,
        "key_pairs": [
            {"source": a, "target": b} for a, b in zip(source_columns, target_columns, strict=True)
        ],
        "condition_template": {
            "operator": "and_equals",
            "pairs": [
                {"left": a, "right": b} for a, b in zip(source_columns, target_columns, strict=True)
            ],
        },
        "evidence": evidence,
        "evidence_grade": grade,
        "confidence": round(confidence, 4),
        "cardinality": "N:1",
        "status": status,
        "domain": domain,
    }


def discover_candidates(
    datasource_a: str,
    tables_a: list[dict[str, Any]],
    datasource_b: str | None = None,
    tables_b: list[dict[str, Any]] | None = None,
    domain: str | None = None,
    declared_relationships: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Generate sparse candidates without putting a full catalog in a model prompt."""
    datasource_b = datasource_b or datasource_a
    tables_b = tables_b or tables_a
    scope = "single_source" if datasource_a == datasource_b else "cross_source"
    result: dict[str, dict[str, Any]] = {}
    # Learn boilerplate from this catalog rather than hard-coding field names.
    key_documents = []
    for table in tables_b:
        columns = {c.get("name"): c for c in table.get("columns") or []}
        for key in _keys(table, unique_only=True):
            for name in key:
                column = columns.get(name, {})
                key_documents.append(_words(column.get("description"), column.get("comment")))
    frequencies: dict[str, int] = {}
    for document in key_documents:
        for token in document:
            frequencies[token] = frequencies.get(token, 0) + 1
    boilerplate = {token for token, count in frequencies.items()
                   if count >= max(4, len(key_documents) * 0.15)}

    def semantic_words(*values):
        # The leading description names the field; trailing clauses often hold
        # storage types, migration notes and constraints rather than its meaning.
        meanings = [re.split(r"[;；\n]", str(value or ""), maxsplit=1)[0] for value in values]
        return {word for word in _words(*meanings) - boilerplate if not word.isdigit()}
    # Declared foreign keys are authoritative for the source-local package.
    if scope == "single_source":
        lookup = {table["id"]: table for table in tables_a}
        declared = list(declared_relationships or [])
        for source in tables_a:
            declared.extend(source.get("foreign_keys") or [])
        for fk in declared:
            source = lookup.get(fk.get("source_table"))
            if source:
                target = lookup.get(fk.get("target_table"))
                source_columns = list(fk.get("columns") or [])
                target_columns = list(fk.get("target_columns") or [])
                if target and source_columns and len(source_columns) == len(target_columns):
                    item = _candidate(
                        datasource_a,
                        datasource_b,
                        source,
                        source_columns,
                        target,
                        target_columns,
                        scope,
                        [{"kind": "foreign_key", "source": "database_constraint"}],
                        1.0,
                        "foreign_key",
                        "accepted",
                        domain,
                    )
                    result[item["id"]] = item
    # Index key registry keeps discovery sparse for large schemas.
    source_columns_by_name: dict[str, list[tuple[dict[str, Any], dict[str, Any]]]] = {}
    source_columns_by_token: dict[str, list[tuple[dict[str, Any], dict[str, Any]]]] = {}
    source_tables_by_id = {table["id"]: table for table in tables_a}
    for source in tables_a:
        for column in source.get("columns") or []:
            name = str(column.get("name") or "").lower()
            if name:
                source_columns_by_name.setdefault(name, []).append((source, column))
            for token in semantic_words(column.get("description"), column.get("comment")) - GENERIC_NAMES:
                source_columns_by_token.setdefault(token, []).append((source, column))
    for target in tables_b:
        target_columns_map = {
            str(column.get("name")): column for column in target.get("columns") or []
        }
        for target_columns in _keys(target, unique_only=True):
            if not target_columns:
                continue
            candidate_sets = []
            for target_column in target_columns:
                stem = str(target.get("name") or "").lower().rstrip("s")
                names = {
                    target_column.lower(),
                    f"{stem}_{target_column.lower()}",
                    f"{stem}_id",
                }
                target_tokens = (
                    semantic_words(
                        target_columns_map.get(target_column, {}).get("description"),
                        target_columns_map.get(target_column, {}).get("comment"),
                    )
                    - GENERIC_NAMES
                )
                candidate_sets.append(
                    {
                        source["id"]
                        for name in names
                        for source, _ in source_columns_by_name.get(name, [])
                    }
                    | {
                        source["id"]
                        for token in target_tokens
                        for source, _ in source_columns_by_token.get(token, [])
                    }
                )
            for source_id in set.intersection(*candidate_sets) if candidate_sets else set():
                source = source_tables_by_id[source_id]
                if datasource_a == datasource_b and source_id == target["id"]:
                    continue
                source_map = {
                    str(column.get("name")).lower(): column
                    for column in source.get("columns") or []
                }
                chosen = []
                evidence = []
                semantic = 0.0
                for target_column in target_columns:
                    stem = str(target.get("name") or "").lower().rstrip("s")
                    names = (
                        [f"{stem}_id", f"{stem}_{target_column.lower()}", target_column.lower()]
                        if target_column.lower() in {"id", "pk"}
                        else [target_column.lower(), f"{stem}_{target_column.lower()}"]
                    )
                    name = next((name for name in names if name in source_map), None)
                    matched_by_name = name is not None
                    if not name:
                        target_tokens = (
                            semantic_words(
                                target_columns_map.get(target_column, {}).get("description"),
                                target_columns_map.get(target_column, {}).get("comment"),
                            )
                            - GENERIC_NAMES
                        )
                        semantic_options = []
                        for source_name, source_column in source_map.items():
                            overlap = target_tokens & (
                                semantic_words(
                                    source_column.get("description"), source_column.get("comment")
                                )
                                - GENERIC_NAMES
                            )
                            if overlap:
                                semantic_options.append((len(overlap), source_name))
                        name = max(semantic_options, default=(0, None))[1]
                    if not name:
                        chosen = []
                        break
                    chosen.append(str(source_map[name]["name"]))
                    if name == target_column.lower():
                        semantic += 0.55
                        evidence.append({"kind": "same_name", "field": name})
                    elif matched_by_name:
                        semantic += 0.72
                        evidence.append({"kind": "entity_key_name", "field": name})
                    else:
                        semantic += 0.62
                        evidence.append({"kind": "comment_field_match", "field": name})
                    source_words = (
                        semantic_words(source_map[name].get("description"), source_map[name].get("comment"))
                        - GENERIC_NAMES
                    )
                    target_words = (
                        semantic_words(
                            target_columns_map.get(target_column, {}).get("description"),
                            target_columns_map.get(target_column, {}).get("comment"),
                        )
                        - GENERIC_NAMES
                    )
                    if source_words & target_words:
                        semantic += 0.08
                        evidence.append(
                            {
                                "kind": "comment_semantics",
                                "tokens": sorted(source_words & target_words)[:5],
                            }
                        )
                if not chosen or len(set(chosen)) != len(chosen):
                    continue
                source_map_exact = {
                    str(column.get("name")): column for column in source.get("columns") or []
                }
                compatible = all(
                    column_type(source_map_exact[a])
                    and column_type(source_map_exact[a]) == column_type(target_columns_map[b])
                    for a, b in zip(chosen, target_columns, strict=True)
                )
                if not compatible:
                    continue
                if (
                    len(chosen) == 1
                    and chosen[0].lower() in GENERIC_NAMES
                    and not any(e["kind"] == "comment_semantics" for e in evidence)
                ):
                    continue
                source_indexed = _indexed(source, chosen)
                target_unique = _indexed(target, target_columns, unique=True)
                evidence += [
                    {"kind": "type_compatible"},
                    {"kind": "target_unique_constraint"},
                    {"kind": "source_index", "confirmed": source_indexed},
                    {"kind": "target_unique_index", "confirmed": target_unique},
                    {"kind": "source_code", "status": "provider_not_configured", "locations": []},
                ]
                confidence = min(
                    0.94, semantic / max(1, len(target_columns)) + (0.04 if source_indexed else 0)
                )
                item = _candidate(
                    datasource_a,
                    datasource_b,
                    source,
                    chosen,
                    target,
                    target_columns,
                    scope,
                    evidence,
                    confidence,
                    "medium" if confidence >= 0.7 else "weak",
                    "pending",
                    domain,
                )
                if item["id"] not in result or result[item["id"]]["confidence"] < confidence:
                    result[item["id"]] = item
    for source, column, target, field, explicit in recall_field_pairs(
        [{**t, "_source_id": datasource_a} for t in tables_a],
        [{**t, "_source_id": datasource_b} for t in tables_b],
    ):
        if not column_type(column) or column_type(column) != column_type(field):
            continue
        evidence = [{"kind": "explicit_field_reference" if explicit else "semantic_recall"},
                    {"kind": "type_compatible"}, {"kind": "target_unique_constraint"},
                    {"kind": "source_index", "confirmed": _indexed(source, [column["name"]])},
                    {"kind": "target_unique_index", "confirmed": _indexed(target, [field["name"]], unique=True)}]
        item = _candidate(datasource_a, datasource_b, source, [column["name"]], target,
                          [field["name"]], scope, evidence, .9 if explicit else .75,
                          domain=domain)
        previous = result.get(item["id"])
        if previous and previous["evidence_grade"] == "foreign_key":
            continue
        if previous:
            previous["evidence"].extend(evidence[:1])
            previous["confidence"] = max(previous["confidence"], item["confidence"])
        else:
            result[item["id"]] = item
    return sorted(
        converge_candidates(list(result.values())),
        key=lambda item: (item["status"] != "accepted", -item["confidence"], item["id"]),
    )


def converge_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep bounded, evidenced alternatives per source key, never arbitrary pairs."""
    groups: dict[tuple, list[dict[str, Any]]] = {}
    authoritative = []
    for item in candidates:
        if item["evidence_grade"] == "foreign_key":
            authoritative.append(item)
            continue
        kinds = {entry["kind"] for entry in item["evidence"]}
        # A physical name shared across tables is not an entity reference.
        if not kinds & {"entity_key_name", "comment_semantics", "comment_field_match", "semantic_recall", "explicit_field_reference"}:
            continue
        key = (item["source_datasource_id"], item["source_table_id"], tuple(item["source_columns"]))
        groups.setdefault(key, []).append(item)
    output = list(authoritative)
    for key, values in groups.items():
        # A declared FK does not prohibit another independently evidenced link.
        values.sort(key=lambda i: (-i["confidence"], i["id"]))
        competing = len(values) > 1 and values[0]["confidence"] - values[1]["confidence"] < 0.1
        # Broad collisions are discovery noise, not thousands of review tasks.
        if competing and len(values) > 3:
            values = [item for item in values if any(e["kind"] in
                      {"explicit_field_reference", "entity_key_name", "semantic_recall"}
                      for e in item["evidence"])]
        for item in values[:3]:
            item["evidence"].append({"kind": "competition", "ambiguous": competing})
            output.append(item)
    return output


def package_relations(
    candidates: list[dict[str, Any]], mapping: dict[str, dict[str, str]]
) -> list[dict[str, Any]]:
    result = []
    for candidate in candidates:
        if candidate["status"] != "accepted":
            continue
        left = mapping.get(candidate["source_table_id"])
        right = mapping.get(candidate["target_table_id"])
        if not left or not right:
            continue
        pairs = []
        for pair in candidate["key_pairs"]:
            if pair["source"] not in left or pair["target"] not in right:
                pairs = []
                break
            pairs.append(
                f"{left['object']}.{left[pair['source']]} = {right['object']}.{right[pair['target']]}"
            )
        if pairs:
            result.append(
                {
                    "name": "r_" + candidate["id"],
                    "label": "自动发现的关联",
                    "from": left["object"],
                    "to": right["object"],
                    "joinType": {"1:1": "one_to_one", "1:N": "one_to_many",
                                 "N:1": "many_to_one", "N:N": "many_to_many"}[candidate["cardinality"]],
                    "cardinality": candidate["cardinality"],
                    "condition": " AND ".join(pairs),
                    "metadata": {
                        "candidateId": candidate["id"],
                        "evidenceGrade": candidate["evidence_grade"],
                    },
                }
            )
    return result


@dataclass
class ModelingRepository:
    dsn: str | None = None

    def __post_init__(self):
        self.dsn = self.dsn if self.dsn is not None else os.getenv("METADATA_DATABASE_URL")
        self.memory_jobs = {}
        self.memory_packages = {}
        self.memory_candidates = {}

    def _connect(self):
        return psycopg.connect(self.dsn, autocommit=True)

    def save_job(self, job: dict[str, Any]):
        job = {**job, "updated_at": _now()}
        self.memory_jobs[job["id"]] = job
        if self.dsn:
            with self._connect() as connection:
                connection.execute(
                    """INSERT INTO semora_meta.datasource_onboarding_jobs
                    (id,datasource_id,kind,peer_datasource_id,domain,status,phase,progress,error,details,created_at,updated_at,completed_at)
                    VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s)
                    ON CONFLICT(id) DO UPDATE SET status=excluded.status,phase=excluded.phase,progress=excluded.progress,
                    error=excluded.error,details=excluded.details,updated_at=excluded.updated_at,completed_at=excluded.completed_at""",
                    (
                        job["id"],
                        job["datasource_id"],
                        job.get("kind", "onboarding"),
                        job.get("peer_datasource_id"),
                        job.get("domain"),
                        job["status"],
                        job["phase"],
                        job.get("progress", 0),
                        job.get("error"),
                        _json(job.get("details", {})),
                        job.get("created_at", _now()),
                        job["updated_at"],
                        job.get("completed_at"),
                    ),
                )
        return job

    def create_job(self, datasource_id: str, **values):
        return self.save_job(
            {
                "id": uuid4().hex,
                "datasource_id": datasource_id,
                "kind": values.get("kind", "onboarding"),
                "peer_datasource_id": values.get("peer_datasource_id"),
                "domain": values.get("domain"),
                "status": "queued",
                "phase": "queued",
                "progress": 0,
                "details": dict(values.get("details") or {}),
                "created_at": _now(),
            }
        )

    def get_job(self, job_id: str):
        if job_id in self.memory_jobs:
            return self.memory_jobs[job_id]
        if not self.dsn:
            raise KeyError(job_id)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT id,datasource_id,kind,peer_datasource_id,domain,status,phase,progress,error,details,created_at,updated_at,completed_at FROM semora_meta.datasource_onboarding_jobs WHERE id=%s",
                (job_id,),
            ).fetchone()
        if not row:
            raise KeyError(job_id)
        keys = (
            "id",
            "datasource_id",
            "kind",
            "peer_datasource_id",
            "domain",
            "status",
            "phase",
            "progress",
            "error",
            "details",
            "created_at",
            "updated_at",
            "completed_at",
        )
        value = dict(zip(keys, row, strict=True))
        self.memory_jobs[job_id] = value
        return value

    def latest_job(self, datasource_id: str, kind="onboarding"):
        if self.dsn:
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT id FROM semora_meta.datasource_onboarding_jobs WHERE datasource_id=%s AND kind=%s ORDER BY created_at DESC LIMIT 1",
                    (datasource_id, kind),
                ).fetchone()
            return self.get_job(row[0]) if row else None
        values = [
            v
            for v in self.memory_jobs.values()
            if v["datasource_id"] == datasource_id and v["kind"] == kind
        ]
        return sorted(values, key=lambda value: value["created_at"])[-1] if values else None

    def interrupted_jobs(self):
        if not self.dsn:
            return [job for job in self.memory_jobs.values() if job["status"] not in TERMINAL]
        with self._connect() as connection:
            ids = connection.execute(
                "SELECT id FROM semora_meta.datasource_onboarding_jobs WHERE status<>ALL(%s)",
                (list(TERMINAL),),
            ).fetchall()
        return [self.get_job(row[0]) for row in ids]

    def retire_pending(self, source_id, retained_ids):
        """Discard obsolete automatic proposals; explicit decisions survive rebuilds."""
        for identifier, item in list(self.memory_candidates.items()):
            if (item["source_datasource_id"] == source_id
                    and item["scope"] == "single_source" and item["status"] == "pending"
                    and not item.get("reviewed_by")
                    and identifier not in retained_ids):
                del self.memory_candidates[identifier]
        if self.dsn:
            with self._connect() as connection:
                connection.execute(
                    """DELETE FROM semora_meta.relationship_candidates
                    WHERE source_datasource_id=%s AND scope='single_source'
                    AND status='pending' AND reviewed_by IS NULL AND NOT (id=ANY(%s))""",
                    (source_id, list(retained_ids)),
                )

    def replace_candidates(
        self, candidates: list[dict[str, Any]], source_hash: str, target_hash: str
    ):
        existing_status = {
            identifier: value["status"]
            for identifier, value in self.memory_candidates.items()
            if value.get("status") == "rejected"
            or (
                value.get("status") == "accepted"
                and value.get("source_schema_hash") == source_hash
                and value.get("target_schema_hash") == target_hash
            )
        }
        if self.dsn and candidates:
            with self._connect() as connection:
                rows = connection.execute(
                    """SELECT id,status FROM semora_meta.relationship_candidates
                    WHERE id=ANY(%s) AND (status='rejected' OR
                    (status='accepted' AND source_schema_hash=%s AND target_schema_hash=%s))""",
                    ([item["id"] for item in candidates], source_hash, target_hash),
                ).fetchall()
            existing_status.update(
                {
                    identifier: status
                    for identifier, status in rows
                    if status in {"accepted", "rejected"}
                }
            )
        values = []
        for item in candidates:
            item = {
                **item,
                "source_schema_hash": source_hash,
                "target_schema_hash": target_hash,
                "profile_version": PROFILE_VERSION,
            }
            if item["id"] in existing_status:
                item["status"] = existing_status[item["id"]]
            self.memory_candidates[item["id"]] = item
            values.append(
                (
                    item["id"],
                    item["scope"],
                    item["source_datasource_id"],
                    item["target_datasource_id"],
                    item["source_table_id"],
                    item["target_table_id"],
                    _json(item["source_columns"]),
                    _json(item["target_columns"]),
                    _json(item["key_pairs"]),
                    _json(item["condition_template"]),
                    _json(item["evidence"]),
                    item["evidence_grade"],
                    item["confidence"],
                    item["cardinality"],
                    item["status"],
                    source_hash,
                    target_hash,
                    PROFILE_VERSION,
                    item.get("domain"),
                )
            )
        if self.dsn and values:
            with self._connect() as connection:
                with connection.cursor() as cursor:
                    cursor.executemany(
                        """INSERT INTO semora_meta.relationship_candidates
                        (id,scope,source_datasource_id,target_datasource_id,source_table_id,target_table_id,source_columns,target_columns,
                         key_pairs,condition_template,evidence,evidence_grade,confidence,cardinality,status,source_schema_hash,target_schema_hash,profile_version,domain)
                        VALUES(%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s::jsonb,%s::jsonb,%s::jsonb,%s,%s,%s,%s,%s,%s,%s,%s)
                        ON CONFLICT(id) DO UPDATE SET evidence=excluded.evidence,evidence_grade=excluded.evidence_grade,
                        confidence=excluded.confidence,status=CASE WHEN relationship_candidates.status='rejected' THEN relationship_candidates.status ELSE excluded.status END,
                        source_schema_hash=excluded.source_schema_hash,target_schema_hash=excluded.target_schema_hash,updated_at=now()""",
                        values,
                    )

    def mark_candidates_stale(self, datasource_id: str, schema_hash: str):
        for candidate in self.memory_candidates.values():
            if candidate.get("status") == "rejected":
                continue
            changed_source = (
                candidate["source_datasource_id"] == datasource_id
                and candidate.get("source_schema_hash") != schema_hash
            )
            changed_target = (
                candidate["target_datasource_id"] == datasource_id
                and candidate.get("target_schema_hash") != schema_hash
            )
            if changed_source or changed_target:
                candidate["status"] = "stale"
        if self.dsn:
            with self._connect() as connection:
                connection.execute(
                    """UPDATE semora_meta.relationship_candidates SET status='stale',updated_at=now()
                    WHERE status<>'rejected' AND ((source_datasource_id=%s AND source_schema_hash<>%s)
                       OR (target_datasource_id=%s AND target_schema_hash<>%s))""",
                    (datasource_id, schema_hash, datasource_id, schema_hash),
                )

    def list_candidates(self, source_id: str, scope: str | None = None, domain: str | None = None):
        if not self.dsn:
            return [
                v
                for v in self.memory_candidates.values()
                if v["source_datasource_id"] == source_id
                and (not scope or v["scope"] == scope)
                and (domain is None or v.get("domain") == domain)
            ]
        sql = """SELECT id,scope,source_datasource_id,target_datasource_id,source_table_id,target_table_id,source_columns,target_columns,
            key_pairs,condition_template,evidence,evidence_grade,confidence,cardinality,status,source_schema_hash,target_schema_hash,profile_version,domain
            FROM semora_meta.relationship_candidates WHERE source_datasource_id=%s"""
        params = [source_id]
        if scope:
            sql += " AND scope=%s"
            params.append(scope)
        if domain is not None:
            sql += " AND domain=%s"
            params.append(domain)
        with self._connect() as connection:
            rows = connection.execute(sql, params).fetchall()
        keys = (
            "id",
            "scope",
            "source_datasource_id",
            "target_datasource_id",
            "source_table_id",
            "target_table_id",
            "source_columns",
            "target_columns",
            "key_pairs",
            "condition_template",
            "evidence",
            "evidence_grade",
            "confidence",
            "cardinality",
            "status",
            "source_schema_hash",
            "target_schema_hash",
            "profile_version",
            "domain",
        )
        return [dict(zip(keys, row, strict=True)) for row in rows]

    def list_cross_candidates(self, left_id: str, right_id: str, domain: str):
        """Return both directions for one explicitly selected source pair."""
        if not self.dsn:
            return [
                value
                for value in self.memory_candidates.values()
                if value["scope"] == "cross_source"
                and value.get("domain") == domain
                and {value["source_datasource_id"], value["target_datasource_id"]}
                == {left_id, right_id}
            ]
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT id,scope,source_datasource_id,target_datasource_id,source_table_id,target_table_id,source_columns,target_columns,
                key_pairs,condition_template,evidence,evidence_grade,confidence,cardinality,status,source_schema_hash,target_schema_hash,profile_version,domain
                FROM semora_meta.relationship_candidates
                WHERE scope='cross_source' AND domain=%s
                AND ((source_datasource_id=%s AND target_datasource_id=%s)
                  OR (source_datasource_id=%s AND target_datasource_id=%s))""",
                (domain, left_id, right_id, right_id, left_id),
            ).fetchall()
        keys = (
            "id",
            "scope",
            "source_datasource_id",
            "target_datasource_id",
            "source_table_id",
            "target_table_id",
            "source_columns",
            "target_columns",
            "key_pairs",
            "condition_template",
            "evidence",
            "evidence_grade",
            "confidence",
            "cardinality",
            "status",
            "source_schema_hash",
            "target_schema_hash",
            "profile_version",
            "domain",
        )
        return [dict(zip(keys, row, strict=True)) for row in rows]

    def get_candidate(self, candidate_id: str):
        value = self.memory_candidates.get(candidate_id)
        if value is not None:
            return value
        if not self.dsn:
            raise KeyError(candidate_id)
        with self._connect() as connection:
            row = connection.execute(
                """SELECT source_datasource_id FROM semora_meta.relationship_candidates
                WHERE id=%s""",
                (candidate_id,),
            ).fetchone()
        if not row:
            raise KeyError(candidate_id)
        value = next(
            item for item in self.list_candidates(row[0]) if item["id"] == candidate_id
        )
        self.memory_candidates[candidate_id] = value
        return value

    def decide_candidate(self, candidate_id: str, status: str, reviewer: str = "", note: str = ""):
        if status not in {"accepted", "rejected", "pending"}:
            raise ValueError("候选关系状态无效")
        current = self.memory_candidates.get(candidate_id)
        if self.dsn:
            with self._connect() as connection:
                row = connection.execute(
                    """UPDATE semora_meta.relationship_candidates SET status=%s,reviewed_by=%s,
                    review_note=%s,updated_at=now() WHERE id=%s
                    RETURNING source_datasource_id""",
                    (status, reviewer or None, note[:2000] or None, candidate_id),
                ).fetchone()
            if not row:
                raise KeyError(candidate_id)
            values = self.list_candidates(row[0])
            current = next(item for item in values if item["id"] == candidate_id)
        elif current:
            current["status"] = status
        if not current:
            raise KeyError(candidate_id)
        self.memory_candidates[candidate_id] = current
        return current

    def save_package(
        self,
        datasource_id: str,
        schema_hash: str,
        payload: dict[str, Any],
        status: str,
        pending: int,
        error=None,
    ):
        value = {
            "datasource_id": datasource_id,
            "schema_hash": schema_hash,
            "status": status,
            "object_count": len(payload.get("objects", [])),
            "relation_count": len(payload.get("relations", [])),
            "pending_count": pending,
            "generator_version": GENERATOR_VERSION,
            "payload": payload,
            "error": error,
            "updated_at": _now(),
        }
        self.memory_packages[datasource_id] = value
        if self.dsn:
            with self._connect() as connection:
                connection.execute(
                    """INSERT INTO semora_meta.datasource_ontology_packages
                    (datasource_id,schema_hash,status,object_count,relation_count,pending_count,generator_version,payload,error)
                    VALUES(%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s) ON CONFLICT(datasource_id) DO UPDATE SET schema_hash=excluded.schema_hash,
                    status=excluded.status,object_count=excluded.object_count,relation_count=excluded.relation_count,pending_count=excluded.pending_count,
                    generator_version=excluded.generator_version,payload=excluded.payload,error=excluded.error,generated_at=now(),updated_at=now()""",
                    (
                        datasource_id,
                        schema_hash,
                        status,
                        value["object_count"],
                        value["relation_count"],
                        pending,
                        GENERATOR_VERSION,
                        _json(payload),
                        error,
                    ),
                )
        return value

    def get_package(self, datasource_id: str):
        if datasource_id in self.memory_packages:
            return self.memory_packages[datasource_id]
        if not self.dsn:
            raise KeyError(datasource_id)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT datasource_id,schema_hash,status,object_count,relation_count,pending_count,generator_version,payload,error,generated_at,updated_at FROM semora_meta.datasource_ontology_packages WHERE datasource_id=%s",
                (datasource_id,),
            ).fetchone()
        if not row:
            raise KeyError(datasource_id)
        keys = (
            "datasource_id",
            "schema_hash",
            "status",
            "object_count",
            "relation_count",
            "pending_count",
            "generator_version",
            "payload",
            "error",
            "generated_at",
            "updated_at",
        )
        value = dict(zip(keys, row, strict=True))
        self.memory_packages[datasource_id] = value
        return value

    def mark_package_stale(self, datasource_id: str, fresh_schema_hash: str):
        try:
            package = self.get_package(datasource_id)
        except KeyError:
            return
        if package["schema_hash"] == fresh_schema_hash:
            return
        package["status"] = "stale"
        package["updated_at"] = _now()
        self.memory_packages[datasource_id] = package
        if self.dsn:
            with self._connect() as connection:
                connection.execute(
                    """UPDATE semora_meta.datasource_ontology_packages
                    SET status='stale',updated_at=now() WHERE datasource_id=%s""",
                    (datasource_id,),
                )

    def save_column_profile(self, candidate: dict[str, Any], profile: dict[str, Any]):
        if not self.dsn:
            return
        statistics = {key: value for key, value in profile.items() if key != "signature"}
        rows = [
            (
                candidate["source_datasource_id"],
                candidate["source_table_id"],
                candidate["source_columns"],
                False,
            )
        ]
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.executemany(
                """INSERT INTO semora_meta.datasource_column_profiles
                (datasource_id,table_id,columns,profile_key,indexed,unique_key,statistics,signature,signature_version)
                VALUES(%s,%s,%s::jsonb,%s,true,%s,%s::jsonb,%s::jsonb,%s)
                ON CONFLICT(datasource_id,table_id,profile_key) DO UPDATE SET
                indexed=excluded.indexed,unique_key=excluded.unique_key,statistics=excluded.statistics,
                signature=excluded.signature,signature_version=excluded.signature_version,profiled_at=now()""",
                [
                    (
                        datasource_id,
                        table_id,
                        _json(columns),
                        hashlib.sha256(_json(columns).encode()).hexdigest(),
                        unique,
                        _json(statistics),
                        _json(profile.get("signature") or []),
                        profile.get("signature_version"),
                    )
                    for datasource_id, table_id, columns, unique in rows
                ],
            )


class ModelingService:
    def __init__(
        self,
        datasource_service,
        executor_request: Callable[..., Awaitable[Any]],
        repository: ModelingRepository | None = None,
        ontology_service=None,
    ):
        self.datasources = datasource_service
        self.executor_request = executor_request
        self.repository = repository or ModelingRepository()
        self.ontology = ontology_service
        self.tasks: dict[str, asyncio.Task] = {}
        self.source_locks: dict[str, asyncio.Lock] = {}

    def start(self, datasource_id: str, *, force=False, persisted_only=False):
        current = self.repository.latest_job(datasource_id)
        if current and current["status"] not in TERMINAL:
            return current
        job = self.repository.create_job(
            datasource_id, details={"persisted_only": persisted_only, "force": force}
        )
        self.tasks[job["id"]] = asyncio.create_task(self._run_onboarding(job, force=force))
        return job

    def resume(self):
        active_sources = set()
        for job in self.repository.interrupted_jobs():
            active_sources.add(job["datasource_id"])
            job.update(status="queued", phase="resuming", error=None)
            self.repository.save_job(job)
            if job.get("kind") == "cross_source":
                self.tasks[job["id"]] = asyncio.create_task(self._run_cross_source(job))
            elif job.get("details", {}).get("package_only"):
                self.tasks[job["id"]] = asyncio.create_task(self._run_package_rebuild(job))
            else:
                self.tasks[job["id"]] = asyncio.create_task(
                    self._run_onboarding(job, force=bool(job.get("details", {}).get("force")))
                )
        # Backfill already-scanned sources without touching the business database.
        for source in self.datasources.repository.list():
            if source.id in active_sources or not source.scan_result.get("tables"):
                continue
            try:
                package = self.repository.get_package(source.id)
                current_hash = tables_digest(source.scan_result["tables"])
                if (package["schema_hash"] == current_hash and package["status"] != "stale"
                        and package.get("generator_version") == GENERATOR_VERSION):
                    continue
            except KeyError:
                pass
            self.start(source.id, force=False, persisted_only=True)

    def _update(self, job, status, phase, progress, **extra):
        job.update(status=status, phase=phase, progress=progress, **extra)
        return self.repository.save_job(job)

    async def _profile(self, item, candidate, target_item=None):
        return await self.executor_request(
            "POST",
            "datasources/profile-relationship",
            json={
                "datasource": {
                    "datasource_id": item.id,
                    "type": item.type,
                    "secret_ref": item.secret_ref,
                },
                "target_datasource": {
                    "datasource_id": target_item.id,
                    "type": target_item.type,
                    "secret_ref": target_item.secret_ref,
                }
                if target_item
                else None,
                "source_table": candidate["source_table_id"],
                "source_columns": candidate["source_columns"],
                "target_table": candidate["target_table_id"],
                "target_columns": candidate["target_columns"],
                "sample_rows": 200 if target_item else 100,
                "source_index_confirmed": True,
                "target_unique_index_confirmed": True,
            },
        )

    async def _semantic_review(self, candidates, table_maps):
        """Review bounded candidate packets; model never writes physical mappings."""
        try:
            from semora_agent.model import structured_model
        except ImportError:
            return
        if not structured_model.configured:
            return
        system = """角色：数据关系语义核对员。
任务：判断候选是否构成合理的实体引用或身份映射，并判断基数及目标角色。
要求：字段同名、值相同不能证明业务关联。区分实体引用(reference)、身份映射(identity)、数据副本(replica)、无法判断(unknown)。同一字段允许关联多个目标，仅在目标互相替代且证据不能区分时标记 alternative_unresolved=true。不得新增表、字段或SQL；证据不足时 semantic_supported=false。
输出 JSON：{"decisions":[{"candidate_id":"...","semantic_supported":true,"target_role":"reference","alternative_unresolved":false,"cardinality":"N:1","confidence":0.9,"reason":"简短原因"}]}。"""
        reviewable = sorted(
            candidates,
            key=lambda item: (
                not any(e["kind"] == "explicit_field_reference" for e in item["evidence"]),
                not any(e["kind"] == "value_profile" and e.get("statistics", {}).get("source_nonnull_distinct_keys", 0) >= 100 for e in item["evidence"]),
                -item["confidence"]),
        )[:60]
        for candidate in candidates:
            if candidate not in reviewable:
                candidate["evidence"].append(
                    {"kind": "model_review_deferred", "reason": "initial_budget"}
                )
        for start in range(0, len(reviewable), 15):
            batch = reviewable[start : start + 15]
            packet = []
            for candidate in batch:
                source = table_maps[candidate["source_datasource_id"]].get(
                    candidate["source_table_id"], {}
                )
                target = table_maps[candidate["target_datasource_id"]].get(
                    candidate["target_table_id"], {}
                )
                source_columns = {item.get("name"): item for item in source.get("columns", [])}
                target_columns = {item.get("name"): item for item in target.get("columns", [])}
                packet.append(
                    {
                        "candidate_id": candidate["id"],
                        "source": {
                            "table": source.get("name"),
                            "description": source.get("description"),
                            "fields": [
                                {
                                    "name": name,
                                    "description": source_columns.get(name, {}).get("description")
                                    or source_columns.get(name, {}).get("comment"),
                                }
                                for name in candidate["source_columns"]
                            ],
                        },
                        "target": {
                            "table": target.get("name"),
                            "description": target.get("description"),
                            "fields": [
                                {
                                    "name": name,
                                    "description": target_columns.get(name, {}).get("description")
                                    or target_columns.get(name, {}).get("comment"),
                                }
                                for name in candidate["target_columns"]
                            ],
                        },
                        "program_evidence": candidate["evidence"],
                        "other_targets": [{"table": c["target_table_id"], "fields": c["target_columns"]}
                                          for c in candidates if c["source_datasource_id"] == candidate["source_datasource_id"]
                                          and c["source_table_id"] == candidate["source_table_id"]
                                          and c["source_columns"] == candidate["source_columns"]
                                          and c["id"] != candidate["id"]][:3],
                    }
                )
            try:
                response = await structured_model.complete_json(
                    system, "请核对这些候选：" + _json(packet)
                )
            except Exception as exc:
                for candidate in batch:
                    candidate["evidence"].append(
                        {"kind": "model_review_unavailable", "reason": type(exc).__name__}
                    )
                continue
            decisions = {
                str(item.get("candidate_id")): item
                for item in response.get("decisions", [])
                if isinstance(item, dict)
            }
            for candidate in batch:
                decision = decisions.get(candidate["id"])
                if not decision:
                    continue
                confidence = decision.get("confidence", 0)
                if not isinstance(confidence, (int, float)):
                    confidence = 0
                candidate["evidence"].append(
                    {
                        "kind": "model_semantic_review",
                        "supported": decision.get("semantic_supported") is True,
                        "confidence": max(0, min(1, float(confidence))),
                        "reason": str(decision.get("reason") or "")[:300],
                        "target_role": decision.get("target_role") if decision.get("target_role") in
                                       {"reference", "identity", "replica", "unknown"} else "unknown",
                        "alternative_unresolved": decision.get("alternative_unresolved") is not False,
                    }
                )
                if decision.get("cardinality") in {"1:1", "1:N", "N:1", "N:N"}:
                    candidate["cardinality"] = decision["cardinality"]
                if (
                    not decision.get("semantic_supported")
                    and candidate["evidence_grade"] != "foreign_key"
                ):
                    candidate["status"] = "pending"
                    candidate["evidence_grade"] = "weak"

    async def _run_onboarding(self, job, force=False):
        identifier = job["datasource_id"]
        persisted_only = bool(job.get("details", {}).get("persisted_only"))
        lock = self.source_locks.setdefault(identifier, asyncio.Lock())
        async with lock:
            try:
                item = self.datasources.repository.get(identifier)
                self._update(job, "testing", "testing", 8)
                if not persisted_only:
                    if force or item.status != "healthy":
                        await self.datasources.test(identifier)
                    self._update(job, "probing", "probing", 18)
                    try:
                        from .capability_service import validate_cached

                        await validate_cached(
                            item, self.datasources.repository, self.executor_request, force=force
                        )
                    except Exception as exc:
                        job.setdefault("details", {})["capability_warning"] = type(exc).__name__
                else:
                    self._update(job, "probing", "using_persisted_metadata", 18)
                self._update(job, "scanning", "scanning", 32)
                if persisted_only and not item.scan_result.get("tables"):
                    raise ValueError("缺少已持久化的表结构，不能离线回填单源本体包")
                if not persisted_only and (force or not item.scan_result.get("tables")):
                    result = await self.executor_request(
                        "POST",
                        "datasources/scan",
                        json={
                            "datasource_id": item.id,
                            "type": item.type,
                            "secret_ref": item.secret_ref,
                            "execution_id": job["id"],
                        },
                    )
                    item = self.datasources.repository.record_scan(identifier, result)
                tables = item.scan_result.get("tables") or []
                schema_hash = tables_digest(tables)
                try:
                    old_package = self.repository.get_package(identifier)
                except KeyError:
                    old_package = None
                self.repository.mark_package_stale(identifier, schema_hash)
                self.repository.mark_candidates_stale(identifier, schema_hash)
                self._update(
                    job,
                    "discovering",
                    "discovering",
                    55,
                    details={
                        **job.get("details", {}),
                        "schema_hash": schema_hash,
                        "tables": len(tables),
                    },
                )
                candidates = discover_candidates(
                    identifier,
                    tables,
                    declared_relationships=item.scan_result.get("foreign_keys") or [],
                )
                self.repository.retire_pending(identifier, {c["id"] for c in candidates})
                self.repository.replace_candidates(candidates, schema_hash, schema_hash)
                self._update(job, "profiling", "profiling", 68)
                started = time.monotonic()
                table_map = {table["id"]: table for table in tables}
                for candidate in candidates:
                    if persisted_only:
                        continue
                    if candidate["status"] == "accepted" or time.monotonic() - started >= 60:
                        continue
                    source = table_map[candidate["source_table_id"]]
                    target = table_map[candidate["target_table_id"]]
                    if not _indexed(source, candidate["source_columns"]) or not _indexed(
                        target, candidate["target_columns"], unique=True
                    ):
                        continue
                    try:
                        profile = await self._profile(item, candidate)
                        self.repository.save_column_profile(candidate, profile)
                        candidate["evidence"].append(
                            {
                                "kind": "value_profile",
                                "statistics": {
                                    k: v for k, v in profile.items() if k != "signature"
                                },
                            }
                        )
                    except Exception as exc:
                        candidate["evidence"].append(
                            {"kind": "profile_unavailable", "reason": type(exc).__name__}
                        )
                await self._semantic_review(candidates, {identifier: table_map})
                finalize_relationships(candidates)
                self.repository.replace_candidates(candidates, schema_hash, schema_hash)
                self._update(job, "building", "building", 88)
                current_candidates = [
                    candidate
                    for candidate in self.repository.list_candidates(identifier, "single_source")
                    if candidate.get("source_schema_hash") == schema_hash
                    and candidate.get("target_schema_hash") == schema_hash
                    and candidate.get("status") != "stale"
                ]
                objects, mapping = package_objects(identifier, tables)
                relations = package_relations(current_candidates, mapping)
                pending = sum(
                    candidate["status"] == "pending" for candidate in current_candidates
                )
                payload = {
                    "datasourceId": identifier,
                    "schemaHash": schema_hash,
                    "objects": objects,
                    "relations": relations,
                    "evidenceProviders": {"catalog": "enabled", "source_code": "reserved"},
                }
                package_status = "ready_with_pending" if pending else "ready"
                package = self.repository.save_package(
                    identifier, schema_hash, payload, package_status, pending
                )
                if self.ontology:
                    await self._sync_bound_package(
                        identifier, package, old_package, objects, relations
                    )
                self._update(
                    job,
                    "completed_with_pending" if pending else "completed",
                    "complete",
                    100,
                    details={
                        **job.get("details", {}),
                        "package_status": package_status,
                        "objects": package["object_count"],
                        "relations": package["relation_count"],
                        "pending": pending,
                    },
                    completed_at=_now(),
                )
            except Exception as exc:
                self._update(
                    job,
                    "failed",
                    "failed",
                    job.get("progress", 0),
                    error=str(exc)[:1000],
                    completed_at=_now(),
                )
            finally:
                self.tasks.pop(job["id"], None)

    async def _sync_bound_package(self, identifier, package, old_package, objects, relations):
        """Refresh materialized effective ontologies and invalidate generated cross-source edges."""
        old_names = {
            item.get("name")
            for item in (old_package or {}).get("payload", {}).get("objects", [])
            if item.get("name")
        }
        for summary in await self.ontology.list_drafts():
            configuration = await self.ontology.get_configuration(summary["domain"])
            refs = {
                ref.get("dataSourceId"): ref
                for ref in configuration.get("sourcePackageRefs", [])
                if ref.get("dataSourceId")
            }
            if identifier not in refs:
                continue
            kept_objects = [
                item
                for item in configuration.get("objects", [])
                if item.get("metadata", {}).get("sourcePackageId") != identifier
                and item.get("name") not in old_names
            ]
            kept_relations = []
            for relation in configuration.get("relations", []):
                metadata = relation.get("metadata", {})
                generated_for_source = metadata.get("sourcePackageId") == identifier
                generated_cross_edge = bool(metadata.get("candidateId")) and (
                    relation.get("from") in old_names or relation.get("to") in old_names
                )
                if not generated_for_source and not generated_cross_edge:
                    kept_relations.append(relation)
            tagged_relations = [
                {
                    **relation,
                    "metadata": {
                        **relation.get("metadata", {}),
                        "sourcePackageId": identifier,
                    },
                }
                for relation in relations
            ]
            refs[identifier] = {
                "dataSourceId": identifier,
                "schemaHash": package["schema_hash"],
                "version": package["generator_version"],
            }
            configuration.update(
                objects=kept_objects + objects,
                relations=kept_relations + tagged_relations,
                sourcePackageRefs=list(refs.values()),
            )
            await self.ontology.save_configuration(summary["domain"], configuration)

    def start_cross_source(self, domain: str, left_id: str, right_id: str):
        if left_id == right_id:
            raise ValueError("请选择两个不同的数据源")
        for identifier in (left_id, right_id):
            source = self.datasources.repository.get(identifier)
            package = self.repository.get_package(identifier)
            tables = source.scan_result.get("tables") or []
            if (
                source.status != "healthy"
                or not tables
                or package["schema_hash"] != tables_digest(tables)
                or package["status"] not in {"ready", "ready_with_pending"}
            ):
                raise ValueError("数据源尚未完成单源建模")
        job = self.repository.create_job(
            left_id, kind="cross_source", peer_datasource_id=right_id, domain=domain
        )
        self.tasks[job["id"]] = asyncio.create_task(self._run_cross_source(job))
        return job

    async def decide_candidate(self, candidate_id: str, status: str, reviewer="", note=""):
        candidate = self.repository.decide_candidate(candidate_id, status, reviewer, note)
        if candidate["scope"] == "single_source":
            source = self.datasources.repository.get(candidate["source_datasource_id"])
            package = self.repository.get_package(source.id)
            candidates = [
                item for item in self.repository.list_candidates(source.id, "single_source")
                if item.get("source_schema_hash") == package["schema_hash"]
                and item.get("target_schema_hash") == package["schema_hash"]
                and item.get("status") != "stale"
            ]
            objects, mapping = package_objects(source.id, source.scan_result.get("tables") or [])
            relations = package_relations(candidates, mapping)
            pending = sum(item["status"] == "pending" for item in candidates)
            payload = {**package["payload"], "objects": objects, "relations": relations}
            self.repository.save_package(
                source.id,
                package["schema_hash"],
                payload,
                "ready_with_pending" if pending else "ready",
                pending,
            )
            if self.ontology:
                for summary in await self.ontology.list_drafts():
                    configuration = await self.ontology.get_configuration(summary["domain"])
                    if source.id not in {
                        ref.get("dataSourceId")
                        for ref in configuration.get("sourcePackageRefs", [])
                    }:
                        continue
                    current = {
                        relation["name"]: relation
                        for relation in configuration.get("relations", [])
                        if relation.get("metadata", {}).get("sourcePackageId") != source.id
                    }
                    for relation in relations:
                        relation = {
                            **relation,
                            "metadata": {
                                **relation.get("metadata", {}),
                                "sourcePackageId": source.id,
                            },
                        }
                        current[relation["name"]] = relation
                    configuration["relations"] = list(current.values())
                    await self.ontology.save_configuration(summary["domain"], configuration)
        elif status == "accepted" and candidate.get("domain") and self.ontology:
            await self._apply_cross_relations(candidate["domain"], [candidate])
        return candidate

    def start_package_rebuild(self, datasource_id: str):
        job = self.repository.create_job(datasource_id, details={"package_only": True})
        self.tasks[job["id"]] = asyncio.create_task(self._run_package_rebuild(job))
        return job

    async def _run_package_rebuild(self, job):
        identifier = job["datasource_id"]
        lock = self.source_locks.setdefault(identifier, asyncio.Lock())
        async with lock:
            try:
                self._update(job, "building", "building", 25)
                source = self.datasources.repository.get(identifier)
                package = self.repository.get_package(identifier)
                tables = source.scan_result.get("tables") or []
                schema_hash = tables_digest(tables)
                if package["schema_hash"] != schema_hash:
                    raise ValueError("数据结构已变化，请先完成重新扫描")
                candidates = [
                    item for item in self.repository.list_candidates(identifier, "single_source")
                    if item.get("source_schema_hash") == schema_hash
                    and item.get("target_schema_hash") == schema_hash
                    and item.get("status") != "stale"
                ]
                objects, mapping = package_objects(identifier, tables)
                relations = package_relations(candidates, mapping)
                pending = sum(item["status"] == "pending" for item in candidates)
                payload = {**package["payload"], "objects": objects, "relations": relations}
                updated = self.repository.save_package(
                    identifier,
                    schema_hash,
                    payload,
                    "ready_with_pending" if pending else "ready",
                    pending,
                )
                self._update(job, "building", "building", 75)
                if self.ontology:
                    await self._sync_bound_package(
                        identifier, updated, package, objects, relations
                    )
                self._update(
                    job, "completed_with_pending" if pending else "completed", "complete", 100,
                    details={"package_only": True, "relations": len(relations), "pending": pending},
                    completed_at=_now(),
                )
            except Exception as exc:
                self._update(
                    job, "failed", "failed", job.get("progress", 0),
                    error=str(exc)[:1000], completed_at=_now(),
                )
            finally:
                self.tasks.pop(job["id"], None)

    async def _apply_cross_relations(self, domain: str, candidates: list[dict[str, Any]]):
        configuration = await self.ontology.get_configuration(domain)
        objects = {
            (
                str(obj.get("dataSourceId") or configuration.get("dataSourceId") or ""),
                str(obj.get("metadata", {}).get("sourceTableId")),
            ): obj
            for obj in configuration.get("objects", [])
        }
        relations = {relation["name"]: relation for relation in configuration.get("relations", [])}
        for candidate in candidates:
            if candidate["status"] != "accepted":
                continue
            source_object = objects.get(
                (candidate["source_datasource_id"], candidate["source_table_id"])
            )
            target_object = objects.get(
                (candidate["target_datasource_id"], candidate["target_table_id"])
            )
            if not source_object or not target_object:
                continue
            source_fields = {
                prop.get("column"): prop.get("name") for prop in source_object.get("properties", [])
            }
            target_fields = {
                prop.get("column"): prop.get("name") for prop in target_object.get("properties", [])
            }
            if any(
                pair["source"] not in source_fields or pair["target"] not in target_fields
                for pair in candidate["key_pairs"]
            ):
                continue
            relations["r_" + candidate["id"]] = {
                "name": "r_" + candidate["id"],
                "label": "自动发现的跨源关联",
                "from": source_object["name"],
                "to": target_object["name"],
                "joinType": "many_to_one",
                "cardinality": candidate["cardinality"],
                "condition": " AND ".join(
                    f"{source_object['name']}.{source_fields[pair['source']]} = "
                    f"{target_object['name']}.{target_fields[pair['target']]}"
                    for pair in candidate["key_pairs"]
                ),
                "metadata": {
                    "candidateId": candidate["id"],
                    "evidenceGrade": candidate["evidence_grade"],
                },
            }
        configuration["relations"] = list(relations.values())
        await self.ontology.save_configuration(domain, configuration)

    async def _run_cross_source(self, job):
        try:
            left = self.datasources.repository.get(job["datasource_id"])
            right = self.datasources.repository.get(job["peer_datasource_id"])
            left_package = self.repository.get_package(left.id)
            right_package = self.repository.get_package(right.id)
            self._update(job, "discovering", "discovering", 25)
            candidates = discover_candidates(
                left.id,
                left.scan_result.get("tables") or [],
                right.id,
                right.scan_result.get("tables") or [],
                job["domain"],
            )
            candidates += discover_candidates(
                right.id,
                right.scan_result.get("tables") or [],
                left.id,
                left.scan_result.get("tables") or [],
                job["domain"],
            )
            self._update(job, "profiling", "profiling", 55)
            lmap = {table["id"]: table for table in left.scan_result.get("tables") or []}
            rmap = {table["id"]: table for table in right.scan_result.get("tables") or []}
            started = time.monotonic()
            for candidate in candidates:
                source_map, target_map = (
                    (lmap, rmap) if candidate["source_datasource_id"] == left.id else (rmap, lmap)
                )
                source = source_map[candidate["source_table_id"]]
                target = target_map[candidate["target_table_id"]]
                if (
                    time.monotonic() - started >= 60
                    or not _indexed(source, candidate["source_columns"])
                    or not _indexed(target, candidate["target_columns"], unique=True)
                ):
                    continue
                try:
                    source_item, target_item = (
                        (left, right)
                        if candidate["source_datasource_id"] == left.id
                        else (right, left)
                    )
                    profile = await self._profile(source_item, candidate, target_item)
                    self.repository.save_column_profile(candidate, profile)
                    candidate["evidence"].append(
                        {
                            "kind": "value_profile",
                            "statistics": {k: v for k, v in profile.items() if k != "signature"},
                        }
                    )
                except Exception as exc:
                    candidate["evidence"].append(
                        {"kind": "profile_unavailable", "reason": type(exc).__name__}
                    )
            await self._semantic_review(candidates, {left.id: lmap, right.id: rmap})
            finalize_relationships(candidates, cross_source=True)
            self.repository.replace_candidates(
                candidates, left_package["schema_hash"], right_package["schema_hash"]
            )
            if self.ontology and any(item["status"] == "accepted" for item in candidates):
                await self._apply_cross_relations(job["domain"], candidates)
            pending = sum(item["status"] == "pending" for item in candidates)
            self._update(
                job,
                "completed_with_pending" if pending else "completed",
                "complete",
                100,
                details={
                    "candidates": len(candidates),
                    "accepted": sum(item["status"] == "accepted" for item in candidates),
                    "pending": pending,
                },
                completed_at=_now(),
            )
        except Exception as exc:
            self._update(
                job,
                "failed",
                "failed",
                job.get("progress", 0),
                error=str(exc)[:1000],
                completed_at=_now(),
            )
        finally:
            self.tasks.pop(job["id"], None)
