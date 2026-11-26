CREATE TABLE video_flags (
    asset_id INTEGER PRIMARY KEY REFERENCES assets(id) ON DELETE CASCADE,
    content_hash TEXT NOT NULL,
    dead INTEGER NOT NULL CHECK(dead IN (0, 1))
);

CREATE TRIGGER video_flags_changed AFTER UPDATE OF content_hash ON assets
WHEN OLD.content_hash <> NEW.content_hash
BEGIN
    DELETE FROM video_flags WHERE asset_id = NEW.id;
END;
