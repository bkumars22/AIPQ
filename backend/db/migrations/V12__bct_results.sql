-- BCT (github.com/bkumars22/bct-framework) integrations — QAIP and
-- ZENTRAVIX adapters — push their behavioral-contract verification
-- result here for prompt-version quality tracking. Distinct from
-- prompt_versions.quality_score, which comes from AIPQ's own deepeval
-- evaluation pipeline, not from an external adversarial-pressure test.
CREATE TABLE bct_results (
    id                  BIGSERIAL PRIMARY KEY,
    prompt_id           BIGINT NOT NULL REFERENCES prompts(id) ON DELETE CASCADE,
    source_system       VARCHAR(50) NOT NULL,
    contract_name       VARCHAR(255) NOT NULL,
    overall_compliance  REAL NOT NULL,
    breaking_point      INTEGER,
    result              VARCHAR(20) NOT NULL,
    role_tested         VARCHAR(100),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_bct_results_prompt_created ON bct_results(prompt_id, created_at);
