CREATE TABLE archive_jobs (
    id TEXT PRIMARY KEY,
    actor TEXT NOT NULL,
    request TEXT NOT NULL,
    confirmation TEXT NOT NULL,
    created_at REAL NOT NULL,
    finished_at REAL,
    cancelled INTEGER NOT NULL DEFAULT 0 CHECK(cancelled IN (0, 1))
);

CREATE TABLE archive_items (
    job_id TEXT NOT NULL REFERENCES archive_jobs(id),
    ordinal INTEGER NOT NULL,
    plan TEXT NOT NULL,
    journal TEXT,
    phase TEXT,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK(status IN ('pending', 'running', 'done', 'error', 'cancelled', 'recovery')),
    error TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(job_id, ordinal)
);

CREATE INDEX archive_items_work ON archive_items(status, job_id, ordinal)
    WHERE status IN ('pending', 'running', 'recovery');

CREATE TABLE archive_trash (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES archive_jobs(id),
    created_at REAL NOT NULL,
    reason TEXT NOT NULL,
    files TEXT NOT NULL,
    renamed_to TEXT,
    status TEXT NOT NULL DEFAULT 'available' CHECK(status IN ('available', 'restored', 'purged'))
);

CREATE INDEX archive_trash_retention ON archive_trash(created_at,id) WHERE status = 'available';

CREATE TABLE archive_audit (
    id INTEGER PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES archive_jobs(id),
    ordinal INTEGER NOT NULL,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    at REAL NOT NULL,
    details TEXT NOT NULL
);

CREATE TRIGGER archive_audit_no_replace BEFORE INSERT ON archive_audit
WHEN EXISTS (SELECT 1 FROM archive_audit WHERE id=NEW.id)
BEGIN
    SELECT RAISE(ABORT, 'Archive audit is append-only');
END;

CREATE TRIGGER archive_audit_no_update BEFORE UPDATE ON archive_audit BEGIN
    SELECT RAISE(ABORT, 'Archive audit is append-only');
END;

CREATE TRIGGER archive_audit_no_delete BEFORE DELETE ON archive_audit BEGIN
    SELECT RAISE(ABORT, 'Archive audit is append-only');
END;

CREATE TABLE archive_video (
    asset_id INTEGER PRIMARY KEY REFERENCES assets(id) ON DELETE CASCADE,
    content_hash TEXT NOT NULL,
    codec TEXT NOT NULL,
    bit_rate INTEGER,
    width INTEGER,
    height INTEGER,
    duration REAL,
    captured_at TEXT,
    make TEXT,
    model TEXT
);

CREATE INDEX archive_exact_hash ON assets(content_hash,id) WHERE active=1;

CREATE TABLE archive_rating_transfer (
    path TEXT PRIMARY KEY,
    content_hash TEXT NOT NULL,
    rating INTEGER NOT NULL,
    rejected INTEGER NOT NULL
);

CREATE TABLE archive_permissions (
    path TEXT PRIMARY KEY,
    writable INTEGER NOT NULL CHECK(writable IN (0,1)),
    checked_at REAL NOT NULL
);
