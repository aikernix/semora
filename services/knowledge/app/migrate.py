from __future__ import annotations

import mimetypes
import os
from pathlib import Path

from .main import db, initialize_database, seed_documents
from .object_store import object_store


def migrate_legacy_sources() -> int:
    if object_store.mode != "s3":
        return 0
    with db() as connection, connection.cursor() as cursor:
        cursor.execute(
            """SELECT id,file_name,source_path FROM knowledge_core.documents
            WHERE source_path NOT LIKE 's3://%' ORDER BY created_at"""
        )
        documents = cursor.fetchall()
    migrated = 0
    for document in documents:
        source = Path(document["source_path"])
        if not source.is_file():
            raise RuntimeError(f"legacy source is missing: {document['id']} / {source.name}")
        source_uri = object_store.put_file(
            str(document["id"]), source, mimetypes.guess_type(document["file_name"])[0]
        )
        try:
            with db(autocommit=False) as connection, connection.cursor() as cursor:
                cursor.execute(
                    """UPDATE knowledge_core.documents SET source_path=%s
                    WHERE id=%s AND source_path=%s""",
                    (source_uri, document["id"], document["source_path"]),
                )
                if cursor.rowcount != 1:
                    raise RuntimeError("document source changed during object migration")
        except Exception:
            object_store.delete(source_uri)
            raise
        migrated += 1
    return migrated


def run() -> None:
    initialize_database()
    if os.getenv("SEMORA_DEMO_SEED", "false").lower() == "true":
        seed_documents()
    migrate_legacy_sources()


if __name__ == "__main__":
    run()
