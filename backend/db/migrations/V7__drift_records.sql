CREATE TABLE drift_records (
    id                  BIGSERIAL PRIMARY KEY,
    prompt_version_id   BIGINT NOT NULL REFERENCES prompt_versions(id) ON DELETE CASCADE,
    recorded_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    faithfulness_score  REAL NOT NULL,
    compliance_score    REAL NOT NULL,
    sample_size         INTEGER NOT NULL DEFAULT 1,
    is_anomaly          BOOLEAN NOT NULL DEFAULT FALSE,
    drift_severity      VARCHAR(10) NOT NULL DEFAULT 'NONE'
                        CHECK (drift_severity IN ('NONE', 'LOW', 'HIGH', 'CRITICAL'))
);

CREATE INDEX idx_drift_records_version_recorded_at ON drift_records(prompt_version_id, recorded_at);
