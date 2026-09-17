-- I-68, amended 9 August 2026: a projection outbox row need no longer
-- descend from a custody mutation. `rebuild-index` clears the whole index
-- before re-projecting, which destroys entries for facts whose mutation
-- rows drained long ago, so a failed re-projection would leave the index
-- short with nothing recording what it owes. The rebuild therefore
-- enqueues one row per catalogue fact before clearing and deletes each as
-- that fact is re-projected, making the outbox mean what it always
-- implied: a row means projection is owed or has not been durably
-- confirmed. Not an equivalence — delivery is at-least-once, so a row
-- lawfully outlives the work it names until the confirming transaction
-- commits.
--
-- Two shape changes follow. `fact-rebuild` joins the three custody kinds,
-- and `mutation_id` becomes null exactly when the kind is `fact-rebuild` —
-- a rebuild row descends from no mutation, and inventing a mutation
-- identity for it would put fiction into the one table offline
-- verification cross-checks against the audit chain.
--
-- SQLite cannot alter a CHECK constraint, so the table is recreated with
-- its rows copied through a scratch table exactly as 0004 did. Unlike
-- 0004 the `sequence` values are copied explicitly rather than
-- reassigned: they exist now, I-78 rests on their monotonicity, and
-- preserving them keeps that promise across the migration instead of
-- re-basing it.
--
-- Copying the live rows is not by itself enough to keep that promise.
-- AUTOINCREMENT's guarantee lives in `sqlite_sequence`, which records the
-- largest value ever *allocated*, not the largest still present — and
-- `DROP TABLE` deletes that row. The deliverer deletes each row it
-- confirms, so the high-water mark routinely sits above `MAX(sequence)`
-- and, on a fully drained queue, above nothing at all. Recreating the
-- table would then restart allocation from the largest surviving row and
-- hand out sequence numbers that were already issued to rows since
-- delivered, which is precisely the reuse 0004 introduced AUTOINCREMENT
-- to prevent. The old mark is therefore carried across the drop in a
-- scratch table and restored afterwards.
CREATE TABLE projection_outbox_migration AS
SELECT sequence, work_id, kind, fact_id, mutation_id, created_at, attempts,
       last_attempt_at, last_failure_code
FROM projection_outbox;

-- Read before the drop destroys it. Empty when the table has never been
-- inserted into, which the restore below treats as no mark at all.
CREATE TABLE projection_outbox_high_water AS
SELECT seq FROM sqlite_sequence WHERE name = 'projection_outbox';

DROP TABLE projection_outbox;

CREATE TABLE projection_outbox (
    sequence INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
    work_id TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL,
    fact_id TEXT NOT NULL,
    mutation_id TEXT,
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
        kind IN ('fact-ingested', 'fact-promoted', 'fact-invalidated',
                 'fact-rebuild')
    ),
    -- The null is not merely permitted, it is required: a custody row
    -- without its mutation identity is as corrupt as a rebuild row with
    -- one, and both are the schema's business rather than the writer's.
    CONSTRAINT ck_projection_outbox_mutation_id CHECK (
        (
            kind = 'fact-rebuild' AND mutation_id IS NULL
        ) OR (
            kind <> 'fact-rebuild'
            AND mutation_id IS NOT NULL
            AND length(mutation_id) = 36
            AND mutation_id GLOB '????????-????-4???-[89ab]???-????????????'
            AND mutation_id NOT GLOB '*[^0-9a-f-]*'
        )
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

INSERT INTO projection_outbox (sequence, work_id, kind, fact_id, mutation_id,
    created_at, attempts, last_attempt_at, last_failure_code)
SELECT sequence, work_id, kind, fact_id, mutation_id, created_at, attempts,
       last_attempt_at, last_failure_code
FROM projection_outbox_migration
ORDER BY sequence;

DROP TABLE projection_outbox_migration;

-- Restore the high-water mark: whichever is larger of what the old table
-- had allocated and what the copy above wrote. The copy's own inserts
-- have already set the row to MAX(sequence), so this only ever raises it,
-- and never past a value that was genuinely issued. The guard keeps a
-- catalogue that has never queued projection work identical to one
-- created before this migration existed — no row, rather than a row
-- saying zero.
DELETE FROM sqlite_sequence WHERE name = 'projection_outbox';

INSERT INTO sqlite_sequence (name, seq)
SELECT 'projection_outbox', high_water
FROM (
    SELECT MAX(candidate) AS high_water
    FROM (
        SELECT COALESCE(
            (SELECT seq FROM projection_outbox_high_water), 0
        ) AS candidate
        UNION ALL
        SELECT COALESCE((SELECT MAX(sequence) FROM projection_outbox), 0)
    )
)
WHERE high_water > 0;

DROP TABLE projection_outbox_high_water;

CREATE INDEX ix_projection_outbox_created ON projection_outbox(created_at);

-- Recreated with the table, unchanged: dropping a table drops its triggers.
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
