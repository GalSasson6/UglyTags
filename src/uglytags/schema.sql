CREATE TABLE IF NOT EXISTS tags (
    id BIGINT PRIMARY KEY,
    guild_id BIGINT NOT NULL,
    name TEXT NOT NULL CHECK (length(name) BETWEEN 1 AND 100),
    content TEXT NOT NULL DEFAULT '',
    owner_id BIGINT NOT NULL,
    reference_id BIGINT REFERENCES tags ON DELETE CASCADE,
    uses BIGINT NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    edited_at TIMESTAMPTZ,
    last_used_at TIMESTAMPTZ,
    CHECK ((reference_id IS NULL) = (content <> ''))
);

CREATE UNIQUE INDEX IF NOT EXISTS tags_guild_name ON tags (guild_id, lower(name));
CREATE INDEX IF NOT EXISTS tags_guild_owner ON tags (guild_id, owner_id);
CREATE INDEX IF NOT EXISTS tags_reference ON tags (reference_id) WHERE reference_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS tag_files (
    tag_id BIGINT NOT NULL REFERENCES tags ON DELETE CASCADE,
    filename TEXT NOT NULL CHECK (length(filename) BETWEEN 1 AND 100),
    data BYTEA NOT NULL CHECK (octet_length(data) > 0),
    PRIMARY KEY (tag_id, filename)
);

CREATE TABLE IF NOT EXISTS tag_variables (
    tag_id BIGINT NOT NULL REFERENCES tags ON DELETE CASCADE,
    scope TEXT NOT NULL CHECK (scope IN ('channel', 'server', 'user')),
    storage_id BIGINT NOT NULL,
    name TEXT NOT NULL CHECK (length(name) <= 128),
    value TEXT NOT NULL CHECK (length(value) <= 16384),
    PRIMARY KEY (tag_id, scope, storage_id, name)
);

-- Migration 2: tag groups. A group is a tag with no content that runs a random member.
ALTER TABLE tags ADD COLUMN IF NOT EXISTS is_group BOOLEAN NOT NULL DEFAULT FALSE;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'tags'::regclass AND conname = 'tags_kind_check'
    ) THEN
        ALTER TABLE tags DROP CONSTRAINT IF EXISTS tags_check;
        ALTER TABLE tags ADD CONSTRAINT tags_kind_check CHECK (
            CASE WHEN is_group THEN reference_id IS NULL AND content = ''
            ELSE (reference_id IS NULL) = (content <> '') END
        );
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS tag_group_members (
    group_id BIGINT NOT NULL REFERENCES tags ON DELETE CASCADE,
    tag_id BIGINT NOT NULL REFERENCES tags ON DELETE CASCADE,
    PRIMARY KEY (group_id, tag_id),
    CHECK (group_id <> tag_id)
);

CREATE INDEX IF NOT EXISTS tag_group_members_tag ON tag_group_members (tag_id);

-- Migration 3: per-use log for statistics, kept for one year.
CREATE TABLE IF NOT EXISTS tag_uses (
    tag_id BIGINT NOT NULL REFERENCES tags ON DELETE CASCADE,
    guild_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL,
    used_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS tag_uses_guild_time ON tag_uses (guild_id, used_at);
CREATE INDEX IF NOT EXISTS tag_uses_tag_time ON tag_uses (tag_id, used_at);
CREATE INDEX IF NOT EXISTS tag_uses_used_at ON tag_uses (used_at);

-- Migration 4: trigram similarity for "did you mean" suggestions on unknown tag names.
CREATE EXTENSION IF NOT EXISTS pg_trgm WITH SCHEMA public;
