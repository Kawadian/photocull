CREATE TABLE archive_reindex (
    path TEXT PRIMARY KEY,
    source_path TEXT NOT NULL,
    asset_id INTEGER NOT NULL REFERENCES assets(id),
    before_hash TEXT NOT NULL,
    after_hash TEXT NOT NULL,
    ready INTEGER NOT NULL DEFAULT 0 CHECK(ready IN (0,1))
);

CREATE INDEX archive_reindex_held ON archive_reindex(asset_id) WHERE ready=0;

DROP TRIGGER faces_delete_changed;
CREATE TRIGGER faces_delete_changed AFTER UPDATE OF content_hash ON assets
WHEN OLD.content_hash <> NEW.content_hash
 AND NOT EXISTS (
     SELECT 1 FROM archive_reindex WHERE path=NEW.path AND asset_id=NEW.id AND ready=1
       AND before_hash=OLD.content_hash AND after_hash=NEW.content_hash
 )
BEGIN
    DELETE FROM faces WHERE asset_id = NEW.id;
END;

CREATE TRIGGER face_index_archive_change AFTER UPDATE OF content_hash ON assets
WHEN OLD.content_hash <> NEW.content_hash
BEGIN
    INSERT INTO face_index_dirty SELECT id FROM faces WHERE asset_id=NEW.id
    ON CONFLICT DO NOTHING;
END;

CREATE TRIGGER face_index_recomputed AFTER UPDATE OF content_hash ON faces
WHEN OLD.content_hash <> NEW.content_hash
BEGIN
    INSERT INTO face_index_dirty VALUES(NEW.id) ON CONFLICT DO NOTHING;
END;
