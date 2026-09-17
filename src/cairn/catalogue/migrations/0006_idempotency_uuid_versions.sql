-- I-27 permits a caller to supply a deterministic UUIDv5 idempotency key.
-- The original foundation constraint predated that decision and pinned the
-- key's version nibble to 4, conflating a caller-supplied replay identity with
-- Cairn-assigned identities. SQLite cannot alter a CHECK constraint, so the
-- table is rebuilt. Every column, row, index, trigger, and other constraint is
-- preserved; only the idempotency-key version pin is removed.
CREATE TABLE idempotency_records_migration AS
SELECT principal_id, operation, idempotency_key, command_digest, result_schema,
       result_bytes, result_digest, mutation_id, original_event_id, created_at
FROM idempotency_records;

DROP TABLE idempotency_records;

CREATE TABLE idempotency_records (
    principal_id TEXT NOT NULL,
    operation TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    command_digest BLOB NOT NULL,
    result_schema TEXT NOT NULL,
    result_bytes BLOB NOT NULL,
    result_digest BLOB NOT NULL,
    mutation_id TEXT NOT NULL,
    original_event_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    CONSTRAINT pk_idempotency_records PRIMARY KEY (
        principal_id,
        operation,
        idempotency_key
    ),
    CONSTRAINT fk_idempotency_records_event FOREIGN KEY (original_event_id)
        REFERENCES audit_events(event_id),
    CONSTRAINT ck_idempotency_records_principal_id CHECK (
        length(principal_id) = 36
        AND principal_id GLOB '????????-????-4???-[89ab]???-????????????'
        AND principal_id NOT GLOB '*[^0-9a-f-]*'
    ),
    CONSTRAINT ck_idempotency_records_operation CHECK (
        length(operation) BETWEEN 1 AND 63
        AND substr(operation, 1, 1) GLOB '[a-z]'
        AND substr(operation, -1, 1) GLOB '[a-z0-9]'
        AND operation NOT GLOB '*[^a-z0-9-]*'
    ),
    CONSTRAINT ck_idempotency_records_key CHECK (
        length(idempotency_key) = 36
        AND idempotency_key GLOB '????????-????-????-[89ab]???-????????????'
        AND idempotency_key NOT GLOB '*[^0-9a-f-]*'
    ),
    CONSTRAINT ck_idempotency_records_command_digest CHECK (
        typeof(command_digest) = 'blob' AND length(command_digest) = 32
    ),
    CONSTRAINT ck_idempotency_records_result_schema CHECK (
        length(result_schema) BETWEEN 1 AND 127
        AND substr(result_schema, 1, 1) GLOB '[a-z]'
        AND result_schema NOT GLOB '*[^a-z0-9._/-]*'
    ),
    CONSTRAINT ck_idempotency_records_result_bytes CHECK (
        typeof(result_bytes) = 'blob' AND length(result_bytes) > 0
    ),
    CONSTRAINT ck_idempotency_records_result_digest CHECK (
        typeof(result_digest) = 'blob' AND length(result_digest) = 32
    ),
    CONSTRAINT ck_idempotency_records_mutation_id CHECK (
        length(mutation_id) = 36
        AND mutation_id GLOB '????????-????-4???-[89ab]???-????????????'
        AND mutation_id NOT GLOB '*[^0-9a-f-]*'
    ),
    CONSTRAINT ck_idempotency_records_original_event_id CHECK (
        length(original_event_id) = 36
        AND original_event_id GLOB '????????-????-4???-[89ab]???-????????????'
        AND original_event_id NOT GLOB '*[^0-9a-f-]*'
    ),
    CONSTRAINT ck_idempotency_records_created_at CHECK (
        length(created_at) = 27
        AND created_at GLOB '????-??-??T??:??:??.??????Z'
        AND created_at NOT GLOB '*[^0-9TZ:.-]*'
    )
) STRICT;

INSERT INTO idempotency_records (
    principal_id, operation, idempotency_key, command_digest, result_schema,
    result_bytes, result_digest, mutation_id, original_event_id, created_at
)
SELECT principal_id, operation, idempotency_key, command_digest, result_schema,
       result_bytes, result_digest, mutation_id, original_event_id, created_at
FROM idempotency_records_migration;

DROP TABLE idempotency_records_migration;

CREATE UNIQUE INDEX uq_idempotency_records_mutation_id
ON idempotency_records(mutation_id);

CREATE TRIGGER trg_idempotency_records_no_update
BEFORE UPDATE ON idempotency_records
BEGIN
    SELECT RAISE(ABORT, 'immutable_idempotency_record');
END;

CREATE TRIGGER trg_idempotency_records_no_delete
BEFORE DELETE ON idempotency_records
BEGIN
    SELECT RAISE(ABORT, 'immutable_idempotency_record');
END;
