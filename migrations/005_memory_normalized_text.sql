-- Additive migration for existing PostgreSQL deployments.
-- Raw message text stays in messages.text; the derived fields below are
-- rebuilt from it by `python -m wechat_agent rebuild-memory`.
ALTER TABLE messages
    ADD COLUMN IF NOT EXISTS text_normalized TEXT NOT NULL DEFAULT '';
ALTER TABLE messages
    ADD COLUMN IF NOT EXISTS text_kind VARCHAR(16) NOT NULL DEFAULT 'text';
ALTER TABLE messages
    ADD COLUMN IF NOT EXISTS text_meta_json TEXT NOT NULL DEFAULT '{}';

CREATE INDEX IF NOT EXISTS ix_messages_text_kind
    ON messages (text_kind);
