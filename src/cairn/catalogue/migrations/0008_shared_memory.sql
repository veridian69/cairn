-- Attributed assertions about disagreement and its resolution. Facts stay immutable.
CREATE TABLE memory_disagreements (
    relationship_id TEXT PRIMARY KEY NOT NULL,
    realm_id TEXT NOT NULL REFERENCES realms(realm_id),
    scope_segments TEXT NOT NULL CHECK(json_valid(scope_segments) AND json_type(scope_segments) = 'array'),
    left_fact_id TEXT NOT NULL REFERENCES facts(fact_id),
    right_fact_id TEXT NOT NULL REFERENCES facts(fact_id),
    classification TEXT NOT NULL CHECK(classification IN ('public','internal','restricted')),
    principal_id TEXT NOT NULL REFERENCES principals(principal_id),
    reason TEXT NOT NULL CHECK(length(CAST(reason AS BLOB)) BETWEEN 1 AND 4096),
    recorded_at TEXT NOT NULL CHECK(length(recorded_at) = 27),
    mutation_id TEXT NOT NULL,
    CHECK(left_fact_id != right_fact_id),
    CHECK(length(relationship_id) = 36 AND relationship_id GLOB '????????-????-4???-[89ab]???-????????????' AND relationship_id NOT GLOB '*[^0-9a-f-]*')
) STRICT;
CREATE INDEX memory_disagreements_left ON memory_disagreements(left_fact_id);
CREATE INDEX memory_disagreements_right ON memory_disagreements(right_fact_id);
CREATE TABLE memory_resolutions (
    relationship_id TEXT PRIMARY KEY NOT NULL,
    disagreement_id TEXT NOT NULL REFERENCES memory_disagreements(relationship_id),
    evidence_id TEXT NOT NULL REFERENCES evidence_records(evidence_id),
    selected_fact_id TEXT REFERENCES facts(fact_id),
    classification TEXT NOT NULL CHECK(classification IN ('public','internal','restricted')),
    principal_id TEXT NOT NULL REFERENCES principals(principal_id),
    reason TEXT NOT NULL CHECK(length(CAST(reason AS BLOB)) BETWEEN 1 AND 4096),
    recorded_at TEXT NOT NULL CHECK(length(recorded_at) = 27),
    mutation_id TEXT NOT NULL,
    CHECK(length(relationship_id) = 36 AND relationship_id GLOB '????????-????-4???-[89ab]???-????????????' AND relationship_id NOT GLOB '*[^0-9a-f-]*')
) STRICT;
CREATE INDEX memory_resolutions_disagreement ON memory_resolutions(disagreement_id);
CREATE TRIGGER memory_disagreements_no_update BEFORE UPDATE ON memory_disagreements BEGIN SELECT RAISE(ABORT, 'immutable_memory_relationship'); END;
CREATE TRIGGER memory_disagreements_no_delete BEFORE DELETE ON memory_disagreements BEGIN SELECT RAISE(ABORT, 'immutable_memory_relationship'); END;
CREATE TRIGGER memory_resolutions_no_update BEFORE UPDATE ON memory_resolutions BEGIN SELECT RAISE(ABORT, 'immutable_memory_relationship'); END;
CREATE TRIGGER memory_resolutions_no_delete BEFORE DELETE ON memory_resolutions BEGIN SELECT RAISE(ABORT, 'immutable_memory_relationship'); END;
