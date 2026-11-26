CREATE TABLE face_index_dirty (id INTEGER PRIMARY KEY);

CREATE TRIGGER face_index_insert AFTER INSERT ON faces BEGIN
    INSERT OR IGNORE INTO face_index_dirty VALUES (NEW.id);
END;

CREATE TRIGGER face_index_update AFTER UPDATE OF key, quality, vector ON faces
WHEN OLD.key <> NEW.key OR OLD.vector <> NEW.vector OR (OLD.quality >= 0.12) <> (NEW.quality >= 0.12)
BEGIN
    INSERT OR IGNORE INTO face_index_dirty VALUES (NEW.id);
END;

CREATE TRIGGER face_index_delete AFTER DELETE ON faces BEGIN
    INSERT OR IGNORE INTO face_index_dirty VALUES (OLD.id);
END;

CREATE TRIGGER face_index_visibility AFTER UPDATE OF active ON assets
WHEN OLD.active <> NEW.active
BEGIN
    INSERT OR IGNORE INTO face_index_dirty SELECT id FROM faces WHERE asset_id = NEW.id;
END;
