CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE projects (
    id              BIGSERIAL PRIMARY KEY,
    name            VARCHAR(255) NOT NULL UNIQUE,
    description     TEXT,
    owner_email     VARCHAR(255) NOT NULL,
    webhook_secret  VARCHAR(255) NOT NULL,
    pipeline_type   VARCHAR(20) NOT NULL DEFAULT 'CUSTOM'
                    CHECK (pipeline_type IN ('LANGGRAPH', 'LANGCHAIN', 'CUSTOM')),
    is_active       BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_projects_owner_email ON projects(owner_email);
