DROP TRIGGER jobs_insert;
DROP TRIGGER jobs_update;
DROP TRIGGER jobs_delete;
DROP INDEX jobs_pending;
DROP INDEX jobs_stage_status;

ALTER TABLE jobs RENAME TO phase1_jobs;

CREATE TABLE jobs (
    asset_id INTEGER NOT NULL REFERENCES assets(id),
    stage TEXT NOT NULL CHECK (stage IN ('exif', 'preview', 'score')),
    content_hash TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'running', 'done', 'error', 'skipped')),
    attempts INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    interruptions INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (asset_id, stage)
);

INSERT INTO jobs SELECT * FROM phase1_jobs;
DROP TABLE phase1_jobs;
DELETE FROM stage_counts;

INSERT INTO jobs (asset_id, stage, content_hash, status, error)
    SELECT id, 'score', content_hash,
           CASE WHEN kind = 'video' THEN 'skipped' ELSE 'pending' END,
           CASE WHEN kind = 'video' THEN 'Video has no image score stage' END
    FROM assets WHERE active = 1;

INSERT INTO stage_counts (stage, status, count)
    SELECT stage, status, count(*) FROM jobs GROUP BY stage, status;

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

CREATE TABLE asset_scores (
    asset_id INTEGER PRIMARY KEY REFERENCES assets(id),
    sharpness REAL NOT NULL CHECK (sharpness BETWEEN 0 AND 1),
    exposure REAL NOT NULL CHECK (exposure BETWEEN 0 AND 1),
    clipped_highlights REAL NOT NULL CHECK (clipped_highlights BETWEEN 0 AND 1),
    clipped_shadows REAL NOT NULL CHECK (clipped_shadows BETWEEN 0 AND 1),
    dhash TEXT NOT NULL CHECK (length(dhash) = 16),
    suggested_reject_reason TEXT
);

ALTER TABLE assets ADD COLUMN auto_reject_dismissed INTEGER NOT NULL DEFAULT 0
    CHECK (auto_reject_dismissed IN (0, 1));
ALTER TABLE assets ADD COLUMN burst_reject_token TEXT;

CREATE INDEX asset_burst_reject ON assets (burst_reject_token)
    WHERE burst_reject_token IS NOT NULL;
CREATE INDEX asset_capture ON assets (captured_at, id)
    WHERE active = 1 AND captured_at IS NOT NULL;

CREATE TABLE bursts (
    id INTEGER PRIMARY KEY REFERENCES assets(id),
    recommended_best_id INTEGER NOT NULL REFERENCES assets(id),
    membership_key TEXT NOT NULL
);

CREATE TABLE burst_members (
    asset_id INTEGER PRIMARY KEY REFERENCES assets(id),
    burst_id INTEGER NOT NULL REFERENCES bursts(id) ON DELETE CASCADE
);

CREATE INDEX burst_members_by_burst ON burst_members (burst_id, asset_id);

CREATE TABLE burst_choices (
    asset_id INTEGER PRIMARY KEY REFERENCES assets(id),
    chosen_revision INTEGER NOT NULL
);

CREATE TABLE analysis_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    bursts_dirty INTEGER NOT NULL DEFAULT 1,
    burst_settings TEXT NOT NULL DEFAULT '',
    scoring_settings TEXT NOT NULL DEFAULT ''
);

INSERT INTO analysis_state (id) VALUES (1);
