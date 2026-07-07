CREATE TABLE ab_results (
    id              BIGSERIAL PRIMARY KEY,
    ab_test_id      BIGINT NOT NULL REFERENCES ab_tests(id) ON DELETE CASCADE,
    version_used    VARCHAR(1) NOT NULL CHECK (version_used IN ('A', 'B')),
    quality_score   REAL NOT NULL,
    recorded_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_ab_results_test_version ON ab_results(ab_test_id, version_used);
