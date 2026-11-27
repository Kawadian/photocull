CREATE TABLE albums (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    name_key TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL CHECK (kind IN ('manual', 'smart')),
    library TEXT,
    filters TEXT,
    revision INTEGER NOT NULL DEFAULT 0,
    CHECK ((kind = 'manual' AND library IS NULL AND filters IS NULL)
           OR (kind = 'smart' AND json_valid(filters)))
);

CREATE TABLE album_members (
    album_id TEXT NOT NULL REFERENCES albums(id) ON DELETE CASCADE,
    library TEXT NOT NULL,
    asset_id INTEGER NOT NULL,
    PRIMARY KEY (album_id, library, asset_id)
) WITHOUT ROWID;

CREATE INDEX album_asset ON album_members (library, asset_id, album_id);
