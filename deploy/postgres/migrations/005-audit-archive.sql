BEGIN;

CREATE TABLE IF NOT EXISTS semora_meta.audit_archives (
    run_id VARCHAR(64) PRIMARY KEY REFERENCES semora_meta.runs(id),
    event_count INTEGER NOT NULL CHECK (event_count > 0),
    terminal_event_hash CHAR(64) NOT NULL CHECK (terminal_event_hash ~ '^[0-9a-f]{64}$'),
    content_hash CHAR(64) NOT NULL CHECK (content_hash ~ '^[0-9a-f]{64}$'),
    object_uri TEXT NOT NULL CHECK (object_uri LIKE 's3://%'),
    retention_until TIMESTAMPTZ,
    kafka_topic VARCHAR(249),
    kafka_partition INTEGER,
    kafka_offset BIGINT,
    exported_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE OR REPLACE FUNCTION semora_meta.reject_audit_archive_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'audit archive registry is append-only';
END;
$$;

DROP TRIGGER IF EXISTS audit_archives_reject_mutation ON semora_meta.audit_archives;
CREATE TRIGGER audit_archives_reject_mutation
BEFORE UPDATE OR DELETE ON semora_meta.audit_archives
FOR EACH ROW EXECUTE FUNCTION semora_meta.reject_audit_archive_mutation();

CREATE OR REPLACE FUNCTION semora_meta.validate_audit_append()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    prior_sequence INTEGER;
    prior_hash CHAR(64);
    persisted_status VARCHAR(32);
BEGIN
    PERFORM pg_advisory_xact_lock(hashtextextended(NEW.run_id, 0));

    SELECT status INTO persisted_status
    FROM semora_meta.runs
    WHERE id = NEW.run_id;
    IF persisted_status IN ('completed', 'failed') THEN
        RAISE EXCEPTION 'cannot append audit events after run % is terminal', NEW.run_id;
    END IF;

    SELECT sequence, event_hash
    INTO prior_sequence, prior_hash
    FROM semora_meta.audit_events
    WHERE run_id = NEW.run_id
    ORDER BY sequence DESC
    LIMIT 1;

    IF NEW.sequence <> COALESCE(prior_sequence, 0) + 1 THEN
        RAISE EXCEPTION 'audit sequence must be contiguous for run %', NEW.run_id;
    END IF;
    IF NEW.previous_hash <> COALESCE(prior_hash, repeat('0', 64)) THEN
        RAISE EXCEPTION 'audit previous hash mismatch for run %', NEW.run_id;
    END IF;
    IF NEW.payload->>'run_id' <> NEW.run_id
       OR (NEW.payload->>'sequence')::INTEGER <> NEW.sequence
       OR NEW.payload->>'event_type' <> NEW.event_type
       OR NEW.payload->>'actor' <> NEW.actor
       OR NEW.payload->>'trace_id' <> NEW.trace_id
       OR NEW.payload->>'previous_hash' <> NEW.previous_hash
       OR NEW.payload->>'event_hash' <> NEW.event_hash THEN
        RAISE EXCEPTION 'audit payload does not match immutable columns';
    END IF;
    RETURN NEW;
END;
$$;

REVOKE ALL ON semora_meta.audit_archives FROM :"api_role";
GRANT SELECT ON semora_meta.audit_archives TO :"api_role";

REVOKE ALL ON semora_meta.audit_archives FROM :"audit_exporter_role";
GRANT SELECT, INSERT ON semora_meta.audit_archives TO :"audit_exporter_role";
GRANT SELECT ON semora_meta.audit_events, semora_meta.runs TO :"audit_exporter_role";
GRANT USAGE ON SCHEMA semora_meta TO :"audit_exporter_role";

COMMIT;
