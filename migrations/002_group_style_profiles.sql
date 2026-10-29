-- Aggregate-only group chat style profiles.  The Python init-db path creates
-- the same table through SQLAlchemy; this file is for explicit PostgreSQL
-- deployments that run migrations separately.
CREATE TABLE IF NOT EXISTS group_style_profiles (
    id BIGSERIAL PRIMARY KEY,
    account_id VARCHAR(128) NOT NULL,
    group_id VARCHAR(256) NOT NULL,
    group_name VARCHAR(256) NOT NULL,
    profile_text TEXT NOT NULL DEFAULT '',
    stats_json TEXT NOT NULL DEFAULT '{}',
    observed_count INTEGER NOT NULL DEFAULT 0,
    last_refresh_count INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    last_refreshed_at TIMESTAMPTZ,
    CONSTRAINT uq_group_style_scope UNIQUE (account_id, group_id)
);

CREATE INDEX IF NOT EXISTS group_style_profiles_updated_at
    ON group_style_profiles (updated_at DESC);
