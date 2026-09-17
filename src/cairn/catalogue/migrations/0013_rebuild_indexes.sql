CREATE INDEX ix_projection_outbox_fact_kind ON projection_outbox(fact_id, kind);
CREATE INDEX ix_facts_recorded_fact ON facts(recorded_at, fact_id);
