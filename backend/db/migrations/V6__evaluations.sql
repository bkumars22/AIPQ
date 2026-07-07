CREATE TABLE evaluations (
    id                  BIGSERIAL PRIMARY KEY,
    prompt_version_id   BIGINT NOT NULL REFERENCES prompt_versions(id) ON DELETE CASCADE,
    golden_dataset_id   BIGINT NOT NULL REFERENCES golden_datasets(id) ON DELETE CASCADE,
    evaluated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    total_cases         INTEGER NOT NULL DEFAULT 0,
    passed_cases        INTEGER NOT NULL DEFAULT 0,
    failed_cases        INTEGER NOT NULL DEFAULT 0,
    faithfulness_score  REAL,
    compliance_score    REAL,
    passed              BOOLEAN NOT NULL DEFAULT FALSE,
    blocked_deployment  BOOLEAN NOT NULL DEFAULT FALSE,
    details             JSONB NOT NULL DEFAULT '{}'
);

CREATE INDEX idx_evaluations_version_evaluated_at ON evaluations(prompt_version_id, evaluated_at);
