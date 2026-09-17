CREATE TABLE catalogue_metadata (
    singleton INTEGER NOT NULL PRIMARY KEY,
    format TEXT NOT NULL,
    instance_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    CONSTRAINT ck_catalogue_metadata_singleton CHECK (singleton = 1),
    CONSTRAINT ck_catalogue_metadata_format CHECK (format = 'cairn.catalogue/v1'),
    CONSTRAINT ck_catalogue_metadata_instance_id CHECK (
        length(instance_id) = 36
        AND instance_id GLOB '????????-????-4???-[89ab]???-????????????'
        AND instance_id NOT GLOB '*[^0-9a-f-]*'
    ),
    CONSTRAINT ck_catalogue_metadata_created_at CHECK (
        length(created_at) = 27
        AND created_at GLOB '????-??-??T??:??:??.??????Z'
        AND created_at NOT GLOB '*[^0-9TZ:.-]*'
    )
) STRICT;

CREATE TABLE schema_migrations (
    version INTEGER NOT NULL PRIMARY KEY,
    name TEXT NOT NULL,
    sql_sha256 BLOB NOT NULL,
    applied_at TEXT NOT NULL,
    CONSTRAINT ck_schema_migrations_version CHECK (version > 0),
    CONSTRAINT ck_schema_migrations_name CHECK (
        length(name) BETWEEN 1 AND 63
        AND substr(name, 1, 1) GLOB '[a-z]'
        AND substr(name, -1, 1) GLOB '[a-z0-9]'
        AND name NOT GLOB '*[^a-z0-9_]*'
    ),
    CONSTRAINT ck_schema_migrations_digest CHECK (
        typeof(sql_sha256) = 'blob' AND length(sql_sha256) = 32
    ),
    CONSTRAINT ck_schema_migrations_applied_at CHECK (
        length(applied_at) = 27
        AND applied_at GLOB '????-??-??T??:??:??.??????Z'
        AND applied_at NOT GLOB '*[^0-9TZ:.-]*'
    )
) STRICT;

CREATE UNIQUE INDEX uq_schema_migrations_name ON schema_migrations(name);

CREATE TABLE realms (
    realm_id TEXT NOT NULL PRIMARY KEY,
    created_at TEXT NOT NULL,
    CONSTRAINT ck_realms_realm_id CHECK (
        length(realm_id) BETWEEN 1 AND 63
        AND substr(realm_id, 1, 1) GLOB '[a-z]'
        AND substr(realm_id, -1, 1) GLOB '[a-z0-9]'
        AND realm_id NOT GLOB '*[^a-z0-9-]*'
    ),
    CONSTRAINT ck_realms_created_at CHECK (
        length(created_at) = 27
        AND created_at GLOB '????-??-??T??:??:??.??????Z'
        AND created_at NOT GLOB '*[^0-9TZ:.-]*'
    )
) STRICT;

CREATE TABLE audit_heads (
    chain_kind TEXT NOT NULL,
    chain_identity TEXT NOT NULL,
    last_sequence INTEGER NOT NULL,
    last_hash BLOB NOT NULL,
    CONSTRAINT pk_audit_heads PRIMARY KEY (chain_kind, chain_identity),
    CONSTRAINT ck_audit_heads_chain_kind CHECK (chain_kind IN ('instance', 'realm')),
    CONSTRAINT ck_audit_heads_chain_identity CHECK (
        (
            chain_kind = 'instance'
            AND length(chain_identity) = 36
            AND chain_identity GLOB '????????-????-4???-[89ab]???-????????????'
            AND chain_identity NOT GLOB '*[^0-9a-f-]*'
        )
        OR (
            chain_kind = 'realm'
            AND length(chain_identity) BETWEEN 1 AND 63
            AND substr(chain_identity, 1, 1) GLOB '[a-z]'
            AND substr(chain_identity, -1, 1) GLOB '[a-z0-9]'
            AND chain_identity NOT GLOB '*[^a-z0-9-]*'
        )
    ),
    CONSTRAINT ck_audit_heads_last_sequence CHECK (last_sequence >= 0),
    CONSTRAINT ck_audit_heads_last_hash CHECK (
        typeof(last_hash) = 'blob' AND length(last_hash) = 32
    )
) STRICT;

CREATE TABLE audit_events (
    chain_kind TEXT NOT NULL,
    chain_identity TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    event_id TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    previous_hash BLOB NOT NULL,
    event_hash BLOB NOT NULL,
    action_kind TEXT NOT NULL,
    action_code TEXT NOT NULL,
    outcome TEXT NOT NULL,
    reason_code TEXT NOT NULL,
    canonical_event BLOB NOT NULL,
    CONSTRAINT pk_audit_events PRIMARY KEY (chain_kind, chain_identity, sequence),
    CONSTRAINT ck_audit_events_chain_kind CHECK (chain_kind IN ('instance', 'realm')),
    CONSTRAINT ck_audit_events_chain_identity CHECK (
        (
            chain_kind = 'instance'
            AND length(chain_identity) = 36
            AND chain_identity GLOB '????????-????-4???-[89ab]???-????????????'
            AND chain_identity NOT GLOB '*[^0-9a-f-]*'
        )
        OR (
            chain_kind = 'realm'
            AND length(chain_identity) BETWEEN 1 AND 63
            AND substr(chain_identity, 1, 1) GLOB '[a-z]'
            AND substr(chain_identity, -1, 1) GLOB '[a-z0-9]'
            AND chain_identity NOT GLOB '*[^a-z0-9-]*'
        )
    ),
    CONSTRAINT ck_audit_events_sequence CHECK (sequence > 0),
    CONSTRAINT ck_audit_events_event_id CHECK (
        length(event_id) = 36
        AND event_id GLOB '????????-????-4???-[89ab]???-????????????'
        AND event_id NOT GLOB '*[^0-9a-f-]*'
    ),
    CONSTRAINT ck_audit_events_recorded_at CHECK (
        length(recorded_at) = 27
        AND recorded_at GLOB '????-??-??T??:??:??.??????Z'
        AND recorded_at NOT GLOB '*[^0-9TZ:.-]*'
    ),
    CONSTRAINT ck_audit_events_previous_hash CHECK (
        typeof(previous_hash) = 'blob' AND length(previous_hash) = 32
    ),
    CONSTRAINT ck_audit_events_event_hash CHECK (
        typeof(event_hash) = 'blob' AND length(event_hash) = 32
    ),
    CONSTRAINT ck_audit_events_action_kind CHECK (
        action_kind IN ('data', 'administration', 'system')
    ),
    CONSTRAINT ck_audit_events_action_code CHECK (
        length(action_code) BETWEEN 1 AND 63
        AND substr(action_code, 1, 1) GLOB '[a-z]'
        AND substr(action_code, -1, 1) GLOB '[a-z0-9]'
        AND action_code NOT GLOB '*[^a-z0-9-]*'
    ),
    CONSTRAINT ck_audit_events_outcome CHECK (outcome IN ('allow', 'deny', 'error')),
    CONSTRAINT ck_audit_events_reason_code CHECK (
        length(reason_code) BETWEEN 1 AND 63
        AND substr(reason_code, 1, 1) GLOB '[a-z]'
        AND substr(reason_code, -1, 1) GLOB '[a-z0-9]'
        AND reason_code NOT GLOB '*[^a-z0-9_]*'
    ),
    CONSTRAINT ck_audit_events_canonical_event CHECK (
        typeof(canonical_event) = 'blob' AND length(canonical_event) > 0
    )
) STRICT;

CREATE UNIQUE INDEX uq_audit_events_event_id ON audit_events(event_id);

CREATE INDEX ix_audit_events_recorded_at
ON audit_events(recorded_at, chain_kind, chain_identity, sequence);

CREATE TABLE audit_scope_index (
    chain_kind TEXT NOT NULL,
    chain_identity TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    role TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    segment_kind TEXT,
    segment_id TEXT,
    CONSTRAINT pk_audit_scope_index PRIMARY KEY (
        chain_kind,
        chain_identity,
        sequence,
        role,
        ordinal
    ),
    CONSTRAINT fk_audit_scope_index_event FOREIGN KEY (
        chain_kind,
        chain_identity,
        sequence
    ) REFERENCES audit_events (chain_kind, chain_identity, sequence),
    CONSTRAINT ck_audit_scope_index_role CHECK (
        role IN ('source', 'requested', 'target')
    ),
    CONSTRAINT ck_audit_scope_index_ordinal CHECK (ordinal BETWEEN -1 AND 15),
    CONSTRAINT ck_audit_scope_index_root_or_segment CHECK (
        (
            ordinal = -1
            AND segment_kind IS NULL
            AND segment_id IS NULL
        )
        OR (
            ordinal BETWEEN 0 AND 15
            AND segment_kind IS NOT NULL
            AND segment_id IS NOT NULL
        )
    ),
    CONSTRAINT ck_audit_scope_index_segment_kind CHECK (
        segment_kind IS NULL
        OR (
            length(segment_kind) BETWEEN 1 AND 63
            AND substr(segment_kind, 1, 1) GLOB '[a-z]'
            AND substr(segment_kind, -1, 1) GLOB '[a-z0-9]'
            AND segment_kind NOT GLOB '*[^a-z0-9-]*'
        )
    ),
    CONSTRAINT ck_audit_scope_index_segment_id CHECK (
        segment_id IS NULL
        OR (
            length(segment_id) BETWEEN 1 AND 255
            AND substr(segment_id, 1, 1) GLOB '[A-Za-z0-9]'
            AND segment_id NOT GLOB '*[^A-Za-z0-9._~:/@+%-]*'
        )
    )
) STRICT;

CREATE INDEX ix_audit_scope_index_lookup
ON audit_scope_index(
    role,
    ordinal,
    segment_kind,
    segment_id,
    chain_kind,
    chain_identity,
    sequence
);

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
        AND idempotency_key GLOB '????????-????-4???-[89ab]???-????????????'
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

CREATE UNIQUE INDEX uq_idempotency_records_mutation_id
ON idempotency_records(mutation_id);

CREATE TRIGGER trg_catalogue_metadata_no_update
BEFORE UPDATE ON catalogue_metadata
BEGIN
    SELECT RAISE(ABORT, 'immutable_catalogue_metadata');
END;

CREATE TRIGGER trg_catalogue_metadata_no_delete
BEFORE DELETE ON catalogue_metadata
BEGIN
    SELECT RAISE(ABORT, 'immutable_catalogue_metadata');
END;

CREATE TRIGGER trg_schema_migrations_no_update
BEFORE UPDATE ON schema_migrations
BEGIN
    SELECT RAISE(ABORT, 'immutable_schema_migration');
END;

CREATE TRIGGER trg_schema_migrations_no_delete
BEFORE DELETE ON schema_migrations
BEGIN
    SELECT RAISE(ABORT, 'immutable_schema_migration');
END;

CREATE TRIGGER trg_realms_no_update
BEFORE UPDATE ON realms
BEGIN
    SELECT RAISE(ABORT, 'immutable_realm');
END;

CREATE TRIGGER trg_realms_no_delete
BEFORE DELETE ON realms
BEGIN
    SELECT RAISE(ABORT, 'immutable_realm');
END;

CREATE TRIGGER trg_audit_heads_no_delete
BEFORE DELETE ON audit_heads
BEGIN
    SELECT RAISE(ABORT, 'immutable_audit_head_identity');
END;

CREATE TRIGGER trg_audit_events_no_update
BEFORE UPDATE ON audit_events
BEGIN
    SELECT RAISE(ABORT, 'immutable_audit_event');
END;

CREATE TRIGGER trg_audit_events_no_delete
BEFORE DELETE ON audit_events
BEGIN
    SELECT RAISE(ABORT, 'immutable_audit_event');
END;

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
