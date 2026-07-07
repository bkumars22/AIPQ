CREATE TABLE prompts (
    id                  BIGSERIAL PRIMARY KEY,
    project_id          BIGINT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    prompt_name         VARCHAR(255) NOT NULL,
    description         TEXT,
    current_version_id BIGINT,  -- FK added in V3 once prompt_versions exists
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT uq_prompts_project_name UNIQUE (project_id, prompt_name)
);

CREATE INDEX idx_prompts_project_id ON prompts(project_id);
