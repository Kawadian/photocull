DROP TRIGGER jobs_insert;
DROP TRIGGER jobs_update;
DROP TRIGGER jobs_delete;
DROP INDEX jobs_pending;
DROP INDEX jobs_stage_status;

ALTER TABLE jobs RENAME TO phase2_jobs;

CREATE TABLE jobs (
    asset_id INTEGER NOT NULL REFERENCES assets(id),
    stage TEXT NOT NULL CHECK (stage IN ('exif', 'preview', 'score', 'embedding')),
    content_hash TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'running', 'done', 'error', 'skipped')),
    attempts INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    interruptions INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (asset_id, stage)
);

INSERT INTO jobs SELECT * FROM phase2_jobs;
DROP TABLE phase2_jobs;
ALTER TABLE jobs ADD COLUMN model_key TEXT;
CREATE INDEX jobs_pending ON jobs (asset_id, stage) WHERE status = 'pending';
CREATE INDEX jobs_stage_status ON jobs (stage, status, asset_id);

CREATE TRIGGER jobs_insert AFTER INSERT ON jobs BEGIN
    INSERT INTO stage_counts (stage, status, count) VALUES (NEW.stage, NEW.status, 1)
        ON CONFLICT (stage, status) DO UPDATE SET count = count + 1;
END;

CREATE TRIGGER jobs_update AFTER UPDATE OF status ON jobs WHEN OLD.status <> NEW.status BEGIN
    UPDATE stage_counts SET count = count - 1 WHERE stage = OLD.stage AND status = OLD.status;
    INSERT INTO stage_counts (stage, status, count) VALUES (NEW.stage, NEW.status, 1)
        ON CONFLICT (stage, status) DO UPDATE SET count = count + 1;
END;

CREATE TRIGGER jobs_delete AFTER DELETE ON jobs BEGIN
    UPDATE stage_counts SET count = count - 1 WHERE stage = OLD.stage AND status = OLD.status;
END;

CREATE TABLE embedding_models (
    key TEXT PRIMARY KEY,
    model_id TEXT NOT NULL,
    revision TEXT NOT NULL,
    preprocessing TEXT NOT NULL
) WITHOUT ROWID;

CREATE TABLE ml_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    model_key TEXT REFERENCES embedding_models(key),
    enabled INTEGER NOT NULL DEFAULT 0,
    search_enabled INTEGER NOT NULL DEFAULT 0,
    tags_enabled INTEGER NOT NULL DEFAULT 0,
    tag_key TEXT,
    image_error TEXT,
    text_error TEXT,
    group_error TEXT,
    threshold REAL,
    generation INTEGER NOT NULL DEFAULT 0,
    grouped_generation INTEGER NOT NULL DEFAULT -1
);
INSERT INTO ml_state (id) VALUES (1);

CREATE TABLE asset_embeddings (
    asset_id INTEGER PRIMARY KEY REFERENCES assets(id),
    model_key TEXT NOT NULL REFERENCES embedding_models(key),
    content_hash TEXT NOT NULL,
    vector BLOB NOT NULL CHECK (length(vector) > 0 AND length(vector) % 2 = 0),
    tag_key TEXT
);
CREATE INDEX embeddings_by_model_asset ON asset_embeddings(model_key, asset_id);
CREATE INDEX embeddings_by_model_tag ON asset_embeddings(model_key, tag_key, asset_id);

CREATE TABLE asset_tags (
    asset_id INTEGER NOT NULL REFERENCES asset_embeddings(asset_id) ON DELETE CASCADE,
    tag TEXT NOT NULL,
    score REAL NOT NULL CHECK (score BETWEEN -1 AND 1),
    PRIMARY KEY (asset_id, tag)
) WITHOUT ROWID;
CREATE INDEX asset_tags_by_tag ON asset_tags(tag, asset_id);

CREATE TABLE similar_groups (
    id INTEGER PRIMARY KEY REFERENCES assets(id),
    member_count INTEGER NOT NULL CHECK (member_count >= 2)
);

CREATE TABLE similar_members (
    asset_id INTEGER PRIMARY KEY REFERENCES assets(id),
    group_id INTEGER NOT NULL REFERENCES similar_groups(id) ON DELETE CASCADE
);
CREATE INDEX similar_members_by_group ON similar_members(group_id, asset_id);

CREATE TRIGGER embedding_insert AFTER INSERT ON asset_embeddings BEGIN
    UPDATE ml_state SET generation = generation + 1;
    INSERT INTO burst_changes SELECT captured_at FROM assets
        WHERE id = NEW.asset_id AND active = 1 AND captured_at IS NOT NULL
        ON CONFLICT DO NOTHING;
END;

CREATE TRIGGER embedding_delete AFTER DELETE ON asset_embeddings BEGIN
    UPDATE ml_state SET generation = generation + 1;
    DELETE FROM similar_groups WHERE id = (
        SELECT group_id FROM similar_members WHERE asset_id = OLD.asset_id
    );
    INSERT INTO burst_changes SELECT captured_at FROM assets
        WHERE id = OLD.asset_id AND active = 1 AND captured_at IS NOT NULL
        ON CONFLICT DO NOTHING;
END;

CREATE TRIGGER asset_invalidates_embedding AFTER UPDATE OF content_hash, active ON assets
WHEN OLD.content_hash <> NEW.content_hash OR (OLD.active = 1 AND NEW.active = 0)
BEGIN
    DELETE FROM asset_embeddings WHERE asset_id = NEW.id;
END;

CREATE TABLE search_requests (
    id TEXT PRIMARY KEY,
    model_key TEXT NOT NULL REFERENCES embedding_models(key),
    query TEXT,
    asset_id INTEGER REFERENCES assets(id),
    content_hash TEXT,
    tag TEXT,
    tag_key TEXT,
    limit_results INTEGER NOT NULL,
    created_at REAL NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'running', 'done', 'error')),
    results TEXT,
    error TEXT,
    CHECK ((query IS NULL) <> (asset_id IS NULL))
) WITHOUT ROWID;
CREATE INDEX search_requests_pending ON search_requests(created_at, id) WHERE status = 'pending';
CREATE INDEX search_requests_expires ON search_requests(created_at);
