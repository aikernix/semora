"""Explicit legacy archive/cleanup. Defaults to preview; never publishes ontologies."""
import argparse
import json
import os
from pathlib import Path

import psycopg


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--archive', type=Path, required=True)
    p.add_argument('--apply', action='store_true')
    args = p.parse_args()
    with psycopg.connect(os.environ['METADATA_DATABASE_URL'], row_factory=psycopg.rows.dict_row) as conn:
        with conn.transaction():
            if args.apply:
                conn.execute('LOCK TABLE semora_meta.relationship_candidates IN SHARE ROW EXCLUSIVE MODE')
            rows = conn.execute('SELECT * FROM semora_meta.relationship_candidates ORDER BY id').fetchall()
            if not args.apply:
                print(json.dumps({'preview': True, 'legacy_records': len(rows), 'archive': str(args.archive)}))
                return
            missing = conn.execute('''SELECT datasource_id FROM semora_meta.datasource_ontology_packages
                WHERE NOT payload ? 'relationshipRecords' ''').fetchall()
            if missing:
                raise RuntimeError('Run migration 013 and verify active package records before cleanup')
            # Exclusive creation prevents accidentally overwriting the only archive.
            with args.archive.open('x') as target:
                args.archive.chmod(0o600)
                json.dump(rows, target, ensure_ascii=False, default=str)
                target.flush()
                os.fsync(target.fileno())
            conn.execute('DELETE FROM semora_meta.relationship_candidates')
            conn.execute("UPDATE semora_meta.datasource_onboarding_jobs SET details=details-'query_table_roles'-'coverage'")
    print(json.dumps({'archived_and_removed':len(rows)}))


if __name__ == '__main__':
    main()
