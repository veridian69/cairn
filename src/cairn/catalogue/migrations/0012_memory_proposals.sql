-- Source-level discussion; immutable records linked to real mutation/audit history.
CREATE TABLE memory_proposals (
    proposal_id TEXT PRIMARY KEY NOT NULL CHECK(length(proposal_id) = 36),
    source_fact_id TEXT NOT NULL REFERENCES facts(fact_id),
    realm_id TEXT NOT NULL REFERENCES realms(realm_id),
    scope_segments TEXT NOT NULL CHECK(json_valid(scope_segments) AND json_type(scope_segments) = 'array'),
    target_segments TEXT NOT NULL CHECK(json_valid(target_segments) AND json_type(target_segments) = 'array'),
    classification TEXT NOT NULL CHECK(classification IN ('public','internal','restricted')),
    reason TEXT NOT NULL CHECK(length(CAST(reason AS BLOB)) BETWEEN 1 AND 4096),
    principal_id TEXT NOT NULL REFERENCES principals(principal_id),
    operation TEXT NOT NULL CHECK(operation = 'memory-propose'),
    idempotency_key TEXT NOT NULL CHECK(length(idempotency_key) = 36),
    command_digest BLOB NOT NULL CHECK(length(command_digest) = 32),
    mutation_id TEXT NOT NULL UNIQUE REFERENCES idempotency_records(mutation_id) DEFERRABLE INITIALLY DEFERRED,
    recorded_at TEXT NOT NULL CHECK(length(recorded_at) = 27),
    FOREIGN KEY(principal_id, operation, idempotency_key)
        REFERENCES idempotency_records(principal_id, operation, idempotency_key) DEFERRABLE INITIALLY DEFERRED
) STRICT;
CREATE INDEX memory_proposals_source ON memory_proposals(realm_id, scope_segments, classification, proposal_id);
CREATE TABLE memory_proposal_decisions (
    proposal_id TEXT PRIMARY KEY NOT NULL REFERENCES memory_proposals(proposal_id),
    state TEXT NOT NULL CHECK(state IN ('accepted','rejected')),
    principal_id TEXT NOT NULL REFERENCES principals(principal_id),
    operation TEXT NOT NULL CHECK(operation IN ('memory-proposal-accept','memory-proposal-reject')),
    idempotency_key TEXT NOT NULL CHECK(length(idempotency_key) = 36),
    command_digest BLOB NOT NULL CHECK(length(command_digest) = 32),
    mutation_id TEXT NOT NULL UNIQUE REFERENCES idempotency_records(mutation_id) DEFERRABLE INITIALLY DEFERRED,
    recorded_at TEXT NOT NULL CHECK(length(recorded_at) = 27),
    reason TEXT CHECK(reason IS NULL OR length(CAST(reason AS BLOB)) BETWEEN 1 AND 4096),
    evidence_id TEXT REFERENCES evidence_records(evidence_id),
    promoted_fact_id TEXT UNIQUE REFERENCES facts(fact_id),
    target_classification TEXT CHECK(target_classification IS NULL OR target_classification IN ('public','internal','restricted')),
    CHECK((state='accepted' AND operation='memory-proposal-accept' AND reason IS NULL AND evidence_id IS NOT NULL AND promoted_fact_id IS NOT NULL AND target_classification IS NOT NULL)
       OR (state='rejected' AND operation='memory-proposal-reject' AND reason IS NOT NULL AND evidence_id IS NULL AND promoted_fact_id IS NULL AND target_classification IS NULL)),
    FOREIGN KEY(principal_id, operation, idempotency_key)
        REFERENCES idempotency_records(principal_id, operation, idempotency_key) DEFERRABLE INITIALLY DEFERRED
) STRICT;
CREATE TRIGGER memory_proposals_no_update BEFORE UPDATE ON memory_proposals BEGIN SELECT RAISE(ABORT, 'immutable_proposal_record'); END;
CREATE TRIGGER memory_proposals_no_delete BEFORE DELETE ON memory_proposals BEGIN SELECT RAISE(ABORT, 'immutable_proposal_record'); END;
CREATE TRIGGER memory_proposal_decisions_no_update BEFORE UPDATE ON memory_proposal_decisions BEGIN SELECT RAISE(ABORT, 'immutable_proposal_record'); END;
CREATE TRIGGER memory_proposal_decisions_no_delete BEFORE DELETE ON memory_proposal_decisions BEGIN SELECT RAISE(ABORT, 'immutable_proposal_record'); END;
