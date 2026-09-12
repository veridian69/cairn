-- One terminal per immutable preparation. Both custody and terminal operations
-- reference real idempotency records; old session operation vocabulary is frozen.
CREATE UNIQUE INDEX memory_session_preparation_binding
    ON memory_session_preparations(session_id, turn_id, mutation_id, payload_digest);
CREATE TABLE memory_session_terminals (
    session_id TEXT NOT NULL,
    turn_id TEXT NOT NULL,
    preparation_mutation_id TEXT NOT NULL UNIQUE REFERENCES memory_session_preparations(mutation_id),
    preparation_digest BLOB NOT NULL CHECK(length(preparation_digest) = 32),
    state TEXT NOT NULL CHECK(state IN ('committed', 'skipped')),
    mutation_id TEXT NOT NULL UNIQUE
        REFERENCES idempotency_records(mutation_id) DEFERRABLE INITIALLY DEFERRED,
    principal_id TEXT NOT NULL REFERENCES principals(principal_id),
    operation TEXT NOT NULL CHECK(operation = 'session-commit'),
    idempotency_key TEXT NOT NULL CHECK(length(idempotency_key) = 36),
    command_digest BLOB NOT NULL CHECK(length(command_digest) = 32),
    recorded_at TEXT NOT NULL CHECK(length(recorded_at) = 27),
    result BLOB NOT NULL CHECK(json_valid(CAST(result AS TEXT))),
    custody_mutation_id TEXT UNIQUE REFERENCES idempotency_records(mutation_id),
    custody_event_id TEXT UNIQUE REFERENCES audit_events(event_id),
    custody_assertion_id TEXT UNIQUE REFERENCES assertions(assertion_id),
    PRIMARY KEY(session_id, turn_id),
    FOREIGN KEY(session_id, turn_id) REFERENCES memory_session_preparations(session_id, turn_id),
    FOREIGN KEY(session_id, turn_id, preparation_mutation_id, preparation_digest)
        REFERENCES memory_session_preparations(session_id, turn_id, mutation_id, payload_digest),
    FOREIGN KEY(principal_id, operation, idempotency_key)
        REFERENCES idempotency_records(principal_id, operation, idempotency_key)
        DEFERRABLE INITIALLY DEFERRED,
    CHECK((state = 'skipped' AND custody_mutation_id IS NULL AND custody_event_id IS NULL AND custody_assertion_id IS NULL)
       OR (state = 'committed' AND custody_mutation_id IS NOT NULL AND custody_event_id IS NOT NULL AND custody_assertion_id IS NOT NULL))
) STRICT;
CREATE TRIGGER memory_session_terminals_no_update BEFORE UPDATE ON memory_session_terminals BEGIN SELECT RAISE(ABORT, 'immutable_session_record'); END;
CREATE TRIGGER memory_session_terminals_no_delete BEFORE DELETE ON memory_session_terminals BEGIN SELECT RAISE(ABORT, 'immutable_session_record'); END;
