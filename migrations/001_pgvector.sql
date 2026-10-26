-- Run on PostgreSQL after enabling the pgvector extension.
CREATE EXTENSION IF NOT EXISTS vector;

-- The Python prototype keeps embeddings optional so SQLite can be used for
-- local tests. This migration adds the production vector column and index.
ALTER TABLE memory_facts
    ADD COLUMN IF NOT EXISTS embedding vector(1024);

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
