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

CREATE TABLE video_cleanup (
    asset_id INTEGER PRIMARY KEY REFERENCES assets(id) ON DELETE CASCADE
);

CREATE TRIGGER video_cleanup_changed AFTER UPDATE OF content_hash, active ON assets
WHEN OLD.kind = 'video' AND (OLD.content_hash <> NEW.content_hash OR NEW.active < OLD.active)
BEGIN
    INSERT OR IGNORE INTO video_cleanup VALUES(NEW.id);
END;
