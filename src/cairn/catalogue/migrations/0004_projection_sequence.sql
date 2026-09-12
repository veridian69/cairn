-- I-78: projection delivery order is undefined without an ordering column,
-- so projection_outbox gains a monotonic sequence. AUTOINCREMENT rather
-- than an application counter: the deliverer deletes confirmed rows, and a
-- bare INTEGER PRIMARY KEY would reuse the largest rowid after such a
-- delete, silently breaking monotonicity exactly when the queue drains.
-- AUTOINCREMENT never reuses a value, allocates only at real insert (an
-- idempotent replay returns the stored receipt without re-inserting, so
-- replays cannot disturb it), and needs no application allocation code.
-- SQLite cannot add a PRIMARY KEY by ALTER, so the accepted slice 4 table
-- is recreated with its rows copied through a scratch table in
-- (created_at, work_id) order, which is what assigns existing rows their
-- backfilled sequence deterministically.
CREATE TABLE projection_outbox_migration AS
SELECT work_id, kind, fact_id, mutation_id, created_at, attempts,
       last_attempt_at, last_failure_code
FROM projection_outbox;

DROP TABLE projection_outbox;

CREATE TABLE projection_outbox (
    sequence INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
    work_id TEXT NOT NULL UNIQUE,
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

INSERT INTO projection_outbox (work_id, kind, fact_id, mutation_id,
    created_at, attempts, last_attempt_at, last_failure_code)
SELECT work_id, kind, fact_id, mutation_id, created_at, attempts,
       last_attempt_at, last_failure_code
FROM projection_outbox_migration
ORDER BY created_at, work_id;

DROP TABLE projection_outbox_migration;

CREATE INDEX ix_projection_outbox_created ON projection_outbox(created_at);

-- The slice 4 identity freeze, extended to the new column per I-78.
CREATE TRIGGER trg_projection_outbox_identity_frozen
BEFORE UPDATE ON projection_outbox
WHEN NEW.sequence IS NOT OLD.sequence
    OR NEW.work_id IS NOT OLD.work_id
    OR NEW.kind IS NOT OLD.kind
    OR NEW.fact_id IS NOT OLD.fact_id
    OR NEW.mutation_id IS NOT OLD.mutation_id
    OR NEW.created_at IS NOT OLD.created_at
BEGIN
    SELECT RAISE(ABORT, 'projection_outbox_identity_frozen');
END;
