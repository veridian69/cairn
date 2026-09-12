-- Owner-private operational state; not facts or projection inputs.
-- The deferred idempotency reference binds each operation to its original audit
-- event through idempotency_records.original_event_id, in the same transaction.
CREATE TABLE memory_session_operations (
    mutation_id TEXT PRIMARY KEY NOT NULL CHECK(length(mutation_id) = 36)
        REFERENCES idempotency_records(mutation_id) DEFERRABLE INITIALLY DEFERRED,
    principal_id TEXT NOT NULL REFERENCES principals(principal_id),
    operation TEXT NOT NULL CHECK(operation IN ('session-open','session-begin','session-prepare','session-abandon','session-issue-visit','session-acknowledge-visit')),
    idempotency_key TEXT NOT NULL CHECK(length(idempotency_key) = 36),
    command_digest BLOB NOT NULL CHECK(length(command_digest) = 32),
    recorded_at TEXT NOT NULL CHECK(length(recorded_at) = 27),
    FOREIGN KEY(principal_id, operation, idempotency_key)
        REFERENCES idempotency_records(principal_id, operation, idempotency_key)
        DEFERRABLE INITIALLY DEFERRED
) STRICT;
CREATE TABLE memory_sessions (
    session_id TEXT PRIMARY KEY NOT NULL CHECK(length(session_id) = 36),
    instance_id TEXT NOT NULL CHECK(length(instance_id) = 36),
    principal_id TEXT NOT NULL REFERENCES principals(principal_id),
    realm_id TEXT NOT NULL REFERENCES realms(realm_id),
    scope_segments TEXT NOT NULL CHECK(json_valid(scope_segments) AND json_type(scope_segments) = 'array'),
    classification TEXT NOT NULL CHECK(classification IN ('public','internal','restricted')),
    mutation_id TEXT NOT NULL UNIQUE REFERENCES memory_session_operations(mutation_id)
) STRICT;
CREATE TABLE memory_session_turns (
    session_id TEXT NOT NULL REFERENCES memory_sessions(session_id),
    turn_id TEXT NOT NULL CHECK(length(turn_id) = 36),
    attempt_id TEXT NOT NULL CHECK(length(attempt_id) = 36),
    replaces_turn_id TEXT,
    mutation_id TEXT NOT NULL UNIQUE REFERENCES memory_session_operations(mutation_id),
    PRIMARY KEY(session_id, turn_id),
    UNIQUE(session_id, attempt_id),
    UNIQUE(session_id, replaces_turn_id),
    FOREIGN KEY(session_id, replaces_turn_id) REFERENCES memory_session_turns(session_id, turn_id),
    CHECK(replaces_turn_id IS NULL OR replaces_turn_id != turn_id)
) STRICT;
CREATE TABLE memory_session_preparations (
    session_id TEXT NOT NULL,
    turn_id TEXT NOT NULL,
    payload BLOB NOT NULL CHECK(length(payload) BETWEEN 1 AND 73728 AND json_valid(CAST(payload AS TEXT))),
    payload_digest BLOB NOT NULL CHECK(length(payload_digest) = 32),
    mutation_id TEXT NOT NULL UNIQUE REFERENCES memory_session_operations(mutation_id),
    PRIMARY KEY(session_id, turn_id),
    FOREIGN KEY(session_id, turn_id) REFERENCES memory_session_turns(session_id, turn_id)
) STRICT;
CREATE TABLE memory_session_abandonments (
    session_id TEXT NOT NULL,
    turn_id TEXT NOT NULL,
    reason TEXT NOT NULL CHECK(length(CAST(reason AS BLOB)) BETWEEN 1 AND 4096),
    mutation_id TEXT NOT NULL UNIQUE REFERENCES memory_session_operations(mutation_id),
    PRIMARY KEY(session_id, turn_id),
    FOREIGN KEY(session_id, turn_id) REFERENCES memory_session_turns(session_id, turn_id)
) STRICT;
CREATE TABLE memory_session_visits (
    session_id TEXT NOT NULL REFERENCES memory_sessions(session_id),
    visit_id TEXT NOT NULL CHECK(length(visit_id) = 36),
    watermark INTEGER NOT NULL CHECK(watermark > 0),
    issued_at TEXT NOT NULL CHECK(length(issued_at) = 27),
    mutation_id TEXT NOT NULL UNIQUE REFERENCES memory_session_operations(mutation_id),
    PRIMARY KEY(session_id, visit_id),
    UNIQUE(session_id, watermark)
) STRICT;
CREATE TABLE memory_session_acknowledgements (
    session_id TEXT NOT NULL,
    visit_id TEXT NOT NULL,
    mutation_id TEXT PRIMARY KEY NOT NULL REFERENCES memory_session_operations(mutation_id),
    FOREIGN KEY(session_id, visit_id) REFERENCES memory_session_visits(session_id, visit_id)
) STRICT;
CREATE INDEX memory_session_acknowledgements_session ON memory_session_acknowledgements(session_id);
CREATE TRIGGER memory_session_prepare_started BEFORE INSERT ON memory_session_preparations
WHEN EXISTS(SELECT 1 FROM memory_session_abandonments WHERE session_id=NEW.session_id AND turn_id=NEW.turn_id)
BEGIN SELECT RAISE(ABORT, 'session_not_started'); END;
CREATE TRIGGER memory_session_abandon_started BEFORE INSERT ON memory_session_abandonments
WHEN EXISTS(SELECT 1 FROM memory_session_preparations WHERE session_id=NEW.session_id AND turn_id=NEW.turn_id)
BEGIN SELECT RAISE(ABORT, 'session_not_started'); END;
CREATE TRIGGER memory_session_replace_abandoned BEFORE INSERT ON memory_session_turns
WHEN NEW.replaces_turn_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM memory_session_abandonments WHERE session_id=NEW.session_id AND turn_id=NEW.replaces_turn_id)
BEGIN SELECT RAISE(ABORT, 'session_not_abandoned'); END;
CREATE TRIGGER memory_session_operations_no_update BEFORE UPDATE ON memory_session_operations BEGIN SELECT RAISE(ABORT, 'immutable_session_record'); END;
CREATE TRIGGER memory_session_operations_no_delete BEFORE DELETE ON memory_session_operations BEGIN SELECT RAISE(ABORT, 'immutable_session_record'); END;
CREATE TRIGGER memory_sessions_no_update BEFORE UPDATE ON memory_sessions BEGIN SELECT RAISE(ABORT, 'immutable_session_record'); END;
CREATE TRIGGER memory_sessions_no_delete BEFORE DELETE ON memory_sessions BEGIN SELECT RAISE(ABORT, 'immutable_session_record'); END;
CREATE TRIGGER memory_session_turns_no_update BEFORE UPDATE ON memory_session_turns BEGIN SELECT RAISE(ABORT, 'immutable_session_record'); END;
CREATE TRIGGER memory_session_turns_no_delete BEFORE DELETE ON memory_session_turns BEGIN SELECT RAISE(ABORT, 'immutable_session_record'); END;
CREATE TRIGGER memory_session_preparations_no_update BEFORE UPDATE ON memory_session_preparations BEGIN SELECT RAISE(ABORT, 'immutable_session_record'); END;
CREATE TRIGGER memory_session_preparations_no_delete BEFORE DELETE ON memory_session_preparations BEGIN SELECT RAISE(ABORT, 'immutable_session_record'); END;
CREATE TRIGGER memory_session_abandonments_no_update BEFORE UPDATE ON memory_session_abandonments BEGIN SELECT RAISE(ABORT, 'immutable_session_record'); END;
CREATE TRIGGER memory_session_abandonments_no_delete BEFORE DELETE ON memory_session_abandonments BEGIN SELECT RAISE(ABORT, 'immutable_session_record'); END;
CREATE TRIGGER memory_session_visits_no_update BEFORE UPDATE ON memory_session_visits BEGIN SELECT RAISE(ABORT, 'immutable_session_record'); END;
CREATE TRIGGER memory_session_visits_no_delete BEFORE DELETE ON memory_session_visits BEGIN SELECT RAISE(ABORT, 'immutable_session_record'); END;
CREATE TRIGGER memory_session_acknowledgements_no_update BEFORE UPDATE ON memory_session_acknowledgements BEGIN SELECT RAISE(ABORT, 'immutable_session_record'); END;
CREATE TRIGGER memory_session_acknowledgements_no_delete BEFORE DELETE ON memory_session_acknowledgements BEGIN SELECT RAISE(ABORT, 'immutable_session_record'); END;
