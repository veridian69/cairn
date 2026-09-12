CREATE TABLE assertions (
    assertion_id TEXT NOT NULL PRIMARY KEY,
    realm_id TEXT NOT NULL,
    scope_segments TEXT NOT NULL,
    classification TEXT NOT NULL,
    source_type TEXT NOT NULL,
    principal_id TEXT NOT NULL,
    observed_at TEXT,
    metadata TEXT,
    recorded_at TEXT NOT NULL,
    CONSTRAINT fk_assertions_realm FOREIGN KEY (realm_id)
        REFERENCES realms (realm_id),
    CONSTRAINT fk_assertions_principal FOREIGN KEY (principal_id)
        REFERENCES principals (principal_id),
    CONSTRAINT ck_assertions_assertion_id CHECK (
        length(assertion_id) = 36
        AND assertion_id GLOB '????????-????-4???-[89ab]???-????????????'
        AND assertion_id NOT GLOB '*[^0-9a-f-]*'
    ),
    -- json(scope_segments) pins minified form (no insignificant whitespace),
    -- which SQLite can check deterministically. It does not sort object
    -- keys, so full canonical-JSON byte-identity is an application-layer
    -- invariant, not something this CHECK can express.
    CONSTRAINT ck_assertions_scope_segments CHECK (
        json_valid(scope_segments)
        AND json_type(scope_segments) = 'array'
        AND json_array_length(scope_segments) <= 16
        AND scope_segments = json(scope_segments)
    ),
    CONSTRAINT ck_assertions_classification CHECK (
        classification IN ('public', 'internal', 'restricted')
    ),
    CONSTRAINT ck_assertions_source_type CHECK (
        source_type IN ('agent-claim', 'verified-check', 'human')
    ),
    CONSTRAINT ck_assertions_observed_at CHECK (
        observed_at IS NULL
        OR (
            length(observed_at) = 27
            AND observed_at GLOB '????-??-??T??:??:??.??????Z'
            AND observed_at NOT GLOB '*[^0-9TZ:.-]*'
        )
    ),
    CONSTRAINT ck_assertions_metadata CHECK (
        metadata IS NULL
        OR (
            json_valid(metadata)
            AND length(CAST(metadata AS BLOB)) <= 65536
        )
    ),
    CONSTRAINT ck_assertions_recorded_at CHECK (
        length(recorded_at) = 27
        AND recorded_at GLOB '????-??-??T??:??:??.??????Z'
        AND recorded_at NOT GLOB '*[^0-9TZ:.-]*'
    )
) STRICT;

CREATE TABLE facts (
    fact_id TEXT NOT NULL PRIMARY KEY,
    realm_id TEXT NOT NULL,
    scope_segments TEXT NOT NULL,
    body TEXT NOT NULL,
    trust TEXT NOT NULL,
    classification TEXT NOT NULL,
    assertion_id TEXT,
    derived_from TEXT,
    promoted_by TEXT,
    evidence_id TEXT,
    valid_from TEXT,
    valid_to TEXT,
    recorded_at TEXT NOT NULL,
    CONSTRAINT fk_facts_realm FOREIGN KEY (realm_id)
        REFERENCES realms (realm_id),
    CONSTRAINT fk_facts_assertion FOREIGN KEY (assertion_id)
        REFERENCES assertions (assertion_id),
    CONSTRAINT fk_facts_derived_from FOREIGN KEY (derived_from)
        REFERENCES facts (fact_id),
    CONSTRAINT fk_facts_promoted_by FOREIGN KEY (promoted_by)
        REFERENCES principals (principal_id),
    CONSTRAINT fk_facts_evidence FOREIGN KEY (evidence_id)
        REFERENCES evidence_records (evidence_id),
    CONSTRAINT ck_facts_fact_id CHECK (
        length(fact_id) = 36
        AND fact_id GLOB '????????-????-4???-[89ab]???-????????????'
        AND fact_id NOT GLOB '*[^0-9a-f-]*'
    ),
    -- Minified-form pin only; see the comment on
    -- ck_assertions_scope_segments for why key-order canonicality can't be
    -- expressed here.
    CONSTRAINT ck_facts_scope_segments CHECK (
        json_valid(scope_segments)
        AND json_type(scope_segments) = 'array'
        AND json_array_length(scope_segments) <= 16
        AND scope_segments = json(scope_segments)
    ),
    CONSTRAINT ck_facts_body CHECK (
        length(CAST(body AS BLOB)) BETWEEN 1 AND 65536
    ),
    CONSTRAINT ck_facts_trust CHECK (
        trust IN ('candidate', 'validated', 'failed-approach')
    ),
    CONSTRAINT ck_facts_classification CHECK (
        classification IN ('public', 'internal', 'restricted')
    ),
    CONSTRAINT ck_facts_provenance_form CHECK (
        (
            assertion_id IS NOT NULL
            AND derived_from IS NULL
            AND promoted_by IS NULL
            AND evidence_id IS NULL
        )
        OR (
            assertion_id IS NULL
            AND derived_from IS NOT NULL
            AND promoted_by IS NOT NULL
            AND evidence_id IS NOT NULL
        )
    ),
    CONSTRAINT ck_facts_valid_from CHECK (
        valid_from IS NULL
        OR (
            length(valid_from) = 27
            AND valid_from GLOB '????-??-??T??:??:??.??????Z'
            AND valid_from NOT GLOB '*[^0-9TZ:.-]*'
        )
    ),
    CONSTRAINT ck_facts_valid_to CHECK (
        valid_to IS NULL
        OR (
            length(valid_to) = 27
            AND valid_to GLOB '????-??-??T??:??:??.??????Z'
            AND valid_to NOT GLOB '*[^0-9TZ:.-]*'
        )
    ),
    CONSTRAINT ck_facts_validity_order CHECK (
        valid_from IS NULL OR valid_to IS NULL OR valid_from < valid_to
    ),
    CONSTRAINT ck_facts_recorded_at CHECK (
        length(recorded_at) = 27
        AND recorded_at GLOB '????-??-??T??:??:??.??????Z'
        AND recorded_at NOT GLOB '*[^0-9TZ:.-]*'
    )
) STRICT;

CREATE INDEX ix_facts_assertion ON facts(assertion_id);
CREATE INDEX ix_facts_derived_from ON facts(derived_from);

CREATE TABLE fact_invalidations (
    fact_id TEXT NOT NULL PRIMARY KEY,
    invalidated_at TEXT NOT NULL,
    principal_id TEXT NOT NULL,
    superseded_by TEXT,
    reason TEXT NOT NULL,
    CONSTRAINT fk_fact_invalidations_fact FOREIGN KEY (fact_id)
        REFERENCES facts (fact_id),
    CONSTRAINT fk_fact_invalidations_principal FOREIGN KEY (principal_id)
        REFERENCES principals (principal_id),
    CONSTRAINT fk_fact_invalidations_superseded_by FOREIGN KEY (superseded_by)
        REFERENCES facts (fact_id),
    CONSTRAINT ck_fact_invalidations_invalidated_at CHECK (
        length(invalidated_at) = 27
        AND invalidated_at GLOB '????-??-??T??:??:??.??????Z'
        AND invalidated_at NOT GLOB '*[^0-9TZ:.-]*'
    ),
    CONSTRAINT ck_fact_invalidations_reason CHECK (
        length(CAST(reason AS BLOB)) BETWEEN 1 AND 4096
    )
) STRICT;

CREATE TABLE evidence_records (
    evidence_id TEXT NOT NULL PRIMARY KEY,
    realm_id TEXT NOT NULL,
    scope_segments TEXT NOT NULL,
    classification TEXT NOT NULL,
    payload_digest BLOB NOT NULL,
    assertion_id TEXT,
    payload_length INTEGER,
    external_uri TEXT,
    recorded_at TEXT NOT NULL,
    CONSTRAINT fk_evidence_records_realm FOREIGN KEY (realm_id)
        REFERENCES realms (realm_id),
    CONSTRAINT fk_evidence_records_assertion FOREIGN KEY (assertion_id)
        REFERENCES assertions (assertion_id),
    CONSTRAINT ck_evidence_records_evidence_id CHECK (
        length(evidence_id) = 36
        AND evidence_id GLOB '????????-????-4???-[89ab]???-????????????'
        AND evidence_id NOT GLOB '*[^0-9a-f-]*'
    ),
    -- Minified-form pin only; see the comment on
    -- ck_assertions_scope_segments for why key-order canonicality can't be
    -- expressed here.
    CONSTRAINT ck_evidence_records_scope_segments CHECK (
        json_valid(scope_segments)
        AND json_type(scope_segments) = 'array'
        AND json_array_length(scope_segments) <= 16
        AND scope_segments = json(scope_segments)
    ),
    CONSTRAINT ck_evidence_records_classification CHECK (
        classification IN ('public', 'internal', 'restricted')
    ),
    CONSTRAINT ck_evidence_records_payload_digest CHECK (
        typeof(payload_digest) = 'blob' AND length(payload_digest) = 32
    ),
    CONSTRAINT ck_evidence_records_payload_length CHECK (
        payload_length IS NULL OR payload_length BETWEEN 1 AND 1048576
    ),
    CONSTRAINT ck_evidence_records_external_uri CHECK (
        external_uri IS NULL
        OR (
            length(CAST(external_uri AS BLOB)) BETWEEN 1 AND 2048
            AND external_uri NOT GLOB '*[^!-~]*'
            AND instr(external_uri, char(58)) > 1
            AND substr(external_uri, 1, 1) GLOB '[A-Za-z]'
            AND substr(external_uri, 1, instr(external_uri, char(58)) - 1)
                NOT GLOB '*[^A-Za-z0-9+.-]*'
        )
    ),
    CONSTRAINT ck_evidence_records_custody_form CHECK (
        (
            assertion_id IS NOT NULL
            AND payload_length IS NOT NULL
            AND external_uri IS NULL
        )
        OR (
            assertion_id IS NULL
            AND payload_length IS NULL
            AND external_uri IS NOT NULL
        )
    ),
    CONSTRAINT ck_evidence_records_recorded_at CHECK (
        length(recorded_at) = 27
        AND recorded_at GLOB '????-??-??T??:??:??.??????Z'
        AND recorded_at NOT GLOB '*[^0-9TZ:.-]*'
    )
) STRICT;

CREATE INDEX ix_evidence_records_assertion ON evidence_records(assertion_id);

CREATE TABLE evidence_outbox (
    work_id TEXT NOT NULL PRIMARY KEY,
    kind TEXT NOT NULL,
    evidence_id TEXT NOT NULL,
    mutation_id TEXT NOT NULL,
    payload BLOB NOT NULL,
    created_at TEXT NOT NULL,
    attempts INTEGER NOT NULL,
    last_attempt_at TEXT,
    last_failure_code TEXT,
    CONSTRAINT fk_evidence_outbox_evidence FOREIGN KEY (evidence_id)
        REFERENCES evidence_records (evidence_id),
    CONSTRAINT ck_evidence_outbox_work_id CHECK (
        length(work_id) = 36
        AND work_id GLOB '????????-????-4???-[89ab]???-????????????'
        AND work_id NOT GLOB '*[^0-9a-f-]*'
    ),
    CONSTRAINT ck_evidence_outbox_kind CHECK (kind IN ('store-payload')),
    CONSTRAINT ck_evidence_outbox_mutation_id CHECK (
        length(mutation_id) = 36
        AND mutation_id GLOB '????????-????-4???-[89ab]???-????????????'
        AND mutation_id NOT GLOB '*[^0-9a-f-]*'
    ),
    CONSTRAINT ck_evidence_outbox_payload CHECK (
        typeof(payload) = 'blob'
        AND length(payload) BETWEEN 1 AND 1048576
    ),
    CONSTRAINT ck_evidence_outbox_created_at CHECK (
        length(created_at) = 27
        AND created_at GLOB '????-??-??T??:??:??.??????Z'
        AND created_at NOT GLOB '*[^0-9TZ:.-]*'
    ),
    CONSTRAINT ck_evidence_outbox_attempts CHECK (attempts >= 0),
    CONSTRAINT ck_evidence_outbox_last_attempt_at CHECK (
        last_attempt_at IS NULL
        OR (
            length(last_attempt_at) = 27
            AND last_attempt_at GLOB '????-??-??T??:??:??.??????Z'
            AND last_attempt_at NOT GLOB '*[^0-9TZ:.-]*'
        )
    ),
    CONSTRAINT ck_evidence_outbox_last_failure_code CHECK (
        last_failure_code IS NULL
        OR (
            length(last_failure_code) BETWEEN 1 AND 63
            AND substr(last_failure_code, 1, 1) GLOB '[a-z]'
            AND substr(last_failure_code, -1, 1) GLOB '[a-z0-9]'
            AND last_failure_code NOT GLOB '*[^a-z0-9_]*'
        )
    )
) STRICT;

CREATE INDEX ix_evidence_outbox_created ON evidence_outbox(created_at);

CREATE TABLE projection_outbox (
    work_id TEXT NOT NULL PRIMARY KEY,
    kind TEXT NOT NULL,
    fact_id TEXT NOT NULL,
    mutation_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    attempts INTEGER NOT NULL,
    last_attempt_at TEXT,
    last_failure_code TEXT,
    CONSTRAINT fk_projection_outbox_fact FOREIGN KEY (fact_id)
        REFERENCES facts (fact_id),
    CONSTRAINT ck_projection_outbox_work_id CHECK (
        length(work_id) = 36
        AND work_id GLOB '????????-????-4???-[89ab]???-????????????'
        AND work_id NOT GLOB '*[^0-9a-f-]*'
    ),
    CONSTRAINT ck_projection_outbox_kind CHECK (
        kind IN ('fact-ingested', 'fact-promoted', 'fact-invalidated')
    ),
    CONSTRAINT ck_projection_outbox_mutation_id CHECK (
        length(mutation_id) = 36
        AND mutation_id GLOB '????????-????-4???-[89ab]???-????????????'
        AND mutation_id NOT GLOB '*[^0-9a-f-]*'
    ),
    CONSTRAINT ck_projection_outbox_created_at CHECK (
        length(created_at) = 27
        AND created_at GLOB '????-??-??T??:??:??.??????Z'
        AND created_at NOT GLOB '*[^0-9TZ:.-]*'
    ),
    CONSTRAINT ck_projection_outbox_attempts CHECK (attempts >= 0),
    CONSTRAINT ck_projection_outbox_last_attempt_at CHECK (
        last_attempt_at IS NULL
        OR (
            length(last_attempt_at) = 27
            AND last_attempt_at GLOB '????-??-??T??:??:??.??????Z'
            AND last_attempt_at NOT GLOB '*[^0-9TZ:.-]*'
        )
    ),
    CONSTRAINT ck_projection_outbox_last_failure_code CHECK (
        last_failure_code IS NULL
        OR (
            length(last_failure_code) BETWEEN 1 AND 63
            AND substr(last_failure_code, 1, 1) GLOB '[a-z]'
            AND substr(last_failure_code, -1, 1) GLOB '[a-z0-9]'
            AND last_failure_code NOT GLOB '*[^a-z0-9_]*'
        )
    )
) STRICT;

CREATE INDEX ix_projection_outbox_created ON projection_outbox(created_at);

CREATE TRIGGER trg_assertions_no_update
BEFORE UPDATE ON assertions
BEGIN
    SELECT RAISE(ABORT, 'immutable_assertion');
END;

CREATE TRIGGER trg_assertions_no_delete
BEFORE DELETE ON assertions
BEGIN
    SELECT RAISE(ABORT, 'immutable_assertion');
END;

CREATE TRIGGER trg_facts_no_update
BEFORE UPDATE ON facts
BEGIN
    SELECT RAISE(ABORT, 'immutable_fact');
END;

CREATE TRIGGER trg_facts_no_delete
BEFORE DELETE ON facts
BEGIN
    SELECT RAISE(ABORT, 'immutable_fact');
END;

CREATE TRIGGER trg_fact_invalidations_no_update
BEFORE UPDATE ON fact_invalidations
BEGIN
    SELECT RAISE(ABORT, 'immutable_fact_invalidation');
END;

CREATE TRIGGER trg_fact_invalidations_no_delete
BEFORE DELETE ON fact_invalidations
BEGIN
    SELECT RAISE(ABORT, 'immutable_fact_invalidation');
END;

CREATE TRIGGER trg_evidence_records_no_update
BEFORE UPDATE ON evidence_records
BEGIN
    SELECT RAISE(ABORT, 'immutable_evidence_record');
END;

CREATE TRIGGER trg_evidence_records_no_delete
BEFORE DELETE ON evidence_records
BEGIN
    SELECT RAISE(ABORT, 'immutable_evidence_record');
END;

CREATE TRIGGER trg_evidence_outbox_identity_frozen
BEFORE UPDATE ON evidence_outbox
WHEN NEW.work_id IS NOT OLD.work_id
    OR NEW.kind IS NOT OLD.kind
    OR NEW.evidence_id IS NOT OLD.evidence_id
    OR NEW.mutation_id IS NOT OLD.mutation_id
    OR NEW.payload IS NOT OLD.payload
    OR NEW.created_at IS NOT OLD.created_at
BEGIN
    SELECT RAISE(ABORT, 'evidence_outbox_identity_frozen');
END;

CREATE TRIGGER trg_projection_outbox_identity_frozen
BEFORE UPDATE ON projection_outbox
WHEN NEW.work_id IS NOT OLD.work_id
    OR NEW.kind IS NOT OLD.kind
    OR NEW.fact_id IS NOT OLD.fact_id
    OR NEW.mutation_id IS NOT OLD.mutation_id
    OR NEW.created_at IS NOT OLD.created_at
BEGIN
    SELECT RAISE(ABORT, 'projection_outbox_identity_frozen');
END;
