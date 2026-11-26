CREATE TABLE burst_changes (
    captured_at TEXT PRIMARY KEY
) WITHOUT ROWID;

-- Changes and the job results that caused them must commit together.
CREATE TRIGGER scores_insert_burst_change AFTER INSERT ON asset_scores BEGIN
    INSERT INTO burst_changes
        SELECT captured_at FROM assets
        WHERE id = NEW.asset_id AND active = 1 AND captured_at IS NOT NULL
        ON CONFLICT DO NOTHING;
END;

CREATE TRIGGER scores_update_burst_change
AFTER UPDATE OF sharpness, exposure, clipped_highlights, clipped_shadows, dhash ON asset_scores
WHEN OLD.sharpness <> NEW.sharpness OR OLD.exposure <> NEW.exposure
     OR OLD.clipped_highlights <> NEW.clipped_highlights
     OR OLD.clipped_shadows <> NEW.clipped_shadows OR OLD.dhash <> NEW.dhash
BEGIN
    INSERT INTO burst_changes
        SELECT captured_at FROM assets
        WHERE id = NEW.asset_id AND active = 1 AND captured_at IS NOT NULL
        ON CONFLICT DO NOTHING;
    DELETE FROM bursts WHERE OLD.dhash <> NEW.dhash AND id = (
        SELECT burst_id FROM burst_members WHERE asset_id = NEW.asset_id
    );
END;

CREATE TRIGGER scores_delete_burst_change AFTER DELETE ON asset_scores BEGIN
    INSERT INTO burst_changes
        SELECT captured_at FROM assets
        WHERE id = OLD.asset_id AND active = 1 AND captured_at IS NOT NULL
        ON CONFLICT DO NOTHING;
    DELETE FROM bursts WHERE id = (
        SELECT burst_id FROM burst_members WHERE asset_id = OLD.asset_id
    );
END;

CREATE TRIGGER assets_update_burst_change
AFTER UPDATE OF active, captured_at, content_hash ON assets
WHEN (OLD.active <> NEW.active OR OLD.captured_at IS NOT NEW.captured_at
      OR OLD.content_hash <> NEW.content_hash)
     AND EXISTS (SELECT 1 FROM asset_scores WHERE asset_id = OLD.id)
BEGIN
    INSERT INTO burst_changes
        SELECT OLD.captured_at WHERE OLD.active = 1 AND OLD.captured_at IS NOT NULL
        ON CONFLICT DO NOTHING;
    INSERT INTO burst_changes
        SELECT NEW.captured_at WHERE NEW.active = 1 AND NEW.captured_at IS NOT NULL
        ON CONFLICT DO NOTHING;
    DELETE FROM bursts WHERE id = (
        SELECT burst_id FROM burst_members WHERE asset_id = OLD.id
    );
END;
