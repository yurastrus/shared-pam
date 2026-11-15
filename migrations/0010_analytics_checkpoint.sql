-- Baseline for incremental PAM analytics. Existing analytics data is untouched.
CREATE TABLE IF NOT EXISTS pam_analytics_checkpoint (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    snapshot JSONB NOT NULL
);
