ALTER TABLE assets ADD COLUMN lens TEXT;
ALTER TABLE assets ADD COLUMN shutter_seconds REAL;
ALTER TABLE assets ADD COLUMN aperture REAL;
ALTER TABLE assets ADD COLUMN focal_length_mm REAL;
ALTER TABLE assets ADD COLUMN iso INTEGER;

CREATE TRIGGER reset_browser_exif AFTER UPDATE OF content_hash ON assets
WHEN OLD.content_hash <> NEW.content_hash
BEGIN
    UPDATE assets SET lens = NULL, shutter_seconds = NULL, aperture = NULL,
        focal_length_mm = NULL, iso = NULL WHERE id = NEW.id;
END;

UPDATE jobs SET status = 'pending', attempts = 0, interruptions = 0
    WHERE stage = 'exif' AND status = 'done'
    AND EXISTS (SELECT 1 FROM assets WHERE id = jobs.asset_id AND active = 1);
