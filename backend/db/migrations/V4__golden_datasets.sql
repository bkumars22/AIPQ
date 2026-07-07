CREATE TABLE golden_datasets (
    id          BIGSERIAL PRIMARY KEY,
    project_id  BIGINT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    prompt_id   BIGINT NOT NULL REFERENCES prompts(id) ON DELETE CASCADE,
    name        VARCHAR(255) NOT NULL,
    description TEXT,
    threshold   REAL NOT NULL DEFAULT 0.85,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT uq_golden_datasets_project_name UNIQUE (project_id, name)
);

CREATE INDEX idx_golden_datasets_prompt_id ON golden_datasets(prompt_id);
