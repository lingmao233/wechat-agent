-- Additive migration for existing PostgreSQL deployments.
-- The application stores only bounded local attachment paths in this column;
-- image bytes are never persisted in the message table.
ALTER TABLE messages
    ADD COLUMN IF NOT EXISTS image_paths_json TEXT NOT NULL DEFAULT '[]';
