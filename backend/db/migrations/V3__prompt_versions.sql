CREATE TABLE prompt_versions (
    id              BIGSERIAL PRIMARY KEY,
    prompt_id       BIGINT NOT NULL REFERENCES prompts(id) ON DELETE CASCADE,
    version_number  INTEGER NOT NULL,
    content         TEXT NOT NULL,
    system_prompt   TEXT,
    temperature     REAL DEFAULT 0.3,
    max_tokens      INTEGER DEFAULT 4096,
    changed_by      VARCHAR(255) NOT NULL,
    change_message  TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    deployed_at     TIMESTAMPTZ,
    quality_score   REAL,
    status          VARCHAR(20) NOT NULL DEFAULT 'TESTING'
                    CHECK (status IN ('TESTING', 'DEPLOYED', 'ROLLED_BACK', 'FAILED')),

    CONSTRAINT uq_prompt_versions_number UNIQUE (prompt_id, version_number)
);

CREATE INDEX idx_prompt_versions_prompt_status ON prompt_versions(prompt_id, status);
CREATE INDEX idx_prompt_versions_deployed_at ON prompt_versions(deployed_at);

ALTER TABLE prompts
    ADD CONSTRAINT fk_prompts_current_version
    FOREIGN KEY (current_version_id) REFERENCES prompt_versions(id) ON DELETE SET NULL;
