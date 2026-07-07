-- Prompt 7's IsolationForest baseline uses 4 features (faithfulness, compliance,
-- response_length, token_count) — the latter two were missed when V7 was designed.
ALTER TABLE drift_records
    ADD COLUMN response_length INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN token_count INTEGER NOT NULL DEFAULT 0;
