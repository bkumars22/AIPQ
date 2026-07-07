CREATE TABLE ab_tests (
    id                  BIGSERIAL PRIMARY KEY,
    prompt_id           BIGINT NOT NULL REFERENCES prompts(id) ON DELETE CASCADE,
    version_a_id        BIGINT NOT NULL REFERENCES prompt_versions(id),
    version_b_id        BIGINT NOT NULL REFERENCES prompt_versions(id),
    traffic_split       REAL NOT NULL DEFAULT 0.5 CHECK (traffic_split >= 0.0 AND traffic_split <= 1.0),
    started_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    ended_at            TIMESTAMPTZ,
    winner_version_id   BIGINT REFERENCES prompt_versions(id),
    min_samples         INTEGER NOT NULL DEFAULT 100,
    current_samples     INTEGER NOT NULL DEFAULT 0,
    status              VARCHAR(10) NOT NULL DEFAULT 'RUNNING'
                        CHECK (status IN ('RUNNING', 'COMPLETED', 'CANCELLED'))
);

CREATE INDEX idx_ab_tests_prompt_id_status ON ab_tests(prompt_id, status);
