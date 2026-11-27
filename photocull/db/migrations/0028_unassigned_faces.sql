DROP TRIGGER faces_delete_changed;
DROP TRIGGER face_index_visibility;
DROP TRIGGER face_index_archive_change;
DROP TRIGGER face_index_insert;
DROP TRIGGER face_index_update;
DROP TRIGGER face_index_delete;
DROP TRIGGER face_index_recomputed;

CREATE TABLE faces_new (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    asset_id INTEGER NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
    person_id INTEGER REFERENCES people(id),
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
INSERT INTO faces_new
SELECT f.id, f.asset_id,
       CASE WHEN f.manual = 1 OR p.name IS NOT NULL OR f.quality >= 0.12 THEN f.person_id END,
       f.key, f.content_hash, f.left, f.top, f.right, f.bottom, f.confidence,
       f.quality, f.vector, f.manual
FROM faces AS f JOIN people AS p ON p.id = f.person_id;

-- Preserve the high-water mark for databases that already deleted faces.
UPDATE sqlite_sequence SET seq = MAX(seq, (SELECT seq FROM sqlite_sequence WHERE name = 'faces'))
WHERE name = 'faces_new' AND EXISTS (SELECT 1 FROM sqlite_sequence WHERE name = 'faces');
INSERT INTO sqlite_sequence(name, seq)
SELECT 'faces_new', seq FROM sqlite_sequence
WHERE name = 'faces' AND NOT EXISTS (SELECT 1 FROM sqlite_sequence WHERE name = 'faces_new');
DROP TABLE faces;
ALTER TABLE faces_new RENAME TO faces;
DELETE FROM people WHERE name IS NULL AND NOT EXISTS (
    SELECT 1 FROM faces WHERE person_id = people.id
);

CREATE INDEX faces_asset ON faces(asset_id, key);
CREATE INDEX faces_person ON faces(person_id, key, asset_id);
CREATE INDEX faces_model ON faces(key, id);
CREATE INDEX faces_unassigned ON faces(key, id) WHERE person_id IS NULL;

CREATE TRIGGER faces_delete_changed AFTER UPDATE OF content_hash ON assets
WHEN OLD.content_hash <> NEW.content_hash
 AND NOT EXISTS (
     SELECT 1 FROM archive_reindex WHERE path=NEW.path AND asset_id=NEW.id AND ready=1
       AND before_hash=OLD.content_hash AND after_hash=NEW.content_hash
 )
BEGIN
    DELETE FROM faces WHERE asset_id = NEW.id;
END;

CREATE TRIGGER face_index_insert AFTER INSERT ON faces BEGIN
    INSERT OR IGNORE INTO face_index_dirty VALUES (NEW.id);
END;

CREATE TRIGGER face_index_update AFTER UPDATE OF key, quality, vector, person_id, content_hash
ON faces
WHEN OLD.key <> NEW.key OR OLD.vector <> NEW.vector OR OLD.quality <> NEW.quality
  OR (OLD.person_id IS NULL) <> (NEW.person_id IS NULL) OR OLD.content_hash <> NEW.content_hash
BEGIN
    INSERT OR IGNORE INTO face_index_dirty VALUES (NEW.id);
END;

CREATE TRIGGER face_index_delete AFTER DELETE ON faces BEGIN
    INSERT OR IGNORE INTO face_index_dirty VALUES (OLD.id);
END;

CREATE TRIGGER face_index_visibility AFTER UPDATE OF active, content_hash ON assets
WHEN OLD.active <> NEW.active OR OLD.content_hash <> NEW.content_hash
BEGIN
    INSERT INTO face_index_dirty SELECT id FROM faces WHERE asset_id = NEW.id
    ON CONFLICT DO NOTHING;
END;
