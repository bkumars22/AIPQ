CREATE TABLE rollbacks (
    id              BIGSERIAL PRIMARY KEY,
    prompt_id       BIGINT NOT NULL REFERENCES prompts(id) ON DELETE CASCADE,
    from_version_id BIGINT NOT NULL REFERENCES prompt_versions(id),
    to_version_id   BIGINT NOT NULL REFERENCES prompt_versions(id),
    triggered_by    VARCHAR(10) NOT NULL DEFAULT 'MANUAL'
                    CHECK (triggered_by IN ('AUTOMATIC', 'MANUAL')),
    reason          TEXT,
    triggered_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at     TIMESTAMPTZ
);

CREATE INDEX idx_rollbacks_prompt_id ON rollbacks(prompt_id);
