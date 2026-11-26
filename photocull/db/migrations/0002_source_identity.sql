ALTER TABLE assets ADD COLUMN source_key TEXT;
CREATE INDEX jobs_stage_status ON jobs (stage, status, asset_id);

CREATE TABLE catalog_status (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    revision INTEGER NOT NULL DEFAULT 0
);

INSERT INTO catalog_status (id) VALUES (1);
