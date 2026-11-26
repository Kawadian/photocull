CREATE TABLE people (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT,
    revision INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE faces (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    asset_id INTEGER NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
    person_id INTEGER NOT NULL REFERENCES people(id),
    key TEXT NOT NULL REFERENCES insight_models(key),
    content_hash TEXT NOT NULL,
    left REAL NOT NULL,
    top REAL NOT NULL,
    right REAL NOT NULL,
    bottom REAL NOT NULL,
    confidence REAL NOT NULL,
    quality REAL NOT NULL,
    vector BLOB NOT NULL,
    manual INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX faces_asset ON faces(asset_id, key);
CREATE INDEX faces_person ON faces(person_id, key, asset_id);
CREATE INDEX faces_model ON faces(key, id);

CREATE TRIGGER faces_delete_changed AFTER UPDATE OF content_hash ON assets
WHEN OLD.content_hash <> NEW.content_hash
BEGIN
    DELETE FROM faces WHERE asset_id = NEW.id;
END;
