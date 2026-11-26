CREATE TABLE insight_models (
    key TEXT PRIMARY KEY,
    model_id TEXT NOT NULL,
    revision TEXT NOT NULL
) WITHOUT ROWID;

CREATE TABLE insight_stages (
    stage TEXT PRIMARY KEY,
    key TEXT NOT NULL REFERENCES insight_models(key)
) WITHOUT ROWID;

CREATE TABLE insight_work (
    stage TEXT NOT NULL REFERENCES insight_stages(stage) ON DELETE CASCADE,
    asset_id INTEGER NOT NULL REFERENCES assets(id),
    attempts INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (stage, asset_id)
) WITHOUT ROWID;

CREATE TABLE insight_results (
    stage TEXT NOT NULL,
    asset_id INTEGER NOT NULL REFERENCES assets(id),
    key TEXT NOT NULL REFERENCES insight_models(key),
    content_hash TEXT NOT NULL,
    score REAL,
    payload TEXT,
    error TEXT,
    PRIMARY KEY (stage, asset_id)
) WITHOUT ROWID;

CREATE INDEX insight_result_scores ON insight_results(stage, key, score DESC, asset_id);
CREATE INDEX insight_errors ON insight_results(stage, key) WHERE error IS NOT NULL;
CREATE INDEX insight_work_asset ON insight_work(asset_id);

CREATE TRIGGER insights_asset_changed AFTER UPDATE OF content_hash, active ON assets
WHEN OLD.content_hash <> NEW.content_hash OR OLD.active <> NEW.active
BEGIN
    DELETE FROM insight_results WHERE asset_id = NEW.id;
    DELETE FROM insight_work WHERE asset_id = NEW.id;
    INSERT OR IGNORE INTO insight_work(stage, asset_id)
        SELECT stage, NEW.id FROM insight_stages
        WHERE NEW.active = 1 AND (NEW.kind = 'video') = (stage IN ('video', 'transcript'));
END;

CREATE TRIGGER insights_asset_added AFTER INSERT ON assets BEGIN
    INSERT OR IGNORE INTO insight_work(stage, asset_id)
        SELECT stage, NEW.id FROM insight_stages
        WHERE (NEW.kind = 'video') = (stage IN ('video', 'transcript'));
END;

CREATE TRIGGER insights_preview_done AFTER UPDATE OF status ON jobs
WHEN NEW.stage = 'preview' AND NEW.status = 'done' AND OLD.status <> NEW.status
BEGIN
    INSERT OR IGNORE INTO insight_work(stage, asset_id)
        SELECT stage, NEW.asset_id FROM insight_stages WHERE stage IN ('faces');
END;

CREATE TRIGGER insights_embedding_insert AFTER INSERT ON asset_embeddings BEGIN
    INSERT OR IGNORE INTO insight_work(stage, asset_id)
        SELECT stage, NEW.asset_id FROM insight_stages WHERE stage IN ('aesthetic', 'taste');
END;

CREATE TRIGGER insights_embedding_update AFTER UPDATE OF model_key, vector ON asset_embeddings
WHEN OLD.model_key <> NEW.model_key OR OLD.vector <> NEW.vector
BEGIN
    INSERT OR IGNORE INTO insight_work(stage, asset_id)
        SELECT stage, NEW.asset_id FROM insight_stages WHERE stage IN ('aesthetic', 'taste');
END;

CREATE TRIGGER insights_burst_score AFTER INSERT ON insight_results
WHEN NEW.stage = 'aesthetic' AND NEW.score IS NOT NULL
BEGIN
    INSERT INTO burst_changes SELECT captured_at FROM assets
        WHERE id = NEW.asset_id AND active = 1 AND captured_at IS NOT NULL
        ON CONFLICT DO NOTHING;
    INSERT OR IGNORE INTO insight_work(stage, asset_id)
        SELECT stage, NEW.asset_id FROM insight_stages WHERE stage = 'taste';
END;

CREATE TABLE taste_state (
    id INTEGER PRIMARY KEY CHECK(id = 1),
    requested INTEGER NOT NULL DEFAULT 1,
    completed INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'pending',
    positives INTEGER NOT NULL DEFAULT 0,
    negatives INTEGER NOT NULL DEFAULT 0,
    model_key TEXT REFERENCES insight_models(key),
    input_key TEXT,
    coefficients BLOB,
    error TEXT
);
INSERT INTO taste_state(id) VALUES (1);

CREATE TRIGGER insights_taste_labels AFTER UPDATE OF rating, rejected ON assets
WHEN OLD.rating <> NEW.rating OR OLD.rejected <> NEW.rejected
BEGIN
    UPDATE taste_state SET requested = requested + 1;
END;

CREATE TRIGGER insights_taste_embedding AFTER INSERT ON asset_embeddings
WHEN EXISTS (SELECT 1 FROM assets WHERE id = NEW.asset_id AND (rating > 0 OR rejected = 1))
BEGIN
    UPDATE taste_state SET requested = requested + 1;
END;

CREATE TRIGGER insights_taste_scores AFTER UPDATE ON asset_scores
WHEN EXISTS (SELECT 1 FROM assets WHERE id = NEW.asset_id AND (rating > 0 OR rejected = 1))
BEGIN
    UPDATE taste_state SET requested = requested + 1;
END;

CREATE TRIGGER insights_taste_retired AFTER UPDATE OF content_hash, active ON assets
WHEN (OLD.content_hash <> NEW.content_hash OR OLD.active <> NEW.active)
 AND (NEW.rating > 0 OR NEW.rejected = 1)
BEGIN
    UPDATE taste_state SET requested = requested + 1;
END;
