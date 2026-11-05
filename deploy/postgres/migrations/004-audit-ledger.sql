BEGIN;

ALTER TABLE semora_meta.audit_events
    ADD COLUMN IF NOT EXISTS event_type VARCHAR(100),
    ADD COLUMN IF NOT EXISTS actor VARCHAR(200),
    ADD COLUMN IF NOT EXISTS trace_id VARCHAR(64),
    ADD COLUMN IF NOT EXISTS previous_hash CHAR(64);

UPDATE semora_meta.audit_events
SET event_type = payload->>'event_type',
    actor = payload->>'actor',
    trace_id = payload->>'trace_id',
    previous_hash = payload->>'previous_hash'
WHERE event_type IS NULL
   OR actor IS NULL
   OR trace_id IS NULL
   OR previous_hash IS NULL;

ALTER TABLE semora_meta.audit_events
    ALTER COLUMN event_type SET NOT NULL,
    ALTER COLUMN actor SET NOT NULL,
    ALTER COLUMN trace_id SET NOT NULL,
    ALTER COLUMN previous_hash SET NOT NULL;

ALTER TABLE semora_meta.audit_events
    DROP CONSTRAINT IF EXISTS audit_events_event_hash_format,
    DROP CONSTRAINT IF EXISTS audit_events_previous_hash_format;

ALTER TABLE semora_meta.audit_events
    ADD CONSTRAINT audit_events_event_hash_format CHECK (event_hash ~ '^[0-9a-f]{64}$'),
    ADD CONSTRAINT audit_events_previous_hash_format CHECK (previous_hash ~ '^[0-9a-f]{64}$');

CREATE OR REPLACE FUNCTION semora_meta.validate_audit_append()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    prior_sequence INTEGER;
    prior_hash CHAR(64);
BEGIN
    PERFORM pg_advisory_xact_lock(hashtextextended(NEW.run_id, 0));

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

CREATE OR REPLACE FUNCTION semora_meta.reject_audit_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'audit ledger is append-only';
END;
$$;

DROP TRIGGER IF EXISTS audit_events_validate_append ON semora_meta.audit_events;
CREATE TRIGGER audit_events_validate_append
BEFORE INSERT ON semora_meta.audit_events
FOR EACH ROW EXECUTE FUNCTION semora_meta.validate_audit_append();

DROP TRIGGER IF EXISTS audit_events_reject_mutation ON semora_meta.audit_events;
CREATE TRIGGER audit_events_reject_mutation
BEFORE UPDATE OR DELETE ON semora_meta.audit_events
FOR EACH ROW EXECUTE FUNCTION semora_meta.reject_audit_mutation();

REVOKE UPDATE, DELETE, TRUNCATE ON semora_meta.audit_events FROM :"api_role";
GRANT SELECT, INSERT ON semora_meta.audit_events TO :"api_role";

COMMIT;
