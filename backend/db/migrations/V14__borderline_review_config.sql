-- Human-in-the-loop borderline review support.
--
-- borderline_lower/upper are nullable and NULL by default: an existing
-- golden_dataset with no values set here keeps the exact pre-existing
-- pure-threshold pass/fail behavior untouched. A dataset only enters the
-- three-way (auto-reject / auto-approve / human-review) routing once both
-- are explicitly configured.
ALTER TABLE golden_datasets
    ADD COLUMN borderline_lower REAL,
    ADD COLUMN borderline_upper REAL,
    ADD COLUMN review_timeout_hours REAL NOT NULL DEFAULT 48;

ALTER TABLE golden_datasets
    ADD CONSTRAINT chk_borderline_bounds
    CHECK (
        borderline_lower IS NULL OR borderline_upper IS NULL
        OR borderline_lower < borderline_upper
    );

-- One row per paused human-review thread. Populated only for genuinely
-- borderline evaluations (auto-reject/auto-approve never create a row
-- here) -- this is what "aipq pending-reviews" and the timeout sweep query.
CREATE TABLE pending_reviews (
    id              BIGSERIAL PRIMARY KEY,
    thread_id       TEXT NOT NULL UNIQUE,
    version_id      BIGINT NOT NULL REFERENCES prompt_versions(id) ON DELETE CASCADE,
    prompt_id       BIGINT NOT NULL REFERENCES prompts(id) ON DELETE CASCADE,
    new_score       REAL NOT NULL,
    current_score   REAL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    timeout_at      TIMESTAMPTZ NOT NULL,
    resolved_at     TIMESTAMPTZ,
    resolution      TEXT
        CHECK (resolution IN ('approved_by_human', 'rejected_by_human', 'timed_out'))
);

CREATE INDEX idx_pending_reviews_unresolved ON pending_reviews(timeout_at) WHERE resolved_at IS NULL;
