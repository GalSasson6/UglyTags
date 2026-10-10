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
