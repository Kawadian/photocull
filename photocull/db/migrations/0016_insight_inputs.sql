CREATE TRIGGER insights_taste_aesthetic_insert AFTER INSERT ON insight_results
WHEN NEW.stage = 'aesthetic' AND NEW.score IS NOT NULL
BEGIN
    UPDATE taste_state SET requested = requested + 1 WHERE EXISTS (
        SELECT 1 FROM assets WHERE id = NEW.asset_id AND active = 1 AND (rating > 0 OR rejected = 1)
    );
END;

CREATE TRIGGER insights_aesthetic_removed AFTER DELETE ON insight_results
WHEN OLD.stage = 'aesthetic' AND OLD.score IS NOT NULL
BEGIN
    INSERT OR IGNORE INTO burst_changes
        SELECT captured_at FROM assets
        WHERE id = OLD.asset_id AND active = 1 AND captured_at IS NOT NULL;
    INSERT OR IGNORE INTO insight_work(stage, asset_id)
        SELECT stage, OLD.asset_id FROM insight_stages
        WHERE stage = 'taste' AND EXISTS(SELECT 1 FROM assets WHERE id = OLD.asset_id AND active = 1);
    UPDATE taste_state SET requested = requested + 1 WHERE EXISTS (
        SELECT 1 FROM assets WHERE id = OLD.asset_id AND active = 1 AND (rating > 0 OR rejected = 1)
    );
    INSERT INTO story_dirty(day) SELECT day FROM assets WHERE id = OLD.asset_id AND active = 1
      AND (SELECT key IS NOT NULL FROM story_state)
    ON CONFLICT(day) DO UPDATE SET generation = generation + 1;
END;

CREATE TRIGGER story_embedding_insert AFTER INSERT ON asset_embeddings
WHEN (SELECT key IS NOT NULL FROM story_state)
BEGIN
    INSERT INTO story_dirty(day) SELECT day FROM assets WHERE id = NEW.asset_id AND active = 1
    ON CONFLICT(day) DO UPDATE SET generation = generation + 1;
END;

CREATE TRIGGER story_taste_embedding_update AFTER UPDATE OF model_key, vector ON asset_embeddings
WHEN OLD.model_key <> NEW.model_key OR OLD.vector <> NEW.vector
BEGIN
    INSERT INTO story_dirty(day) SELECT day FROM assets WHERE id = NEW.asset_id AND active = 1
      AND (SELECT key IS NOT NULL FROM story_state)
    ON CONFLICT(day) DO UPDATE SET generation = generation + 1;
    UPDATE taste_state SET requested = requested + 1 WHERE EXISTS (
        SELECT 1 FROM assets WHERE id = NEW.asset_id AND active = 1 AND (rating > 0 OR rejected = 1)
    );
END;
