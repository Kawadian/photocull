CREATE TABLE archive_previews (
    id TEXT PRIMARY KEY,
    request TEXT NOT NULL,
    policy_key TEXT NOT NULL,
    created_at REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','running','ready','error')),
    finished_at REAL,
    confirmation TEXT,
    summary TEXT,
    error TEXT
);
CREATE INDEX archive_previews_confirmation ON archive_previews(confirmation);
CREATE UNIQUE INDEX archive_previews_one_active ON archive_previews((1))
    WHERE status IN ('pending','running');

CREATE TABLE archive_preview_items (
    preview_id TEXT NOT NULL REFERENCES archive_previews(id) ON DELETE CASCADE,
    ordinal INTEGER NOT NULL,
    payload TEXT NOT NULL,
    eligible INTEGER NOT NULL CHECK(eligible IN (0,1)),
    PRIMARY KEY(preview_id,ordinal)
) WITHOUT ROWID;

ALTER TABLE archive_jobs ADD COLUMN preview_id TEXT REFERENCES archive_previews(id);
ALTER TABLE archive_jobs ADD COLUMN manifest_cursor INTEGER NOT NULL DEFAULT 0;
ALTER TABLE archive_jobs ADD COLUMN manifest_remaining INTEGER NOT NULL DEFAULT 0;

UPDATE archive_jobs SET request=json_set(request,'$.dng_preview','omit')
WHERE json_extract(request,'$.operation')='dng' AND json_type(request,'$.dng_preview') IS NULL;

DROP INDEX archive_items_work;
ALTER TABLE archive_items RENAME TO archive_items_v17;
CREATE TABLE archive_items (
    job_id TEXT NOT NULL REFERENCES archive_jobs(id),
    ordinal INTEGER NOT NULL,
    plan TEXT NOT NULL,
    journal TEXT,
    phase TEXT,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK(status IN ('pending', 'running', 'done', 'error', 'skipped', 'cancelled', 'recovery')),
    error TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(job_id, ordinal)
);
INSERT INTO archive_items SELECT * FROM archive_items_v17;
DROP TABLE archive_items_v17;
CREATE INDEX archive_items_work ON archive_items(status,job_id,ordinal)
    WHERE status IN ('pending','running','recovery');
CREATE INDEX archive_items_progress ON archive_items(job_id,status);
CREATE INDEX archive_blocked_trash ON archive_items(json_extract(plan,'$.trash_id'))
    WHERE status='recovery';
