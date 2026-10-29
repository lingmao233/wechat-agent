-- Scoped memory and profile migration for PostgreSQL deployments.
-- Existing rows are preserved.  SQLite development databases run the same
-- additive shape from Storage.init_schema().

CREATE EXTENSION IF NOT EXISTS pgcrypto;

ALTER TABLE memory_facts ADD COLUMN IF NOT EXISTS memory_id VARCHAR(128);
ALTER TABLE memory_facts ADD COLUMN IF NOT EXISTS scope_json TEXT NOT NULL DEFAULT '{}';
ALTER TABLE memory_facts ADD COLUMN IF NOT EXISTS source_message_ids_json TEXT NOT NULL DEFAULT '[]';
ALTER TABLE memory_facts ADD COLUMN IF NOT EXISTS task_id VARCHAR(256);
ALTER TABLE memory_facts ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ;
ALTER TABLE memory_facts ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ;
ALTER TABLE memory_facts ADD COLUMN IF NOT EXISTS valid_until TIMESTAMPTZ;
ALTER TABLE memory_facts ADD COLUMN IF NOT EXISTS importance DOUBLE PRECISION NOT NULL DEFAULT 0.5;
ALTER TABLE memory_facts ADD COLUMN IF NOT EXISTS embedding_model VARCHAR(256) NOT NULL DEFAULT '';
ALTER TABLE memory_facts ADD COLUMN IF NOT EXISTS embedding_json TEXT;

ALTER TABLE messages ADD COLUMN IF NOT EXISTS external_message_id VARCHAR(256);
ALTER TABLE messages ADD COLUMN IF NOT EXISTS thread_id VARCHAR(256);
ALTER TABLE messages ADD COLUMN IF NOT EXISTS task_id VARCHAR(256);
ALTER TABLE messages ADD COLUMN IF NOT EXISTS reply_to_json TEXT NOT NULL DEFAULT '{}';
ALTER TABLE messages ADD COLUMN IF NOT EXISTS is_self BOOLEAN NOT NULL DEFAULT FALSE;
CREATE INDEX IF NOT EXISTS messages_thread_scope
    ON messages (account_id, group_id, thread_id, observed_at DESC);
CREATE INDEX IF NOT EXISTS messages_task_scope
    ON messages (account_id, group_id, task_id, observed_at DESC);

UPDATE memory_facts
SET memory_id = encode(digest(
    account_id || E'\x1f' || group_id || E'\x1f' || member_id || E'\x1f' ||
    memory_type || E'\x1f' || content || E'\x1f' || COALESCE(source_event_id, '') ||
    E'\x1f' || id::text,
    'sha256'
), 'hex')
WHERE memory_id IS NULL;

UPDATE memory_facts SET created_at = observed_at WHERE created_at IS NULL;
UPDATE memory_facts SET updated_at = observed_at WHERE updated_at IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS memory_facts_memory_id_uq ON memory_facts (memory_id);
CREATE INDEX IF NOT EXISTS memory_facts_scoped_status
    ON memory_facts (account_id, group_id, member_id, task_id, status, updated_at DESC);

CREATE TABLE IF NOT EXISTS user_profiles (
    id BIGSERIAL PRIMARY KEY,
    account_id VARCHAR(128) NOT NULL,
    group_id VARCHAR(256) NOT NULL,
    member_id VARCHAR(256) NOT NULL,
    member_name VARCHAR(256) NOT NULL DEFAULT '',
    profile_json TEXT NOT NULL DEFAULT '{}',
    history_json TEXT NOT NULL DEFAULT '[]',
    source_message_ids_json TEXT NOT NULL DEFAULT '[]',
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    CONSTRAINT uq_user_profile_scope UNIQUE (account_id, group_id, member_id)
);
CREATE INDEX IF NOT EXISTS user_profiles_updated_at ON user_profiles (updated_at DESC);

CREATE TABLE IF NOT EXISTS ai_profiles (
    id BIGSERIAL PRIMARY KEY,
    account_id VARCHAR(128) NOT NULL,
    group_id VARCHAR(256) NOT NULL,
    group_name VARCHAR(256) NOT NULL DEFAULT '',
    profile_json TEXT NOT NULL DEFAULT '{}',
    history_json TEXT NOT NULL DEFAULT '[]',
    source_message_ids_json TEXT NOT NULL DEFAULT '[]',
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    CONSTRAINT uq_ai_profile_scope UNIQUE (account_id, group_id)
);
CREATE INDEX IF NOT EXISTS ai_profiles_updated_at ON ai_profiles (updated_at DESC);

CREATE TABLE IF NOT EXISTS task_profiles (
    id BIGSERIAL PRIMARY KEY,
    account_id VARCHAR(128) NOT NULL,
    group_id VARCHAR(256) NOT NULL,
    task_id VARCHAR(256) NOT NULL,
    member_id VARCHAR(256) NOT NULL DEFAULT '',
    profile_json TEXT NOT NULL DEFAULT '{}',
    history_json TEXT NOT NULL DEFAULT '[]',
    source_message_ids_json TEXT NOT NULL DEFAULT '[]',
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    CONSTRAINT uq_task_profile_scope UNIQUE (account_id, group_id, task_id, member_id)
);
CREATE INDEX IF NOT EXISTS task_profiles_updated_at ON task_profiles (updated_at DESC);

