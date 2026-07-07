CREATE TABLE golden_cases (
    id                  BIGSERIAL PRIMARY KEY,
    dataset_id          BIGINT NOT NULL REFERENCES golden_datasets(id) ON DELETE CASCADE,
    input_text          TEXT NOT NULL,
    expected_behavior   TEXT NOT NULL,
    forbidden_patterns  JSONB NOT NULL DEFAULT '[]',
    required_patterns   JSONB NOT NULL DEFAULT '[]',
    category            VARCHAR(50) NOT NULL DEFAULT 'baseline',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_golden_cases_dataset_category ON golden_cases(dataset_id, category);
