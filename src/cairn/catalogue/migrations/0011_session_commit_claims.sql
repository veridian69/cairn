-- Reserve caller commit keys before normal ingest, without spanning writers.
CREATE TABLE memory_session_commit_claims (
    principal_id TEXT NOT NULL REFERENCES principals(principal_id),
    operation TEXT NOT NULL CHECK(operation = 'session-commit-claim'),
    idempotency_key TEXT NOT NULL CHECK(length(idempotency_key) = 36),
    command_digest BLOB NOT NULL CHECK(length(command_digest) = 32),
    session_id TEXT NOT NULL,
    turn_id TEXT NOT NULL,
    preparation_mutation_id TEXT NOT NULL,
    preparation_digest BLOB NOT NULL CHECK(length(preparation_digest) = 32),
    mutation_id TEXT NOT NULL UNIQUE REFERENCES idempotency_records(mutation_id)
        DEFERRABLE INITIALLY DEFERRED,
    PRIMARY KEY(principal_id, idempotency_key),
    FOREIGN KEY(principal_id, operation, idempotency_key)
        REFERENCES idempotency_records(principal_id, operation, idempotency_key)
        DEFERRABLE INITIALLY DEFERRED,
    FOREIGN KEY(session_id, turn_id, preparation_mutation_id, preparation_digest)
        REFERENCES memory_session_preparations(session_id, turn_id, mutation_id, payload_digest)
) STRICT;
CREATE TRIGGER memory_session_commit_claims_no_update BEFORE UPDATE ON memory_session_commit_claims BEGIN SELECT RAISE(ABORT, 'immutable_session_record'); END;
CREATE TRIGGER memory_session_commit_claims_no_delete BEFORE DELETE ON memory_session_commit_claims BEGIN SELECT RAISE(ABORT, 'immutable_session_record'); END;
-- Historical 0010 terminals remain valid. New terminals require their claim.
CREATE TRIGGER memory_session_terminal_claim BEFORE INSERT ON memory_session_terminals
WHEN NOT EXISTS (
    SELECT 1 FROM memory_session_commit_claims c
    WHERE c.principal_id=NEW.principal_id AND c.idempotency_key=NEW.idempotency_key
      AND c.command_digest=NEW.command_digest
      AND c.session_id=NEW.session_id AND c.turn_id=NEW.turn_id
      AND c.preparation_mutation_id=NEW.preparation_mutation_id
      AND c.preparation_digest=NEW.preparation_digest
)
BEGIN SELECT RAISE(ABORT, 'session_commit_claim_required'); END;
