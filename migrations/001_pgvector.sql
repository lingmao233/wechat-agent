-- Run on PostgreSQL after enabling the pgvector extension.
CREATE EXTENSION IF NOT EXISTS vector;

-- The Python prototype keeps embeddings optional so SQLite can be used for
-- local tests. The configured Ark model (doubao-embedding-vision-251215)
-- currently returns 2048 dimensions; change this dimension together with
-- EMBED_DIMENSION and the deployed model before applying a different model.
ALTER TABLE memory_facts
    ADD COLUMN IF NOT EXISTS embedding vector(2048);

CREATE INDEX IF NOT EXISTS memory_facts_embedding_hnsw
    ON memory_facts USING hnsw (embedding vector_cosine_ops);

CREATE INDEX IF NOT EXISTS memory_facts_scope_time
    ON memory_facts (account_id, group_id, member_id, observed_at DESC);

-- Durable inbound idempotency and answer ledger used by the agent.  A
-- successful client send is the only transition that blocks a repeated
-- normalized question; skipped and failed rows remain retryable.
CREATE TABLE IF NOT EXISTS reply_records (
    id BIGSERIAL PRIMARY KEY,
    event_key VARCHAR(128) NOT NULL UNIQUE,
    question_hash VARCHAR(64) NOT NULL,
    account_id VARCHAR(128) NOT NULL,
    group_id VARCHAR(256) NOT NULL,
    group_name VARCHAR(256) NOT NULL,
    member_id VARCHAR(256) NOT NULL,
    member_name VARCHAR(256) NOT NULL,
    text TEXT NOT NULL,
    status VARCHAR(32) NOT NULL,
    reason VARCHAR(512) NOT NULL DEFAULT '',
    reply_text TEXT,
    source VARCHAR(64) NOT NULL DEFAULT 'weixin_ui',
    attempt_count INTEGER NOT NULL DEFAULT 1,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    answered_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS reply_records_question_scope
    ON reply_records (account_id, group_id, member_id, question_hash, status);

CREATE INDEX IF NOT EXISTS reply_records_updated_at
    ON reply_records (updated_at DESC);

-- Platform comment/reply plans are separate from Weixin action_audit.  The
-- default status is a dry-run plan and must be explicitly reviewed before any
-- real browser adapter is allowed to execute it.
CREATE TABLE IF NOT EXISTS platform_actions (
    id BIGSERIAL PRIMARY KEY,
    action_key VARCHAR(64) NOT NULL UNIQUE,
    platform VARCHAR(32) NOT NULL,
    account_id VARCHAR(128) NOT NULL,
    action_type VARCHAR(32) NOT NULL,
    target_id VARCHAR(256) NOT NULL,
    parent_comment_id VARCHAR(256),
    target_url VARCHAR(2048),
    text TEXT NOT NULL,
    status VARCHAR(32) NOT NULL,
    reason VARCHAR(512) NOT NULL DEFAULT '',
    dry_run BOOLEAN NOT NULL DEFAULT TRUE,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    executed_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS platform_actions_scope
    ON platform_actions (platform, account_id, action_type, target_id, status);

CREATE INDEX IF NOT EXISTS platform_actions_updated_at
    ON platform_actions (updated_at DESC);
