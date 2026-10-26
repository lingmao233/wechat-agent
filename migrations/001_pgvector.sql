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
